"""
db.py — unified database layer

- SQLite  when DATABASE_URL is empty (local dev / PythonAnywhere)
- PostgreSQL when DATABASE_URL is set (Render + Supabase in prod)

All callers use the same get_db() API.  The wrapper translates:
  - ? placeholder  →  %s
  - datetime('now') / datetime('now', '+N X')  →  NOW() / NOW() + INTERVAL
  - INTEGER PRIMARY KEY AUTOINCREMENT  →  SERIAL PRIMARY KEY (in CREATE TABLE)
  - INSERT OR IGNORE  →  INSERT ... ON CONFLICT DO NOTHING
"""

from __future__ import annotations
import os
import re
import sqlite3

DATABASE_URL: str = os.environ.get("DATABASE_URL", "")

# Auto-place SQLite on the persistent volume when /data (or DATA_DIR) is mounted.
# This ensures the database survives container rebuilds on Coolify/Render.
# Explicit SQLITE_PATH env var always takes priority.
if os.environ.get("SQLITE_PATH"):
    SQLITE_PATH: str = os.environ["SQLITE_PATH"]
elif not DATABASE_URL:
    _auto_data = os.environ.get("DATA_DIR", "/data")
    if os.path.isdir(_auto_data):
        SQLITE_PATH = os.path.join(_auto_data, "doccompare.db")
    else:
        SQLITE_PATH = "instance/doccompare.db"
else:
    SQLITE_PATH = "instance/doccompare.db"

# Coolify / Heroku sometimes emit "postgres://" — psycopg2 requires "postgresql://"
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

# ── SQL translation ───────────────────────────────────────────────────────────

_INTERVAL_RE      = re.compile(r"datetime\('now',\s*'([^']+)'\)", re.IGNORECASE)
_NOW_RE           = re.compile(r"datetime\('now'\)", re.IGNORECASE)
_DATE_NOW_RE      = re.compile(r"date\('now'\)", re.IGNORECASE)
_DATE_NOW_INT_RE  = re.compile(r"date\('now',\s*'([^']+)'\)", re.IGNORECASE)
_DATE_COL_RE      = re.compile(r"\bdate\(([\w.]+)\)", re.IGNORECASE)  # też kolumny kwalifikowane: date(t.created_at)


def _pg_sql(sql: str) -> str:
    # The datetime('now'…) / date('now'…) idioms are SQLite-specific and embed
    # their own single-quoted literals ('now', '+7 days'), so they must be matched
    # over the full string. The bare date(col) → (col)::date rewrite, however, can
    # accidentally fire inside a genuine quoted string literal (e.g. a value like
    # 'date(x)'), so it is applied only outside single-quoted literals — together
    # with the ?→%s placeholder substitution.
    sql = _NOW_RE.sub("NOW()", sql)
    sql = _INTERVAL_RE.sub(lambda m: f"NOW() + INTERVAL '{m.group(1)}'", sql)
    sql = _DATE_NOW_INT_RE.sub(
        lambda m: f"(CURRENT_DATE + INTERVAL '{m.group(1)}')", sql)
    sql = _DATE_NOW_RE.sub("CURRENT_DATE", sql)

    # Split into segments: even idx = outside literals, odd idx = quoted literals.
    _parts = re.split(r"('(?:[^']|'')*')", sql)

    def _xlate(seg: str) -> str:
        seg = seg.replace("?", "%s")
        seg = _DATE_COL_RE.sub(lambda m: f"({m.group(1)})::date", seg)
        return seg

    sql = "".join(
        _xlate(p) if i % 2 == 0 else p
        for i, p in enumerate(_parts)
    )
    sql = re.sub(r'\blast_insert_rowid\(\)', 'lastval()', sql, flags=re.IGNORECASE)
    # SQLite BLOB → PostgreSQL BYTEA
    sql = re.sub(r'\bBLOB\b', 'BYTEA', sql, flags=re.IGNORECASE)
    if re.search(r"\bINSERT\s+OR\s+IGNORE\b", sql, re.IGNORECASE):
        sql = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", sql, flags=re.IGNORECASE)
        if "ON CONFLICT" not in sql.upper():
            sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    if re.search(r"\bCREATE\s+TABLE\b", sql, re.IGNORECASE):
        sql = re.sub(
            r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
            "SERIAL PRIMARY KEY",
            sql, flags=re.IGNORECASE,
        )
        sql = re.sub(
            r"\bINTEGER\s+PRIMARY\s+KEY\b",
            "SERIAL PRIMARY KEY",
            sql, flags=re.IGNORECASE,
        )
        # DEFAULT (datetime('now')) already handled above; strip leftover parens
        sql = re.sub(r"DEFAULT\s+\(NOW\(\)\)", "DEFAULT NOW()", sql, flags=re.IGNORECASE)
        # For TIMESTAMP/TIMESTAMPTZ columns NOW() is correct; for TEXT columns cast to ::TEXT
        sql = re.sub(
            r'(\bTIMESTAMP(?:TZ)?\b[^,]*?)DEFAULT NOW\(\)(?!::)',
            r'\1DEFAULT NOW()::TIMESTAMPTZ',
            sql, flags=re.IGNORECASE | re.DOTALL,
        )
        sql = re.sub(r"\bDEFAULT NOW\(\)(?!::)", "DEFAULT NOW()::TEXT", sql, flags=re.IGNORECASE)
    return sql


# ── PostgreSQL row / cursor / connection wrappers ─────────────────────────────

class _PGRow:
    """Mimics sqlite3.Row: supports row["col"] and dict(row)."""
    __slots__ = ("_d", "_vals")

    def __init__(self, description, values):
        vals = list(values)
        # Przy zduplikowanych nazwach kolumn (JOIN a.*, b.* → dwie 'id') zachowaj
        # PIERWSZE wystąpienie — tak samo jak sqlite3.Row indeksowany nazwą.
        # Dict-comprehension zostawiałby OSTATNIE → cichy rozjazd SQLite vs Postgres.
        d = {}
        for col, val in zip(description, vals):
            if col.name not in d:
                d[col.name] = val
        self._d = d
        self._vals = vals

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._vals[key]
        return self._d[key]

    def __iter__(self):
        return iter(self._d.values())

    def keys(self):
        return list(self._d.keys())

    def get(self, key, default=None):
        if isinstance(key, int):
            return self._vals[key] if 0 <= key < len(self._vals) else default
        return self._d.get(key, default)

    def __repr__(self):
        return f"<PGRow {self._d!r}>"


class _PGCursor:
    def __init__(self, cur):
        self._c = cur

    def execute(self, sql: str, params=()):
        # None (nie ()) dla braku parametrów — psycopg2 z pustym tuple próbuje
        # interpolować '%' w bezparametrowych DDL (np. DEFAULT '%Y/%m/%d',
        # CHECK url LIKE 'http://%') → 'tuple index out of range'. Z None nie tyka %.
        self._c.execute(_pg_sql(sql), params if params else None)
        return self

    def executemany(self, sql: str, seq):
        self._c.executemany(_pg_sql(sql), seq)
        return self

    def fetchone(self):
        row = self._c.fetchone()
        return _PGRow(self._c.description, row) if row is not None else None

    def fetchall(self):
        return [_PGRow(self._c.description, r) for r in (self._c.fetchall() or [])]

    @property
    def rowcount(self):
        return self._c.rowcount

    @property
    def lastrowid(self):
        # NOTE: lastval() returns the last value generated by a sequence in this
        # session. After INSERT … ON CONFLICT DO NOTHING that did nothing, or an
        # INSERT into a table without a sequence, this can be stale/None — there is
        # no generic reliable rewrite (RETURNING id would have to be threaded through
        # every caller). Callers needing a guaranteed id should use RETURNING
        # explicitly. We at least never raise: on any error we return None.
        cur = None
        try:
            cur = self._c.connection.cursor()
            cur.execute("SELECT lastval()")
            row = cur.fetchone()
            return row[0] if row else None
        except Exception:
            return None
        finally:
            if cur:
                cur.close()

    def __iter__(self):
        for row in self._c:
            yield _PGRow(self._c.description, row)


class _PGConnection:
    """Wraps psycopg2 connection to mimic sqlite3.Connection."""

    def __init__(self, conn):
        self._conn = conn
        self._closed = False

    def execute(self, sql: str, params=()):
        cur = _PGCursor(self._conn.cursor())
        cur.execute(sql, params)
        return cur

    def executemany(self, sql: str, seq):
        cur = _PGCursor(self._conn.cursor())
        cur.executemany(sql, seq)
        return cur

    def executescript(self, script: str):
        """Run a semicolon-delimited SQL script atomically (sqlite3 compat).

        All statements execute within a single transaction: either every
        statement succeeds and the transaction commits, or the first failure
        rolls back the entire batch.
        """
        # Split on ; only outside single-quoted strings to handle DEFAULT 'val;1' etc.
        _parts = re.split(r"('(?:[^']|'')*')", script)
        _joined = "".join(
            p.replace(";", "\x00") if i % 2 == 0 else p
            for i, p in enumerate(_parts)
        )
        stmts = [s.strip() for s in _joined.split("\x00") if s.strip()]
        cur = self._conn.cursor()
        try:
            for stmt in stmts:
                cur.execute(_pg_sql(stmt))
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()
        self._conn.commit()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if not self._closed:
            self._conn.close()
            self._closed = True

    def cursor(self):
        return _PGCursor(self._conn.cursor())

    # context-manager support
    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self.close()


# ── Public API ────────────────────────────────────────────────────────────────

# ── PostgreSQL connection pool (created lazily on first use) ──────────────────
_pg_pool = None
_pg_pool_lock = __import__("threading").Lock()

# Server-side statement timeout (ms) applied to every PG connection so a runaway
# query can't pin a pooled connection indefinitely (SQLite uses busy_timeout for
# lock waits below; that's a different thing). Configurable; "0" disables.
_STMT_TIMEOUT_MS = os.environ.get("DB_STATEMENT_TIMEOUT_MS", "30000").strip()

def _pg_connect_kwargs() -> dict:
    """kwargs passed to psycopg2.connect / pool so the timeout applies once per
    physical connection (session default), not as a per-query round-trip."""
    if _STMT_TIMEOUT_MS and _STMT_TIMEOUT_MS not in ("0", ""):
        return {"options": f"-c statement_timeout={_STMT_TIMEOUT_MS}"}
    return {}

def _get_pg_pool():
    """Return a psycopg2 ThreadedConnectionPool (min=1, max=10).

    Using a pool instead of psycopg2.connect() per request avoids the
    TCP handshake + TLS overhead on every query (~5-20 ms on cloud DBs).
    """
    global _pg_pool
    if _pg_pool is None:
        with _pg_pool_lock:
            if _pg_pool is None:
                try:
                    from psycopg2 import pool as _pool
                    _pg_max = max(2, int(os.environ.get("DB_POOL_SIZE", "10")))
                    _pg_pool = _pool.ThreadedConnectionPool(
                        minconn=1, maxconn=_pg_max, dsn=DATABASE_URL,
                        **_pg_connect_kwargs()
                    )
                except Exception as _e:
                    import logging as _log
                    _log.getLogger("db").warning("PG pool creation failed, falling back to direct connect: %s", _e)
    return _pg_pool


def get_db() -> "_PGConnection | sqlite3.Connection":
    """Return an open database connection.

    For PostgreSQL: connections are drawn from a ThreadedConnectionPool
    (min=1, max=10) and returned on db.close() / Flask teardown.

    When called inside a Flask request context the connection is registered
    for automatic cleanup at teardown, so leaked connections (routes that
    raise an exception before reaching db.close()) are always released.
    """
    if DATABASE_URL:
        import psycopg2  # noqa: PLC0415
        pool = _get_pg_pool()
        if pool is not None:
            try:
                raw_conn = pool.getconn()
                raw_conn.autocommit = False
                conn = _PGConnection(raw_conn)
                # Wrap close() to return connection to pool exactly once.
                # Without the guard, Flask teardown calling close() after a
                # route that already called close() would putconn() twice,
                # corrupting psycopg2's internal pool state.
                _pool_returned = [False]
                def _pool_close():
                    if _pool_returned[0]:
                        return
                    _pool_returned[0] = True
                    _rollback_ok = True
                    try:
                        raw_conn.rollback()  # discard uncommitted work before returning to pool
                    except Exception:
                        _rollback_ok = False
                    if _rollback_ok:
                        try:
                            pool.putconn(raw_conn)
                        except Exception:
                            try:
                                raw_conn.close()
                            except Exception:
                                pass
                    else:
                        # Connection is broken — close and free the pool slot via putconn
                        try:
                            pool.putconn(raw_conn, close=True)
                        except Exception:
                            try:
                                raw_conn.close()
                            except Exception:
                                pass
                conn.close = _pool_close
            except Exception:
                # Pool exhausted or error — fall back to direct connect
                raw_conn = psycopg2.connect(DATABASE_URL, **_pg_connect_kwargs())
                raw_conn.autocommit = False
                conn = _PGConnection(raw_conn)
        else:
            raw_conn = psycopg2.connect(DATABASE_URL, **_pg_connect_kwargs())
            raw_conn.autocommit = False
            conn = _PGConnection(raw_conn)
        db = conn
    else:
        os.makedirs(os.path.dirname(SQLITE_PATH) or ".", exist_ok=True)
        db = sqlite3.connect(SQLITE_PATH, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")    # concurrent reads during writes
            db.execute("PRAGMA busy_timeout=8000")   # wait up to 8s if locked (multiple workers)
            db.execute("PRAGMA synchronous=NORMAL")  # WAL + NORMAL = safe + faster
            db.execute("PRAGMA foreign_keys=ON")     # enforce FK constraints
        except Exception:
            # A failing PRAGMA must not leak the open connection (it is not yet
            # registered on flask.g for teardown cleanup).
            try:
                db.close()
            except Exception:
                pass
            raise
    try:
        from flask import g, has_request_context
        if has_request_context():
            if not hasattr(g, "_open_dbs"):
                g._open_dbs = []
            g._open_dbs.append(db)
    except Exception:
        pass
    return db


# Unified IntegrityError — catch this instead of sqlite3.IntegrityError
try:
    import psycopg2 as _pg
    IntegrityError = (sqlite3.IntegrityError, _pg.IntegrityError)
except ImportError:
    IntegrityError = (sqlite3.IntegrityError,)


def escape_like(value: str) -> str:
    """Escape a user-supplied string for safe use in a SQL LIKE pattern.

    Escapes the LIKE wildcards (% _) and the escape char itself (\\) so the
    value is matched literally. Use together with: ... LIKE ? ESCAPE '\\'.
    Centralises the pattern previously duplicated across route handlers.
    """
    return (str(value or "")
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_"))
