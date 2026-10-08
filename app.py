import io
import os
import re
import json
import shutil
import logging
import secrets
import threading
import time
import zipfile
from functools import wraps
from datetime import timedelta
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, g, send_file, flash, Response, has_request_context
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from db import get_db, IntegrityError, escape_like
from core.security import (
    role_level, can_see_all, can_delete, is_admin,
    login_required, require_role, csrf_protect,
    check_session_active as _check_session_active,
    get_csrf_token as _get_csrf_token,
    verify_csrf as _verify_csrf,
)
from core.audit import log_audit as _log_audit
from constants import ROLE_LEVEL, VALID_ROLES, API_RATE_MAX, API_RATE_WINDOW, KPI_CACHE_TTL, MAX_UPLOAD_BYTES, MAX_UPLOAD_MB, RenderVariant
from translations import t as _t_func, TRANSLATIONS
from translation_en import translate_html as _translate_html_en

# Ładuj .env (lokalne dev) — ignoruj brak pliku
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Logging ───────────────────────────────────────────────────────────────────
from logging.handlers import RotatingFileHandler
_log_dir = os.environ.get("LOG_DIR", "logs")
_log_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_log_fmt)
_handlers = [_stream_handler]
try:
    os.makedirs(_log_dir, exist_ok=True)
    _file_handler = RotatingFileHandler(
        os.path.join(_log_dir, "app.log"),
        maxBytes=10 * 1024 * 1024,  # 10 MB per file
        backupCount=5,
        encoding="utf-8",
    )
    _file_handler.setFormatter(_log_fmt)
    _handlers.append(_file_handler)
except OSError:
    pass  # log dir not writable — fall back to stdout only
logging.basicConfig(level=logging.INFO, handlers=_handlers)
logger = logging.getLogger("doccompare")

app = Flask(__name__)

# Trust one reverse-proxy hop (Render, Heroku, nginx) so request.remote_addr
# reflects the real client IP rather than the load-balancer address.
try:
    _proxy_count = int(os.environ.get("PROXY_COUNT", "1"))
    if _proxy_count < 0 or _proxy_count > 10:
        raise ValueError("out of range")
except (ValueError, TypeError):
    logger.warning("PROXY_COUNT invalid — defaulting to 1")
    _proxy_count = 1
if _proxy_count > 0:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=_proxy_count, x_proto=_proxy_count, x_host=_proxy_count)

def _load_or_create_secret_key():
    """Return a stable random session key persisted to instance/.secret_key.

    Used only when SECRET_KEY env is absent. Persisting (rather than a random
    per-process key) keeps sessions valid across both gunicorn workers and
    across restarts, while removing the predictable hard-coded key. Returns None
    if the filesystem is not writable (caller falls back). O_EXCL avoids a race
    when workers start concurrently.
    """
    import secrets as _secrets
    path = os.path.join("instance", ".secret_key")
    try:
        os.makedirs("instance", exist_ok=True)
        if os.path.exists(path):
            with open(path) as _f:
                k = _f.read().strip()
                if k:
                    return k
        key = _secrets.token_hex(32)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as _f:
                _f.write(key)
            return key
        except FileExistsError:                       # another worker won the race
            with open(path) as _f:
                return _f.read().strip() or key
    except Exception:
        return None


_secret_key = os.environ.get("SECRET_KEY", "")
if not _secret_key:
    _persisted = _load_or_create_secret_key()
    if _persisted:
        logger.critical(
            "SECRET_KEY env not set — using a persisted random key (instance/.secret_key). "
            "Set SECRET_KEY in the environment for stable, secure sessions."
        )
        _secret_key = _persisted
    elif os.environ.get("DATABASE_URL"):
        # Production (PostgreSQL) — refuse to boot with a publicly-known key.
        raise SystemExit(
            "FATAL: SECRET_KEY env not set and key file unwritable in a production "
            "deployment (DATABASE_URL is set). Set SECRET_KEY in the environment."
        )
    else:
        logger.critical(
            "SECRET_KEY env not set and key file unwritable — falling back to an INSECURE "
            "built-in key. Set SECRET_KEY in the environment immediately."
        )
        _secret_key = "doccompare-local-secret-2024"
app.secret_key = _secret_key

# Katalog uploadów: preferuj trwały wolumen, by pliki (np. bajty masterów do
# podglądu/porównania) NIE ginęły przy każdym redeployu kontenera.
#  1) jawny UPLOAD_FOLDER z env (np. ustawiony w Coolify/Render), albo
#  2) /data/uploads, jeśli istnieje trwały mount /data, albo
#  3) fallback 'uploads' (efemeryczny — tylko gdy nie ma trwałego wolumenu).
_upload_folder = os.environ.get("UPLOAD_FOLDER") or (
    "/data/uploads" if os.path.isdir("/data") else "uploads")
try:
    os.makedirs(_upload_folder, exist_ok=True)
except OSError:
    _upload_folder = "uploads"
    try:
        os.makedirs(_upload_folder, exist_ok=True)
    except OSError:
        pass
app.config["UPLOAD_FOLDER"] = _upload_folder
app.config["LIBRARY_FOLDER"] = os.environ.get("LIBRARY_FOLDER", "library")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)
# Secure session cookie + HSTS. Domyślnie WŁĄCZONE w produkcji (DATABASE_URL
# ustawione — Coolify/Render serwują po HTTPS), żeby zapomnienie FORCE_HTTPS nie
# wysyłało ciasteczka sesji bez flagi Secure ani nie pomijało HSTS. Jawny
# FORCE_HTTPS nadpisuje auto-detekcję w obie strony (0/false wymusza wyłączenie,
# np. do lokalnego testu prod-configu po HTTP).
_force_https_env = os.environ.get("FORCE_HTTPS", "").strip().lower()
if _force_https_env in ("true", "1", "yes", "on"):
    _is_https = True
elif _force_https_env in ("false", "0", "no", "off"):
    _is_https = False
else:
    _is_https = bool(os.environ.get("DATABASE_URL"))
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
app.config["SESSION_COOKIE_SECURE"] = _is_https

# ── Blueprinty (trasy wydzielone z app.py) ────────────────────────────────────
from blueprints.incoterms import bp as _incoterms_bp, ensure_incoterms_table as _ensure_incoterms_table
from blueprints.translation_dict import bp as _translation_dict_bp
from blueprints.transit_countries import bp as _transit_bp, list_countries as _list_transit_countries
from blueprints.palviz import bp as _palviz_bp
app.register_blueprint(_incoterms_bp)
app.register_blueprint(_translation_dict_bp)
app.register_blueprint(_transit_bp)
app.register_blueprint(_palviz_bp)


@app.teardown_appcontext
def _auto_close_dbs(exc):
    """Close any DB connections that routes opened but didn't close (e.g. on exception)."""
    for _db in g.pop("_open_dbs", []):
        try:
            _db.close()
        except Exception:
            pass


@app.after_request
def _set_security_headers(response):
    response.headers.pop("Server", None)
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-XSS-Protection", "1; mode=block")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://unpkg.com https://cdn.tailwindcss.com https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.tailwindcss.com; "
        "font-src 'self' data: https://fonts.gstatic.com; "
        "img-src 'self' data: blob: https://*.tile.openstreetmap.org; "
        "connect-src 'self' blob:; "          # model-viewer pobiera tekstury GLB przez fetch(blob:) — bez tego białe ściany 3D
        "worker-src 'self' blob:; "           # model-viewer dekoduje w Web Workerze z blob:

        "frame-ancestors 'self';"
    )
    response.headers.setdefault(
        "Permissions-Policy",
        "geolocation=(), camera=(), microphone=(), payment=()"
    )
    # Instruct caches to vary on Accept-Language and Cookie so localised pages
    # aren't served to the wrong user/language.
    if response.content_type and "text/html" in response.content_type:
        existing_vary = response.headers.get("Vary", "")
        extras = [v for v in ("Accept-Language", "Cookie") if v.lower() not in existing_vary.lower()]
        if extras:
            new_vary = ", ".join(filter(None, [existing_vary] + extras))
            response.headers["Vary"] = new_vary
        if "user_id" in session:
            response.headers.setdefault("Cache-Control", "no-cache, no-store, must-revalidate")
            response.headers.setdefault("Pragma", "no-cache")
    if _is_https:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains; preload")
    return response


# ── Sentry ────────────────────────────────────────────────────────────────────
_sentry_dsn = os.environ.get("SENTRY_DSN", "")
if _sentry_dsn:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        sentry_sdk.init(
            dsn=_sentry_dsn,
            integrations=[FlaskIntegration()],
            traces_sample_rate=0.2,
            environment=os.environ.get("RENDER_SERVICE_NAME", "local"),
        )
        logger.info("Sentry initialized")
    except ImportError:
        logger.warning("sentry-sdk not installed — Sentry disabled")

# ── Shipment document storage path ───────────────────────────────────────────
# Domyślnie na TRWAŁY dysk (/data) jeśli jest zamontowany (Render persistent disk),
# inaczej lokalnie w instance/. Inaczej pliki dostaw znikają przy każdym redeployu
# (rekordy w bazie zostają → kafelki widać, ale pliku na dysku brak).
_DATA_DIR_DEFAULT = os.environ.get("DATA_DIR", "/data")
_SHIPMENT_DOCS_PATH = os.path.abspath(os.environ.get(
    "SHIPMENT_DOCS_PATH",
    os.path.join(_DATA_DIR_DEFAULT, "shipment_docs")
    if os.path.isdir(_DATA_DIR_DEFAULT)
    else os.path.join(os.path.dirname(__file__), "instance", "shipment_docs")
))


def _ts_ago(**kwargs) -> str:
    """'YYYY-MM-DD HH:MM:SS' dla teraz minus delta. Używać zamiast SQL
    datetime('now','-N ...') przy porównaniach z kolumnami TEXT — na PostgreSQL
    SQL-owy wariant tłumaczy się na NOW()+INTERVAL (timestamptz) i porównanie
    z TEXT rzuca 'operator does not exist'."""
    from datetime import datetime as _d, timedelta as _t, timezone as _z
    return (_d.now(_z.utc) - _t(**kwargs)).strftime("%Y-%m-%d %H:%M:%S")


def _validate_startup_config() -> None:
    """Validate critical configuration at startup and log errors/warnings."""
    _db_url = os.environ.get("DATABASE_URL", "")
    errors: list[str] = []
    warnings: list[str] = []

    _known_insecure = {"", "dev-secret", "doccompare-local-secret-2024"}
    if not os.environ.get("SECRET_KEY") or os.environ.get("SECRET_KEY") in _known_insecure:
        if _db_url:
            errors.append("SECRET_KEY must be set explicitly in production (DATABASE_URL is set)")
        else:
            warnings.append("SECRET_KEY not set — sessions are forgeable; set SECRET_KEY env var")

    upload_folder = app.config.get("UPLOAD_FOLDER", "uploads")
    if _db_url and not str(upload_folder).startswith("/data"):
        warnings.append(
            f"UPLOAD_FOLDER={upload_folder!r} may be ephemeral in container deployments — consider /data/uploads"
        )

    for _e in errors:
        logger.error("STARTUP CONFIG ERROR: %s", _e)
    for _w in warnings:
        logger.warning("STARTUP CONFIG WARNING: %s", _w)

    # In production (DATABASE_URL set) a misconfigured SECRET_KEY is fatal — refuse
    # to boot rather than serve forgeable sessions.
    if _db_url and errors:
        raise SystemExit("FATAL startup config: " + "; ".join(errors))


_validate_startup_config()

# Rate limiting: { ip: [timestamp, ...] }
_login_attempts: dict = {}
_login_attempts_lock = threading.Lock()   # guards read-modify-write on _login_attempts
_RATE_LIMIT_MAX = 5       # max prób na okno
_RATE_LIMIT_WINDOW = 900  # 15 minut (sekundy)

_forgot_attempts: dict = {}  # rate limit for password reset requests
_forgot_attempts_lock = threading.Lock()  # separate lock to avoid contention with _login_attempts_lock
_FORGOT_LIMIT_MAX = 3
_FORGOT_LIMIT_WINDOW = 900   # 15 minut

# Per-user rate limiting for expensive AI/comparison endpoints
# { user_id: [timestamp, ...] }
_api_attempts: dict = {}
_api_attempts_lock = threading.Lock()

# Async job store — backed by the DB so job state is shared across gunicorn
# workers (a client polling worker B sees a job created by worker A) and
# survives worker recycling (max_requests).
# Row shape: {id, user_id, status, step, step_label, pct, result(JSON), error,
#             created_at, done_at}
# Used by /api/compare-async, /api/compare-async/<job_id>, /api/compare-async/<job_id>/events
_ASYNC_JOB_TTL = 3600        # seconds before a completed job is evicted
_ASYNC_JOB_STALE_TTL = 7200  # 2 h — in-progress jobs older than this are treated as crashed
_async_jobs_table_ready = False

def _ensure_async_jobs_table(db) -> None:
    global _async_jobs_table_ready
    if _async_jobs_table_ready:
        return
    db.execute("""CREATE TABLE IF NOT EXISTS async_jobs (
        id TEXT PRIMARY KEY,
        user_id INTEGER,
        status TEXT NOT NULL DEFAULT 'pending',
        step INTEGER DEFAULT 0,
        step_label TEXT DEFAULT '',
        pct INTEGER DEFAULT 0,
        result TEXT,
        error TEXT DEFAULT '',
        created_at DOUBLE PRECISION,
        done_at DOUBLE PRECISION
    )""")
    db.commit()
    _async_jobs_table_ready = True

def _async_job_create(user_id: int) -> str:
    import uuid
    jid = uuid.uuid4().hex[:16]
    db = get_db()
    try:
        _ensure_async_jobs_table(db)
        db.execute(
            "INSERT INTO async_jobs(id,user_id,status,step,step_label,pct,result,error,created_at) "
            "VALUES(?,?,'pending',0,'Oczekiwanie…',0,NULL,'',?)",
            (jid, user_id, time.time()),
        )
        db.commit()
    finally:
        db.close()
    return jid

def _async_job_set(jid: str, status: str, result=None, error: str = "",
                   step: int = 0, step_label: str = "", pct: int = 0):
    done_at = time.time() if status in ("done", "error") else None
    if status == "done":
        pct = 100
    db = get_db()
    try:
        _ensure_async_jobs_table(db)
        if result is not None:
            db.execute(
                "UPDATE async_jobs SET status=?, error=?, step=?, step_label=?, pct=?, "
                "result=?, done_at=COALESCE(?, done_at) WHERE id=?",
                (status, error, step, step_label or status, pct,
                 json.dumps(result, ensure_ascii=False, default=str), done_at, jid),
            )
        else:
            db.execute(
                "UPDATE async_jobs SET status=?, error=?, step=?, step_label=?, pct=?, "
                "done_at=COALESCE(?, done_at) WHERE id=?",
                (status, error, step, step_label or status, pct, done_at, jid),
            )
        db.commit()
    finally:
        db.close()

def _async_job_get(jid: str) -> dict | None:
    db = get_db()
    try:
        _ensure_async_jobs_table(db)
        row = db.execute("SELECT * FROM async_jobs WHERE id=?", (jid,)).fetchone()
        if not row:
            return None
        entry = dict(row)
        now = time.time()
        # Evict completed/errored jobs past their TTL
        if entry.get("done_at") and (now - entry["done_at"]) > _ASYNC_JOB_TTL:
            db.execute("DELETE FROM async_jobs WHERE id=?", (jid,)); db.commit()
            return None
        # Evict stale in-progress jobs (worker thread likely crashed)
        if entry.get("status") not in ("done", "error") and \
                (now - (entry.get("created_at") or now)) > _ASYNC_JOB_STALE_TTL:
            db.execute("DELETE FROM async_jobs WHERE id=?", (jid,)); db.commit()
            return None
        # Deserialize result JSON back to a dict (matches prior in-memory shape)
        if entry.get("result"):
            try:
                entry["result"] = json.loads(entry["result"])
            except (ValueError, TypeError):
                entry["result"] = None
        else:
            entry["result"] = None
        return entry
    finally:
        db.close()

# KPI data cache — keyed by (uid_key, period, user_filter), TTL 5 minutes
_kpi_cache: dict = {}
_kpi_cache_lock = threading.Lock()

def _kpi_cache_get(key: tuple):
    with _kpi_cache_lock:
        entry = _kpi_cache.get(key)
        if entry and (time.time() - entry[0]) < KPI_CACHE_TTL:
            return entry[1]
    return None

def _kpi_cache_set(key: tuple, data):
    with _kpi_cache_lock:
        _kpi_cache[key] = (time.time(), data)

def _kpi_cache_invalidate():
    """Call after any comparison is written to keep KPI fresh."""
    with _kpi_cache_lock:
        _kpi_cache.clear()

_api_rate_table_ready = False

def _ensure_api_rate_table(db) -> None:
    global _api_rate_table_ready
    if _api_rate_table_ready:
        return
    db.execute("""CREATE TABLE IF NOT EXISTS api_rate_events (
        user_id INTEGER NOT NULL,
        created_at DOUBLE PRECISION NOT NULL
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_api_rate_user_ts ON api_rate_events(user_id, created_at)")
    db.commit()
    _api_rate_table_ready = True

def _check_and_record_api_rate(user_id: int) -> bool:
    """Check the per-user API rate limit and record the call. Returns True if limited.

    DB-backed (table api_rate_events) so the limit is shared across gunicorn
    workers instead of being counted per-process (which let the effective limit
    scale with WEB_CONCURRENCY). Falls back to the in-memory dict on DB errors."""
    now = time.time()
    cutoff = now - API_RATE_WINDOW
    try:
        db = get_db()
        try:
            _ensure_api_rate_table(db)
            db.execute("DELETE FROM api_rate_events WHERE user_id=? AND created_at < ?",
                       (user_id, cutoff))
            count = db.execute(
                "SELECT COUNT(*) FROM api_rate_events WHERE user_id=? AND created_at >= ?",
                (user_id, cutoff)).fetchone()[0]
            if count >= API_RATE_MAX:
                db.commit()
                return True
            db.execute("INSERT INTO api_rate_events(user_id, created_at) VALUES(?,?)",
                       (user_id, now))
            db.commit()
            return False
        finally:
            db.close()
    except Exception:
        with _api_attempts_lock:
            ts = [t for t in _api_attempts.get(user_id, []) if now - t < API_RATE_WINDOW]
            if len(ts) >= API_RATE_MAX:
                _api_attempts[user_id] = ts
                return True
            ts.append(now)
            _api_attempts[user_id] = ts[-(API_RATE_MAX + 5):]
            return False

# ── Upload cleanup thread ──────────────────────────────────────────────────────
def _cleanup_uploads():
    """Delete upload files + regenerowalne artefakty 3D/PalViz starsze niż 24 h.
    Co 30 min w tle. NIE rusza masters/ (trwałe wzorce) — tylko efemeryczne wyniki."""
    upload_dir = app.config["UPLOAD_FOLDER"]
    cutoff = 24 * 3600
    subdirs = ["artwork3d", "palviz_glb", "palviz_zip", "tpl_preview"]
    while True:
        time.sleep(1800)
        now = time.time()
        for d in [upload_dir] + [os.path.join(upload_dir, s) for s in subdirs]:
            if not os.path.isdir(d):
                continue
            try:
                for fname in os.listdir(d):
                    fpath = os.path.join(d, fname)
                    try:
                        if os.path.isfile(fpath) and (now - os.path.getmtime(fpath)) > cutoff:
                            os.remove(fpath)
                    except (OSError, FileNotFoundError):
                        pass
            except Exception as _exc:
                logger.warning("Upload cleanup error in %s: %s", d, _exc)

_cleanup_thread = threading.Thread(target=_cleanup_uploads, daemon=True)
_cleanup_thread.start()


# ── Automatic SQLite backup (daily) ───────────────────────────────────────────

def _backup_sqlite_once(backup_dir: str) -> str | None:
    """Copy the SQLite DB to backup_dir/doccompare_YYYYMMDD_HHMMSS.db using the
    SQLite online-backup API (safe while app is running).  Returns backup path
    on success or None on failure / non-SQLite environments."""
    from db import DATABASE_URL, SQLITE_PATH
    if DATABASE_URL:
        return None  # PostgreSQL — backup is the responsibility of the managed DB service
    import sqlite3 as _sq
    try:
        ts = time.strftime("%Y%m%d_%H%M%S")
        os.makedirs(backup_dir, exist_ok=True)
        dest = os.path.join(backup_dir, f"doccompare_{ts}.db")
        src = _sq.connect(SQLITE_PATH, timeout=5)
        dst = _sq.connect(dest)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        # Keep only the 7 most recent backups
        backups = sorted(
            f for f in os.listdir(backup_dir) if f.startswith("doccompare_") and f.endswith(".db")
        )
        for old in backups[:-7]:
            try: os.remove(os.path.join(backup_dir, old))
            except OSError: pass
        logger.info("SQLite backup created: %s", dest)
        return dest
    except Exception as exc:
        logger.warning("SQLite backup failed: %s", exc)
        return None


def _auto_backup_loop():
    """Daemon thread: run a daily SQLite backup."""
    backup_dir = os.environ.get("BACKUP_DIR", os.path.join(os.path.dirname(__file__) or ".", "backups"))
    interval = int(os.environ.get("BACKUP_INTERVAL_HOURS", "24")) * 3600
    # Run one backup immediately at startup (avoids 24h gap on first deploy)
    time.sleep(30)  # small delay so DB is fully initialized
    _backup_sqlite_once(backup_dir)
    while True:
        time.sleep(interval)
        _backup_sqlite_once(backup_dir)


_backup_thread = threading.Thread(target=_auto_backup_loop, daemon=True)
_backup_thread.start()

_ALLOWED_PDF_MIMES = {"application/pdf", "application/x-pdf", "binary/octet-stream"}

# JSON body validation ─────────────────────────────────────────────────────────

def _validate_json_body(data: dict, schema: dict) -> str | None:
    """Lightweight JSON body validator.

    schema example:
        {"username": (str, True), "role": (str, False), "is_active": (bool, False)}
    Returns an error string, or None if valid.
    """
    if data is None:
        return "Wymagane dane JSON w treści żądania"
    for field, (ftype, required) in schema.items():
        if field not in data:
            if required:
                return f"Wymagane pole: '{field}'"
        else:
            val = data[field]
            if val is not None and not isinstance(val, ftype):
                # Accept int where float expected
                if ftype is float and isinstance(val, int):
                    continue
                type_name = ftype.__name__ if hasattr(ftype, "__name__") else str(ftype)
                return f"Pole '{field}' musi być typu {type_name}"
    return None
def _validate_pdf_upload(f) -> str | None:
    """Return error string if file is not a valid PDF upload, else None."""
    if not f or not f.filename:
        return "Brak pliku"
    if not f.filename.lower().endswith(".pdf"):
        return f"'{f.filename}' nie jest plikiem PDF"
    ct = (f.content_type or "").lower().split(";")[0].strip()
    if ct and ct not in _ALLOWED_PDF_MIMES:
        return f"Nieprawidłowy typ MIME: {ct}"
    # Validate PDF magic bytes (%PDF header) and check for encryption
    try:
        f.stream.seek(0)
        header = f.stream.read(4)
        if not header or header != b"%PDF":
            f.stream.seek(0)
            return "Plik nie jest prawidłowym dokumentem PDF"
        # Scan PDF tail for /Encrypt entry (present in all password-protected PDFs)
        f.stream.seek(0, 2)
        file_size = f.stream.tell()
        tail_size = min(4096, file_size)
        f.stream.seek(max(0, file_size - tail_size))
        tail = f.stream.read(tail_size)
        f.stream.seek(0)
        if b"/Encrypt" in tail:
            # /Encrypt ≠ „hasło do otwarcia". Bardzo częsty przypadek: puste hasło
            # użytkownika + tylko ograniczenia właściciela (np. zakaz kopiowania) —
            # plik otwiera się normalnie. Odrzucamy WYŁĄCZNIE, gdy realnie wymaga
            # hasła do otwarcia (needs_pass). Inaczej przepuszczamy (odszyfrujemy
            # pustym hasłem przy zapisie — patrz _decrypt_pdf_inplace).
            try:
                import fitz
                f.stream.seek(0)
                _data = f.stream.read()
                f.stream.seek(0)
                _doc = fitz.open(stream=_data, filetype="pdf")
                _needs = bool(_doc.needs_pass)
                _doc.close()
                if _needs:
                    return "Plik PDF wymaga hasła do otwarcia — prześlij wersję bez hasła."
            except Exception as _ce:
                logger.warning("PDF encrypt check for '%s' failed: %s", f.filename, _ce)
                return "Plik PDF jest chroniony hasłem — prześlij wersję bez zabezpieczeń"
    except Exception as _e:
        logger.warning("PDF validation stream error for '%s': %s", f.filename, _e)
        return "Błąd odczytu pliku — prześlij ponownie"
    return None


def _decrypt_pdf_inplace(path: str) -> None:
    """Jeśli PDF jest zaszyfrowany PUSTYM hasłem użytkownika (tylko ograniczenia
    właściciela), zapisz w to samo miejsce odszyfrowaną kopię — by wszystkie
    czytniki (pdfplumber/pdfminer/camelot) działały. Pliki niezaszyfrowane lub
    wymagające realnego hasła pozostają nietknięte."""
    try:
        import fitz
        doc = fitz.open(path)
        try:
            if not getattr(doc, "is_encrypted", False) or doc.needs_pass:
                return  # niezaszyfrowany albo wymaga realnego hasła → nie ruszamy
            tmp = path + ".dec.pdf"
            doc.save(tmp, encryption=fitz.PDF_ENCRYPT_NONE)
        finally:
            doc.close()
        try:
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try: os.remove(tmp)
                except OSError: pass
    except Exception as _e:
        logger.debug("PDF decrypt skip for %s: %s", path, _e)

# role_level / can_see_all / can_delete / is_admin → core.security (import na górze)


# ─────────────────────────────────────────────────────────────────────────────
# BAZA DANYCH
# ─────────────────────────────────────────────────────────────────────────────


def _db_run_migrations(db):
    # Migracje
    for migration in [
        "ALTER TABLE users ADD COLUMN last_login TEXT",
        "ALTER TABLE users ADD COLUMN email TEXT",
        "ALTER TABLE users ADD COLUMN is_active INTEGER DEFAULT 1",
        """CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT UNIQUE NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )""",
        "ALTER TABLE suppliers ADD COLUMN column_mapping_json TEXT DEFAULT '{}'",
        "ALTER TABLE suppliers ADD COLUMN detect_keywords_json TEXT DEFAULT '[]'",
        """CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            UNIQUE(category, key)
        )""",
        """CREATE TABLE IF NOT EXISTS suppliers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            country TEXT DEFAULT 'XX',
            currency TEXT DEFAULT 'USD',
            language TEXT DEFAULT 'EN',
            price_rounding INTEGER DEFAULT 4,
            po_rounding INTEGER DEFAULT 2,
            payment_terms_json TEXT DEFAULT '{}',
            date_format TEXT DEFAULT '%Y/%m/%d',
            known_issues_json TEXT DEFAULT '[]',
            price_tolerance_pct REAL DEFAULT 0.5,
            qty_tolerance_pct REAL DEFAULT 0.0,
            detect_keywords_json TEXT DEFAULT '[]',
            notes TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        )""",
        """CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL,
            username TEXT,
            detail TEXT DEFAULT '',
            ip TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        )""",
        "ALTER TABLE audit_log ADD COLUMN duration_ms INTEGER DEFAULT NULL",
        "ALTER TABLE audit_log ADD COLUMN extra TEXT DEFAULT NULL",
        "CREATE INDEX IF NOT EXISTS idx_audit_ip_event ON audit_log(ip, event, created_at)",
        # Lokalna tabela referencyjna taryfy CN/TARIC (import nomenklatury UE)
        """CREATE TABLE IF NOT EXISTS cn_reference (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            description TEXT DEFAULT '',
            duty_rate TEXT DEFAULT '',
            restrictions TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            updated_at TEXT DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_cn_reference_code ON cn_reference(code)",
        # comparisons — dodatkowe kolumny (migrate_db.py sync)
        "ALTER TABLE comparisons ADD COLUMN po_number TEXT DEFAULT ''",
        "ALTER TABLE comparisons ADD COLUMN supplier_code TEXT DEFAULT ''",
        "ALTER TABLE comparisons ADD COLUMN total_a TEXT DEFAULT ''",
        "ALTER TABLE comparisons ADD COLUMN total_b TEXT DEFAULT ''",
        "ALTER TABLE comparisons ADD COLUMN comment TEXT DEFAULT ''",
        "ALTER TABLE comparisons ADD COLUMN approval_status TEXT DEFAULT 'pending'",
        "ALTER TABLE comparisons ADD COLUMN approved_by INTEGER",
        "ALTER TABLE comparisons ADD COLUMN approved_at TEXT",
        "ALTER TABLE comparisons ADD COLUMN is_public INTEGER DEFAULT 0",
        # users — dodatkowe kolumny
        "ALTER TABLE users ADD COLUMN is_active INTEGER DEFAULT 1",
        # suppliers — dodatkowe kolumny
        "ALTER TABLE suppliers ADD COLUMN synonyms_json TEXT DEFAULT '[]'",
        "ALTER TABLE suppliers ADD COLUMN pi_column_mapping_json TEXT DEFAULT '{}'",
        "ALTER TABLE suppliers ADD COLUMN po_column_mapping_json TEXT DEFAULT '{}'",
        "ALTER TABLE suppliers ADD COLUMN ref_format_pi TEXT DEFAULT 'standard'",
        "ALTER TABLE suppliers ADD COLUMN ref_format_po TEXT DEFAULT 'standard'",
        "ALTER TABLE suppliers ADD COLUMN wizard_completed INTEGER DEFAULT 0",
        "ALTER TABLE suppliers ADD COLUMN profile_updated_at TEXT",
        "ALTER TABLE suppliers ADD COLUMN profile_updated_by TEXT",
        "ALTER TABLE suppliers ADD COLUMN profile_version INTEGER DEFAULT 1",
        "ALTER TABLE suppliers ADD COLUMN header_patterns_json TEXT DEFAULT '{}'",
        "ALTER TABLE suppliers ADD COLUMN ignore_fields_json TEXT DEFAULT '[]'",
        "ALTER TABLE suppliers ADD COLUMN custom_rules_json TEXT DEFAULT '[]'",
        """CREATE TABLE IF NOT EXISTS artwork_batch_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            status TEXT DEFAULT 'pending' CHECK(status IN ('pending','running','done','failed','cancelled')),
            total INTEGER DEFAULT 0 CHECK(total >= 0),
            done INTEGER DEFAULT 0 CHECK(done >= 0),
            failed INTEGER DEFAULT 0 CHECK(failed >= 0),
            pairs_json TEXT DEFAULT '[]',
            results_json TEXT DEFAULT '[]',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )""",
        """CREATE TABLE IF NOT EXISTS api_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT (datetime('now')),
            user_id INTEGER,
            model TEXT,
            call_type TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0
        )""",
        "CREATE INDEX IF NOT EXISTS idx_comparisons_user_created ON comparisons(user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_comparisons_status ON comparisons(status)",
        "ALTER TABLE comparisons ADD COLUMN file_hash_a TEXT",
        "ALTER TABLE comparisons ADD COLUMN file_hash_b TEXT",
        "CREATE INDEX IF NOT EXISTS idx_comparisons_hashes ON comparisons(file_hash_a, file_hash_b)",
        """CREATE TABLE IF NOT EXISTS comparison_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            page_type TEXT NOT NULL DEFAULT 'compare',
            is_public INTEGER DEFAULT 0,
            supplier_code TEXT DEFAULT '',
            doc_type_a TEXT DEFAULT '',
            doc_type_b TEXT DEFAULT '',
            price_tolerance_pct REAL DEFAULT 0.5,
            qty_tolerance_pct REAL DEFAULT 0.0,
            ai_model TEXT DEFAULT 'claude-sonnet-4-6',
            use_ai INTEGER DEFAULT 1,
            column_mapping_json TEXT DEFAULT '{}',
            ignore_fields_json TEXT DEFAULT '[]',
            use_count INTEGER DEFAULT 0,
            created_by INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(created_by) REFERENCES users(id)
        )""",
        "ALTER TABLE suppliers ADD COLUMN sad_column_mapping_json TEXT DEFAULT '{}'",
        """CREATE TABLE IF NOT EXISTS library_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rel_path TEXT NOT NULL UNIQUE,
            filename TEXT NOT NULL,
            size_bytes INTEGER DEFAULT 0,
            modified_at TEXT,
            synced_at TEXT DEFAULT (datetime('now')),
            checksum TEXT,
            lvl1 TEXT DEFAULT '',
            lvl2 TEXT DEFAULT '',
            lvl3 TEXT DEFAULT '',
            lvl4 TEXT DEFAULT '',
            lvl5 TEXT DEFAULT '',
            file_path TEXT DEFAULT '',
            thumb_data BLOB,
            z_path TEXT DEFAULT ''
        )""",
        "CREATE INDEX IF NOT EXISTS idx_library_rel_path ON library_files(rel_path)",
        "CREATE INDEX IF NOT EXISTS idx_library_lvl1 ON library_files(lvl1)",
        # Nawigacja w głąb drzewa filtruje po kolejnych poziomach (WHERE lvl1=? AND
        # lvl2=? GROUP BY lvl3) — sam indeks na lvl1 nie wystarczał i każde kliknięcie
        # folderu skanowało całą tabelę.
        "CREATE INDEX IF NOT EXISTS idx_library_lvl123 ON library_files(lvl1, lvl2, lvl3)",
        "ALTER TABLE library_files ADD COLUMN thumb_data BLOB",
        "ALTER TABLE library_files ADD COLUMN z_path TEXT DEFAULT ''",
        """CREATE TABLE IF NOT EXISTS ref_database (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ref_code TEXT NOT NULL,
            product_name TEXT NOT NULL,
            uploaded_by INTEGER,
            uploaded_at TEXT DEFAULT (datetime('now')),
            UNIQUE(ref_code)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_ref_database_code ON ref_database(ref_code)",
        # Soft-delete support for comparisons
        "ALTER TABLE comparisons ADD COLUMN is_deleted INTEGER DEFAULT 0",
        "ALTER TABLE comparisons ADD COLUMN deleted_at TEXT",
        "ALTER TABLE comparisons ADD COLUMN deleted_by INTEGER",
        # Approval workflow
        "ALTER TABLE comparisons ADD COLUMN approval_status TEXT DEFAULT NULL",
        "ALTER TABLE comparisons ADD COLUMN approved_by INTEGER",
        "ALTER TABLE comparisons ADD COLUMN approved_at TEXT",
        "ALTER TABLE comparisons ADD COLUMN approval_note TEXT DEFAULT ''",
        "CREATE INDEX IF NOT EXISTS idx_comparisons_not_deleted ON comparisons(is_deleted, created_at DESC)",
        # Webhooks table
        """CREATE TABLE IF NOT EXISTS webhooks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL CHECK(url LIKE 'http://%' OR url LIKE 'https://%'),
            secret TEXT DEFAULT '',
            events TEXT DEFAULT 'comparison.completed',
            is_active INTEGER DEFAULT 1 CHECK(is_active IN (0,1)),
            created_by INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        )""",
        # Artwork profile version history
        """CREATE TABLE IF NOT EXISTS artwork_profile_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL,
            action TEXT NOT NULL DEFAULT 'update',
            changed_by INTEGER,
            changed_by_name TEXT DEFAULT '',
            snapshot_json TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(profile_id) REFERENCES artwork_profiles(id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_artwork_profile_history_pid ON artwork_profile_history(profile_id, created_at DESC)",
        # Artwork profile indexes
        "CREATE INDEX IF NOT EXISTS idx_artwork_profiles_ean ON artwork_profiles(ean)",
        "CREATE INDEX IF NOT EXISTS idx_artwork_profiles_ref ON artwork_profiles(ref_code)",
        "CREATE INDEX IF NOT EXISTS idx_artwork_profiles_active ON artwork_profiles(is_active)",
        "CREATE INDEX IF NOT EXISTS idx_artwork_profile_fields_pid ON artwork_profile_fields(profile_id)",
        # API usage index
        "CREATE INDEX IF NOT EXISTS idx_api_usage_user_created ON api_usage(user_id, created_at DESC)",
        # Artwork field comments — per-field annotations on comparison reports
        """CREATE TABLE IF NOT EXISTS artwork_field_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            comparison_id INTEGER NOT NULL,
            field_name TEXT NOT NULL,
            comment TEXT NOT NULL DEFAULT '',
            created_by INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(comparison_id, field_name),
            FOREIGN KEY(comparison_id) REFERENCES comparisons(id)
        )""",
        """CREATE TABLE IF NOT EXISTS translation_dictionary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            term_original TEXT NOT NULL,
            lang_from TEXT NOT NULL DEFAULT 'en',
            term_translated TEXT NOT NULL,
            lang_to TEXT NOT NULL DEFAULT 'pl',
            context TEXT NOT NULL DEFAULT 'all',
            notes TEXT DEFAULT '',
            created_by INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(term_original, lang_from, lang_to, context)
        )""",
        """CREATE TABLE IF NOT EXISTS sad_field_mapping (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sad_field_code TEXT NOT NULL,
            sad_field_name_pl TEXT NOT NULL,
            sad_field_name_en TEXT DEFAULT '',
            mapped_to_field TEXT DEFAULT '',
            mapped_to_doctype TEXT DEFAULT 'CI',
            data_type TEXT DEFAULT 'text',
            description TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(sad_field_code)
        )""",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('1', 'Deklaracja / typ', 'Declaration / type', 'doc_type', 'CI', 'text', 'Pole 1 SAD: typ deklaracji')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('2', 'Nadawca / eksporter', 'Sender / exporter', 'seller_name', 'CI', 'text', 'Pole 2 SAD: nazwa i adres eksportera')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('3', 'Formularze', 'Forms', '', '', 'text', 'Pole 3 SAD: liczba formularzy')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('4', 'Wykazy', 'Lists', '', '', 'text', 'Pole 4 SAD: wykazy załadunkowe')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('5', 'Pozycje', 'Items', '', '', 'number', 'Pole 5 SAD: łączna liczba pozycji')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('6', 'Opakowania', 'Packages', 'total_packages', 'PL', 'number', 'Pole 6 SAD: łączna liczba opakowań')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('7', 'Nr referencyjny', 'Reference number', '', '', 'text', 'Pole 7 SAD: numer referencyjny')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('8', 'Odbiorca', 'Consignee', 'buyer_name', 'CI', 'text', 'Pole 8 SAD: nazwa i adres odbiorcy')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('11', 'Kraj handlujący', 'Trading country', 'origin_country', 'CI', 'text', 'Pole 11 SAD: kraj handlujący')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('12', 'Wartość celna', 'Customs value', 'total_net', 'CI', 'number', 'Pole 12 SAD: wartość celna')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('14', 'Zgłaszający/przedstawiciel', 'Declarant', '', '', 'text', 'Pole 14 SAD: zgłaszający')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('15', 'Kraj wysyłki', 'Country of dispatch', '', '', 'text', 'Pole 15 SAD: kraj wysyłki')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('17', 'Kraj przeznaczenia', 'Country of destination', '', '', 'text', 'Pole 17 SAD: kraj przeznaczenia')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('18', 'Środek transportu', 'Transport means', '', '', 'text', 'Pole 18 SAD: środek transportu przy wyjściu')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('20', 'Warunki dostawy', 'Delivery terms', 'incoterms', 'CI', 'text', 'Pole 20 SAD: warunki dostawy (Incoterms)')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('21', 'Aktywny środek transportu', 'Active transport', '', '', 'text', 'Pole 21 SAD: aktywny środek transportu')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('22', 'Waluta i wartość', 'Currency and value', 'currency', 'CI', 'text', 'Pole 22 SAD: waluta i wartość faktury')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('23', 'Kurs wymiany', 'Exchange rate', '', '', 'number', 'Pole 23 SAD: kurs wymiany waluty')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('24', 'Rodzaj transakcji', 'Transaction type', '', '', 'text', 'Pole 24 SAD: rodzaj transakcji')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('25', 'Rodzaj transportu', 'Transport mode', '', '', 'text', 'Pole 25 SAD: rodzaj transportu na granicy')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('26', 'Wewnętrzny rodzaj transportu', 'Inland transport mode', '', '', 'text', 'Pole 26 SAD: wewnętrzny rodzaj transportu')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('28', 'Rachunek finansowy', 'Financial account', '', '', 'text', 'Pole 28 SAD: rachunek finansowy')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('29', 'Urząd wyjścia', 'Exit office', '', '', 'text', 'Pole 29 SAD: urząd celny wyjścia')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('30', 'Lokalizacja towaru', 'Location of goods', '', '', 'text', 'Pole 30 SAD: lokalizacja towarów')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('31', 'Opakowania i opis', 'Packages and description', 'description', 'CI', 'text', 'Pole 31 SAD: opakowania i opis towaru')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('32', 'Nr pozycji', 'Item number', 'ref', 'CI', 'text', 'Pole 32 SAD: numer pozycji')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('33', 'Kod towaru', 'Commodity code', 'tariff_code', 'CI', 'code', 'Pole 33 SAD: kod taryfy celnej CN')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('34', 'Kod kraju', 'Country code', 'origin_country', 'CI', 'text', 'Pole 34 SAD: kod kraju pochodzenia')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('35', 'Masa brutto', 'Gross mass', 'gross_weight', 'PL', 'number', 'Pole 35 SAD: masa brutto (kg)')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('36', 'Preferencja', 'Preference', '', '', 'text', 'Pole 36 SAD: preferencja celna')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('37', 'Procedura', 'Procedure', '', '', 'text', 'Pole 37 SAD: procedura celna')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('38', 'Masa netto', 'Net mass', 'net_weight', 'PL', 'number', 'Pole 38 SAD: masa netto (kg)')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('39', 'Kontyngent', 'Quota', '', '', 'text', 'Pole 39 SAD: kontyngent')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('40', 'Deklaracja skrócona', 'Summary declaration', 'po_number', 'PO', 'text', 'Pole 40 SAD: poprzedni dokument / PO')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('41', 'Uzupełniające jednostki', 'Supplementary units', 'qty', 'CI', 'number', 'Pole 41 SAD: uzupełniające jednostki miary')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('42', 'Cena pozycji', 'Item price', 'price', 'CI', 'number', 'Pole 42 SAD: cena pozycji')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('43', 'Metoda wartości', 'Valuation method', '', '', 'text', 'Pole 43 SAD: metoda wartości celnej')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('44', 'Dodatkowe informacje', 'Additional info', 'notes', 'CI', 'text', 'Pole 44 SAD: dodatkowe informacje / dokumenty')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('45', 'Korekta', 'Adjustment', '', '', 'number', 'Pole 45 SAD: korekta')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('46', 'Wartość statystyczna', 'Statistical value', '', '', 'number', 'Pole 46 SAD: wartość statystyczna')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('47', 'Obliczanie opłat', 'Calculation of taxes', '', '', 'text', 'Pole 47 SAD: obliczanie opłat')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('48', 'Odroczenie płatności', 'Deferred payment', '', '', 'text', 'Pole 48 SAD: odroczenie płatności')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('49', 'Magazyn celny', 'Warehouse', '', '', 'text', 'Pole 49 SAD: magazyn celny')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('50', 'Zleceniodawca', 'Principal', '', '', 'text', 'Pole 50 SAD: zleceniodawca')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('51', 'Planowane urzędy tranzytu', 'Intended offices of transit', '', '', 'text', 'Pole 51 SAD: planowane urzędy tranzytu')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('52', 'Gwarancja', 'Guarantee', '', '', 'text', 'Pole 52 SAD: gwarancja')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('53', 'Urząd przeznaczenia', 'Office of destination', '', '', 'text', 'Pole 53 SAD: urząd celny przeznaczenia')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('54', 'Miejsce i data', 'Place and date', '', '', 'text', 'Pole 54 SAD: miejsce i data, podpis')",
        # ── Dane referencyjne: kursy walut ──────────────────────────────────────
        """CREATE TABLE IF NOT EXISTS currency_rates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    currency_code TEXT NOT NULL,
    currency_name TEXT NOT NULL,
    rate REAL NOT NULL,
    rate_date TEXT NOT NULL,
    source TEXT DEFAULT 'NBP',
    fetched_at TEXT DEFAULT (datetime('now')),
    UNIQUE(currency_code, rate_date)
)""",
        "CREATE INDEX IF NOT EXISTS idx_currency_rates_date ON currency_rates(currency_code, rate_date DESC)",
        # ── Dane referencyjne: baza produktów ───────────────────────────────────
        """CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref_code TEXT NOT NULL UNIQUE,
    product_name TEXT NOT NULL,
    ean TEXT DEFAULT '',
    unit TEXT DEFAULT 'szt',
    tariff_cn TEXT DEFAULT '',
    description TEXT DEFAULT '',
    supplier_codes TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
)""",
        "CREATE INDEX IF NOT EXISTS idx_products_ref ON products(ref_code)",
        "CREATE INDEX IF NOT EXISTS idx_products_ean ON products(ean)",
        "ALTER TABLE users ADD COLUMN language TEXT DEFAULT 'pl'",
        "ALTER TABLE users ADD COLUMN department TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN allowed_modules TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN phone TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN avatar_initials TEXT DEFAULT ''",
        """CREATE TABLE IF NOT EXISTS supplier_checklists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER,
    supplier_name TEXT NOT NULL DEFAULT '',
    doc_type TEXT NOT NULL,
    required INTEGER DEFAULT 1,
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
)""",
        """CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL DEFAULT 'system',
            title TEXT NOT NULL,
            message TEXT DEFAULT '',
            link TEXT DEFAULT '',
            is_read INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, is_read, created_at DESC)",
        """CREATE TABLE IF NOT EXISTS job_progress (
    job_id TEXT PRIMARY KEY,
    user_id INTEGER,
    status TEXT DEFAULT 'pending',
    step TEXT DEFAULT '',
    progress INTEGER DEFAULT 0,
    current_page INTEGER DEFAULT 0,
    total_pages INTEGER DEFAULT 0,
    items_found INTEGER DEFAULT 0,
    message TEXT DEFAULT '',
    result_json TEXT,
    error TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
)""",
        """CREATE TABLE IF NOT EXISTS comparison_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    comparison_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    comment TEXT NOT NULL,
    comment_type TEXT DEFAULT 'note',
    created_at TEXT DEFAULT (datetime('now'))
)""",
        "CREATE INDEX IF NOT EXISTS idx_comments_cid ON comparison_comments(comparison_id)",
    ]:
        try:
            db.execute(migration)
            db.commit()
        except Exception as _mig_err:
            db.rollback()
            _mig_msg = str(_mig_err).lower()
            if "already exists" in _mig_msg or "duplicate column" in _mig_msg:
                pass  # idempotent — column/table already present
            else:
                logger.warning("Migration failed (schema may be incomplete): %s", _mig_err)


def _db_track_schema_version(db):
    # ── Schema version tracking (#48) ──────────────────────────────────────────
    # Record the current schema version as a SHA-1 digest of all migration SQL.
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT DEFAULT (datetime('now')),
            description TEXT DEFAULT ''
        )""")
        db.commit()
        import hashlib as _hl48
        # Use a digest of the init script + migration count as version string
        _payload = "doccompare-v6-migrations-rev20260530"
        _ver = _hl48.sha1(_payload.encode(), usedforsecurity=False).hexdigest()[:12]
        db.execute(
            "INSERT OR IGNORE INTO schema_migrations(version) VALUES(?)", (_ver,)
        )
        db.commit()
    except Exception:
        pass


def _db_seed_defaults(db):
    # Domyślne typy dokumentów
    defaults = [
        ('auto',  '❓', 'Wykryj automatycznie',          'Silnik sam rozpozna typ dokumentu', 1, 0),
        ('PO',    '📋', 'Purchase Order (PO)',             'Zamówienie zakupu — SAP, zewnętrzne', 1, 1),
        ('PI',    '📄', 'Proforma Invoice (PI)',           'Faktura proforma od dostawcy', 1, 2),
        ('CI',    '🧾', 'Commercial Invoice (CI)',         'Faktura handlowa eksportowa', 1, 3),
        ('PL',    '📦', 'Packing List',                   'Lista pakowania', 1, 4),
        ('SAD',   '🛃', 'SAD / ZC415',                    'Zgłoszenie celne importowe (WinSAD)', 1, 5),
        ('BL',    '🚢', 'Bill of Lading',                 'Konosament morski', 1, 6),
        ('WZ',    '📑', 'WZ (wydanie zewnętrzne)',         'Dokument magazynowy WZ', 1, 7),
        ('FV',    '🔖', 'Faktura VAT (polska)',            'Polska faktura VAT', 1, 8),
        ('MULTI', '🔀', 'Zestaw faktur (multi-CI)',        'Wiele CI w jednym PDF', 1, 9),
        ('CMR',   '🚛', 'CMR (list przewozowy)',           'Międzynarodowy list przewozowy', 1, 10),
        ('SWIFT', '💳', 'Potwierdzenie SWIFT',             'Potwierdzenie przelewu bankowego', 0, 11),
    ]
    for code, icon, label, desc, active, order in defaults:
        db.execute(
            'INSERT INTO doc_types(code,icon,label,description,active,sort_order) VALUES(?,?,?,?,?,?) ON CONFLICT(code) DO NOTHING',
            (code, icon, label, desc, active, order)
        )

    if not db.execute("SELECT id FROM users WHERE username='admin'").fetchone():
        import secrets as _sec
        _rand_pw = _sec.token_urlsafe(16)
        # System jednoosobowy — seedujemy TYLKO konto admin (bez kont-widm).
        db.execute(
            "INSERT INTO users(username, password_hash, role) VALUES (?, ?, ?)",
            ("admin", generate_password_hash(_rand_pw), "admin")
        )
        # Save generated admin password to a local file (not just stdout)
        try:
            _pw_file = os.path.join(os.path.dirname(__file__) or ".", "INITIAL_ADMIN_PASSWORD.txt")
            with open(_pw_file, "w") as _pf:
                _pf.write(f"admin: {_rand_pw}\n")
            os.chmod(_pw_file, 0o600)
            logger.warning("First-run: admin password saved to %s — delete after first login!", _pw_file)
        except Exception:
            pass
        print(f"\n{'='*60}")
        print(f"  NOWA INSTALACJA — hasło administratora: {_rand_pw}")
        print(f"  Zaloguj się jako 'admin' i natychmiast zmień hasło!")
        print(f"{'='*60}\n")
        logger.warning("First-run: default users created with random passwords.")
    db.commit()


def _db_ensure_extra_columns(db):
    # Explicit column-presence migrations — safe to re-run on every startup
    _missing_cols = [
        ("library_files", "thumb_data", "BYTEA"),
        ("library_files", "z_path",     "TEXT DEFAULT ''"),
        ("library_files", "file_path",  "TEXT DEFAULT ''"),
    ]
    # Defence-in-depth: although _missing_cols is a hardcoded controlled list,
    # validate identifiers before interpolating them into PRAGMA/ALTER (which
    # cannot be parameterised), so a future refactor can't introduce injection.
    _IDENT_RE  = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    _COLDEF_RE = re.compile(r"^[A-Za-z0-9_'()., ]+$")
    for _tbl, _col, _def in _missing_cols:
        if not (_IDENT_RE.match(_tbl) and _IDENT_RE.match(_col) and _COLDEF_RE.match(_def)):
            logger.warning("Pomijam niebezpieczną migrację kolumny: %r %r %r", _tbl, _col, _def)
            continue
        try:
            if os.environ.get("DATABASE_URL"):  # PostgreSQL
                _exists = db.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name=? AND column_name=?", (_tbl, _col)
                ).fetchone()
            else:  # SQLite
                _exists = None
                for _r in db.execute(f"PRAGMA table_info({_tbl})").fetchall():
                    if _r[1] == _col:
                        _exists = _r
                        break
            if not _exists:
                db.execute(f"ALTER TABLE {_tbl} ADD COLUMN {_col} {_def}")
                db.commit()
        except Exception:
            db.rollback()


def init_db():
    os.makedirs("instance", exist_ok=True)
    os.makedirs("uploads", exist_ok=True)
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS comparisons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            doc_type TEXT,
            file_a TEXT,
            file_b TEXT,
            result_json TEXT,
            status TEXT DEFAULT 'ok',
            diff_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL,
            username TEXT,
            detail TEXT DEFAULT '',
            ip TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS doc_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            label TEXT NOT NULL,
            icon TEXT NOT NULL DEFAULT '📄',
            description TEXT DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 99
        );
    """)
    _db_run_migrations(db)
    _db_track_schema_version(db)
    _db_seed_defaults(db)
    _db_ensure_extra_columns(db)
    db.close()


# Run on every startup (python app.py AND gunicorn) — safe with CREATE IF NOT EXISTS
try:
    init_db()
    try:
        from supplier_profiles import init_suppliers_table
        init_suppliers_table()
    except Exception:
        pass
except Exception as _e:
    logger.critical("init_db() failed at startup — DB schema may be incomplete: %s", _e)

# Załaduj ANTHROPIC_API_KEY z bazy danych jeśli nie ma w środowisku
if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
    try:
        _db = get_db()
        try:
            _row = _db.execute(
                "SELECT value FROM settings WHERE category='ai' AND key='anthropic_api_key'"
            ).fetchone()
        finally:
            _db.close()
        if _row and _row[0]:
            os.environ["ANTHROPIC_API_KEY"] = _row[0]
            logger.info("ANTHROPIC_API_KEY loaded from database settings")
    except Exception as _e:
        logger.warning(f"Could not load API key from DB: {_e}")


# ─────────────────────────────────────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────────────────────────────────────

# _check_session_active / login_required / require_role → core.security (import na górze)


def _check_rate_limit(ip: str) -> bool:
    """Return True if IP has too many recent failed login attempts.

    Queries audit_log so the limit survives server restarts and works across
    multiple worker processes.  Falls back to the in-memory dict on DB errors.
    """
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S",
                           time.gmtime(time.time() - _RATE_LIMIT_WINDOW))
    try:
        db = get_db()
        try:
            count = db.execute(
                "SELECT COUNT(*) FROM audit_log WHERE event='login_fail' AND ip=? AND created_at > ?",
                (ip, cutoff)
            ).fetchone()[0]
            return count >= _RATE_LIMIT_MAX
        finally:
            db.close()
    except Exception:
        now = time.time()
        with _login_attempts_lock:
            attempts = _login_attempts.get(ip, [])
            attempts = [t for t in attempts if now - t < _RATE_LIMIT_WINDOW]
            _login_attempts[ip] = attempts
            return len(attempts) >= _RATE_LIMIT_MAX


# ── CSRF protection → core.security (_get_csrf_token / _verify_csrf / csrf_protect) ──

@app.context_processor
def _inject_csrf():
    """Make csrf_token() available in every Jinja2 template."""
    return {"csrf_token": _get_csrf_token}


@app.context_processor
def inject_i18n():
    lang = session.get("lang", "pl")
    def _t(key):
        return _t_func(key, lang)
    return {"_t": _t, "_lang": lang}


@app.after_request
def apply_en_translation(response):
    """Post-process HTML responses: replace Polish text with English when lang=en."""
    if (session.get("lang", "pl") == "en"
            and response.status_code == 200
            and "text/html" in response.content_type):
        try:
            html = response.get_data(as_text=True)
            html = _translate_html_en(html)
            response.set_data(html)
        except Exception:
            pass  # Never break the response on translation errors
    return response


def _clear_login_attempts(ip: str):
    with _login_attempts_lock:
        _login_attempts.pop(ip, None)


def _check_forgot_limit(ip: str) -> bool:
    """Return True if IP has exceeded the password-reset request rate limit.

    Uses audit_log so it persists across restarts.  Falls back to in-memory.
    """
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S",
                           time.gmtime(time.time() - _FORGOT_LIMIT_WINDOW))
    try:
        db = get_db()
        try:
            count = db.execute(
                "SELECT COUNT(*) FROM audit_log WHERE event='forgot_request' AND ip=? AND created_at > ?",
                (ip, cutoff)
            ).fetchone()[0]
            return count >= _FORGOT_LIMIT_MAX
        finally:
            db.close()
    except Exception:
        now = time.time()
        with _forgot_attempts_lock:
            attempts = _forgot_attempts.get(ip, [])
            attempts = [t for t in attempts if now - t < _FORGOT_LIMIT_WINDOW]
            _forgot_attempts[ip] = attempts
            return len(attempts) >= _FORGOT_LIMIT_MAX


def _record_forgot_attempt(ip: str):
    now = time.time()
    with _forgot_attempts_lock:
        attempts = _forgot_attempts.get(ip, [])
        attempts.append(now)
        _forgot_attempts[ip] = attempts[-(_FORGOT_LIMIT_MAX + 5):]


# _log_audit → core.audit (import na górze)


# ─────────────────────────────────────────────────────────────────────────────
# BEFORE REQUEST — cross-cutting concerns
# ─────────────────────────────────────────────────────────────────────────────

@app.before_request
def _before_request():
    """Set Sentry user context for every authenticated request."""
    if _sentry_dsn and "user_id" in session:
        try:
            import sentry_sdk
            sentry_sdk.set_user({
                "id": session.get("user_id"),
                "username": session.get("username"),
                "ip_address": request.remote_addr,
            })
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES — STRONY
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    # Public liveness probe — intentionally minimal to avoid leaking business
    # metrics or infrastructure details to unauthenticated callers.
    details: dict = {}
    overall = "ok"
    # DB reachability (no row counts — comparisons_total is business-sensitive)
    try:
        db = get_db()
        try:
            db.execute("SELECT 1")
            details["db"] = "ok"
        finally:
            db.close()
    except Exception as e:
        details["db"] = "error"
        overall = "degraded"
    # Disk space warning only (no exact free-space number)
    try:
        import shutil as _shutil
        _total, _used, _free = _shutil.disk_usage(".")
        if _free < 200 * 1024 * 1024:
            details["disk_warning"] = "Low disk space"
            overall = "degraded"
    except Exception:
        pass
    return jsonify({"status": overall, **details}), 200 if overall == "ok" else 503


@app.route("/api/admin/backup", methods=["POST"])
@require_role("admin")
@csrf_protect
def api_admin_backup():
    """Trigger an immediate SQLite backup. No-op for PostgreSQL environments."""
    backup_dir = os.environ.get("BACKUP_DIR", os.path.join(os.path.dirname(__file__) or ".", "backups"))
    dest = _backup_sqlite_once(backup_dir)
    if dest is None:
        from db import DATABASE_URL as _dbu
        if _dbu:
            return jsonify({"ok": True, "message": "Backup zarządzany przez serwis PostgreSQL — nie wymagany ręczny backup"}), 200
        return jsonify({"error": "Backup nie powiódł się — sprawdź logi"}), 500
    return jsonify({"ok": True, "path": os.path.basename(dest)})


@app.route("/api/admin/schema-versions")
@require_role("admin")
def api_admin_schema_versions():
    """Return the list of recorded schema migration version hashes."""
    db = get_db()
    try:
        rows = db.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY applied_at DESC"
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception:
        return jsonify([])
    finally:
        db.close()


@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("artwork_page"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
@csrf_protect
def login():
    if "user_id" in session:
        return redirect(url_for("index"))
    error = None
    ip = request.remote_addr or "unknown"

    if request.method == "POST":
        if _check_rate_limit(ip):
            remaining = _RATE_LIMIT_WINDOW // 60
            return render_template("login.html",
                error=f"Zbyt wiele prób. Spróbuj za {remaining} min."), 429

        identifier = request.form.get("identifier", "").strip()[:254]
        password   = request.form.get("password", "")
        if len(password) > 1000:
            error = "Nieprawidłowy email/login lub hasło."
            return render_template("login.html", error=error)
        db = get_db()
        login_ok = False
        login_user = None
        try:
            user = db.execute(
                """SELECT * FROM users
                   WHERE (LOWER(email)=LOWER(?) OR LOWER(username)=LOWER(?))
                   AND COALESCE(is_active,1)=1""",
                (identifier, identifier)
            ).fetchone()
            # Always run a hash check regardless of whether the user exists to prevent
            # user enumeration via response-time analysis.
            _dummy = "pbkdf2:sha256:260000$x$" + "a" * 64
            _pw_ok = check_password_hash(user["password_hash"] if user else _dummy, password)
            if user and _pw_ok:
                _clear_login_attempts(ip)
                session.clear()
                session.permanent = True
                session["user_id"]  = user["id"]
                session["username"] = user["username"]
                session["role"]     = user["role"]
                _u = dict(user)   # sqlite3.Row nie ma .get(); dict() działa na obu backendach
                session["department"] = _u.get("department") or ""
                session["lang"]     = _u.get("language", "pl") or "pl"
                db.execute("UPDATE users SET last_login=datetime('now') WHERE id=?", (user["id"],))
                db.commit()
                login_ok = True
                login_user = user
        finally:
            db.close()
        if login_ok:
            _log_audit("login", login_user["username"], f"IP {ip}")
            return redirect(url_for("index"))
        with _login_attempts_lock:
            _attempts = _login_attempts.get(ip, [])
            _attempts.append(time.time())
            _login_attempts[ip] = _attempts[-(_RATE_LIMIT_MAX + 5):]
            remaining_tries = max(0, _RATE_LIMIT_MAX - len(_login_attempts[ip]))
        _log_audit("login_fail", identifier, f"Błędne hasło, IP {ip}")
        error = "Nieprawidłowy email/login lub hasło."
        if remaining_tries < 3:
            error += f" Pozostało prób: {remaining_tries}"
    return render_template("login.html", error=error)


@app.route("/set-language/<lang>")
@login_required
def set_language(lang):
    if lang not in ("pl", "en"):
        lang = "pl"
    session["lang"] = lang
    uid = session.get("user_id")
    if uid:
        db = get_db()
        try:
            db.execute("UPDATE users SET language=? WHERE id=?", (lang, uid))
            db.commit()
        finally:
            db.close()
    from urllib.parse import urlparse as _urlparse
    _ref = request.referrer or ""
    _safe = (_ref and _urlparse(_ref).netloc == _urlparse(request.host_url).netloc)
    return redirect(_ref if _safe else "/artwork")


def _send_reset_email(to_email: str, username: str, reset_url: str) -> bool:
    """Send password reset email via Resend. Returns True if sent."""
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        return False
    email_from = os.environ.get(
        "EMAIL_FROM", "DocCompare <noreply@doccompare.app>"
    )
    from markupsafe import escape as _esc
    username_safe = str(_esc(username))
    html_body = (
        f"<p>Cześć <strong>{username_safe}</strong>,</p>"
        "<p>Otrzymaliśmy prośbę o reset hasła do Twojego konta DocCompare.</p>"
        f'<p><a href="{reset_url}" style="background:#5b9bff;color:#fff;padding:10px 20px;'
        f'border-radius:6px;text-decoration:none;font-weight:600">Ustaw nowe hasło →</a></p>'
        "<p style='color:#999;font-size:12px'>Link ważny 24 godziny. "
        "Jeśli nie prosiłeś o reset, zignoruj tę wiadomość.</p>"
    )
    try:
        import httpx
        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "DocCompare/1.0",
            },
            json={"from": email_from, "to": [to_email],
                  "subject": "Reset hasła — DocCompare", "html": html_body},
            timeout=15,
        )
        if resp.status_code in (200, 201):
            logger.info("Reset email sent to %s", to_email)
            return True
        logger.warning("Resend error %s: %s", resp.status_code, resp.text)
        return False
    except Exception as exc:
        logger.warning("Resend exception: %s", exc)
        return False


def _send_critical_alert(username: str, to_email: str, cid: int,
                          doc_type: str, file_a: str, file_b: str,
                          diff_count: int, po_number: str) -> None:
    """Send a non-blocking email alert when a comparison is rated critical."""
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key or not to_email:
        return
    base_url = os.environ.get("APP_BASE_URL", "").rstrip("/")
    report_url = f"{base_url}/history" if base_url else "/history"
    from markupsafe import escape as _esc
    u_safe  = str(_esc(username))
    fa_safe = str(_esc(file_a))
    fb_safe = str(_esc(file_b))
    dt_safe = str(_esc(doc_type))
    po_safe = str(_esc(po_number)) if po_number else "—"
    html_body = (
        f"<p>Cześć <strong>{u_safe}</strong>,</p>"
        f"<p>Porównanie dokumentów zakończyło się wynikiem <strong style='color:#ff5c5c'>KRYTYCZNYM</strong>.</p>"
        f"<table style='border-collapse:collapse;font-size:14px'>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#999'>Typ dokumentu</td><td><strong>{dt_safe}</strong></td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#999'>Plik A</td><td>{fa_safe}</td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#999'>Plik B</td><td>{fb_safe}</td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#999'>Nr PO</td><td>{po_safe}</td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#999'>Liczba różnic</td><td><strong>{diff_count}</strong></td></tr>"
        f"</table>"
        f"<p style='margin-top:16px'>"
        f'<a href="{report_url}" style="background:#ff5c5c;color:#fff;padding:10px 20px;'
        f'border-radius:6px;text-decoration:none;font-weight:600">Przejdź do raportu →</a></p>'
        "<p style='color:#999;font-size:12px;margin-top:16px'>DocCompare — ACME</p>"
    )
    email_from = os.environ.get("EMAIL_FROM", "DocCompare <noreply@doccompare.app>")
    def _do_send():
        try:
            import httpx
            httpx.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"from": email_from, "to": [to_email],
                      "subject": f"⚠ KRYTYCZNE porównanie #{cid} — DocCompare", "html": html_body},
                timeout=15,
            )
        except Exception as exc:
            logger.warning("Critical alert email failed: %s", exc)
    threading.Thread(target=_do_send, daemon=True).start()


@app.route("/forgot-password", methods=["GET", "POST"])
@csrf_protect
def forgot_password():
    message = None
    email_sent = False
    if request.method == "POST":
        ip = request.remote_addr or "unknown"
        if _check_forgot_limit(ip):
            return render_template("forgot_password.html",
                message="Zbyt wiele prób resetu hasła. Spróbuj ponownie za 15 minut.",
                email_sent=False), 429
        _record_forgot_attempt(ip)
        _log_audit("forgot_request", None, f"IP={ip}")
        identifier = request.form.get("identifier", "").strip()
        db = get_db()
        try:
            user = db.execute(
                "SELECT * FROM users WHERE LOWER(email)=LOWER(?) OR LOWER(username)=LOWER(?)",
                (identifier, identifier)
            ).fetchone()
            # Per-user throttle: cap reset tokens generated for one account in a
            # short window so an attacker can't email-bomb a user from rotating
            # IPs (the IP limit above only covers a single source).
            _throttled = False
            if user and user["email"]:
                _recent = db.execute(
                    "SELECT COUNT(*) FROM password_reset_tokens WHERE user_id=? "
                    "AND created_at > ?",
                    (user["id"], _ts_ago(minutes=15))
                ).fetchone()
                _throttled = bool(_recent and (_recent[0] or 0) >= 3)
            if user and user["email"] and not _throttled:
                token = secrets.token_urlsafe(32)
                # Invalidate any old unused tokens for this user to prevent token accumulation
                db.execute(
                    "UPDATE password_reset_tokens SET used=1 WHERE user_id=? AND used=0",
                    (user["id"],)
                )
                db.execute(
                    "INSERT INTO password_reset_tokens(user_id,token,expires_at) "
                    "VALUES(?,?,datetime('now','+24 hours'))",
                    (user["id"], token)
                )
                db.commit()
                _log_audit("password_reset_request", user["username"], "Token wygenerowany")
                base_url = os.environ.get("APP_BASE_URL", request.host_url.rstrip("/"))
                reset_url = f"{base_url}/reset-password?token={token}"
                email_sent = _send_reset_email(user["email"], user["username"], reset_url)
                if not email_sent:
                    logger.error("password_reset: email send failed for user_id=%s", user["id"])
        finally:
            db.close()
        # Always show a generic success message regardless of whether the account
        # exists or the email send succeeded, to prevent user enumeration.
        message = "Jeśli podany adres e-mail lub nazwa użytkownika istnieje w systemie, wyślemy na nią link do resetu hasła. Sprawdź skrzynkę (i folder spam)."
        email_sent = True
    return render_template("forgot_password.html", message=message, email_sent=email_sent)


@app.route("/reset-password", methods=["GET", "POST"])
@csrf_protect
def reset_password():
    token = request.args.get("token") or request.form.get("token", "")
    if not token:
        return redirect(url_for("login"))

    db = get_db()
    error = None
    success = False
    expired = False
    username = ""
    try:
        row = db.execute(
            """SELECT pr.*, u.username FROM password_reset_tokens pr
               JOIN users u ON u.id=pr.user_id
               WHERE pr.token=? AND pr.used=0""",
            (token,)
        ).fetchone()
        # Compare expiry in Python — avoids TEXT vs TIMESTAMPTZ operator error on PostgreSQL.
        # Truncate to [:19] so "+00:00" timezone suffix from PostgreSQL TIMESTAMPTZ is ignored.
        from datetime import datetime as _dt, timezone as _tz
        _now_str = _dt.now(_tz.utc).strftime("%Y-%m-%d %H:%M:%S")
        if row and str(row["expires_at"])[:19] <= _now_str:
            row = None

        if not row:
            expired = True
        else:
            username = row["username"]
            if request.method == "POST":
                pw  = request.form.get("password", "")
                pw2 = request.form.get("confirm", "")
                if len(pw) > 1000:
                    error = "Hasło jest za długie."
                elif len(pw) < 8:
                    error = "Hasło musi mieć co najmniej 8 znaków."
                elif pw != pw2:
                    error = "Hasła nie są zgodne."
                else:
                    # Atomically claim the token — prevents double-use race condition
                    _claim = db.execute(
                        "UPDATE password_reset_tokens SET used=1 WHERE token=? AND used=0",
                        (token,)
                    )
                    if _claim.rowcount == 0:
                        expired = True
                    else:
                        db.execute("UPDATE users SET password_hash=? WHERE id=?",
                                   (generate_password_hash(pw), row["user_id"]))
                        db.commit()
                        success = True
                        session.clear()
                        _log_audit("password_reset", row["username"], "Hasło zmienione")
    finally:
        db.close()
    if expired:
        return render_template("reset_password.html", expired=True, token=token)
    return render_template("reset_password.html", token=token, error=error,
                           success=success, username=username)


@app.route("/logout")
def logout():
    uname = session.get("username", "?")
    _log_audit("logout", uname)
    session.clear()
    return redirect(url_for("login"))


@app.route("/search")
@login_required
def global_search():
    """Globalna wyszukiwarka — z jednego pola przeszukuje porównania i dostawców.
    Read-only, parametryzowane zapytania przez get_db().

    PO/zlecenia i kontenery wypadły razem z modułem Kolejki/Transportu w forku Artwork:
    zapytania szły do nieistniejących tabel, ginęły w try/except i zwracały pustkę,
    a wyszukiwarka obiecywała w UI dane, których nie ma."""
    role = session["role"]
    uid = session["user_id"]
    q = request.args.get("q", "")[:200].strip()
    results = {"comparisons": [], "suppliers": []}
    if q:
        s_esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{s_esc}%"
        db = get_db()
        # ── Porównania dokumentów (respektuje widoczność per rola) ──
        try:
            conds = ["(c.is_deleted IS NULL OR c.is_deleted=0)",
                     "(c.file_a LIKE ? ESCAPE '\\' OR c.file_b LIKE ? ESCAPE '\\' "
                     "OR c.po_number LIKE ? ESCAPE '\\' OR c.supplier_code LIKE ? ESCAPE '\\')"]
            params = [like, like, like, like]
            if not can_see_all(role):
                conds.append("c.user_id = ?")
                params.append(uid)
            for r in db.execute(
                # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
                "SELECT c.id, c.doc_type, c.file_a, c.file_b, c.po_number, "  # nosec B608
                "c.supplier_code, c.status, c.created_at FROM comparisons c "
                "WHERE " + " AND ".join(conds) +
                " ORDER BY c.created_at DESC LIMIT 15", params).fetchall():
                results["comparisons"].append({
                    "id": r["id"], "doc_type": r["doc_type"],
                    "file_a": r["file_a"], "file_b": r["file_b"],
                    "po_number": r["po_number"], "supplier_code": r["supplier_code"],
                    "status": r["status"], "created_at": r["created_at"],
                })
        except Exception as e:
            app.logger.debug("search comparisons failed: %s", e)
        # ── Dostawcy ──
        try:
            for r in db.execute(
                "SELECT id, code, name, country FROM suppliers "
                "WHERE code LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' "
                "OR country LIKE ? ESCAPE '\\' ORDER BY name LIMIT 15",
                [like, like, like]).fetchall():
                results["suppliers"].append({
                    "code": r["code"], "name": r["name"], "country": r["country"],
                })
        except Exception as e:
            app.logger.debug("search suppliers failed: %s", e)
        finally:
            db.close()   # zwolnij połączenie z puli od razu (nie czekaj na teardown)
    total = sum(len(v) for v in results.values())
    return render_template("search.html", q=q, results=results, total=total,
                           username=session["username"], role=role)


@app.route("/history")
@login_required
def history_page():
    role = session["role"]
    uid = session["user_id"]
    # Parametry paginacji i wyszukiwania — strict validation
    try:
        page = max(1, min(10000, int(request.args.get("page", 1))))
    except (ValueError, TypeError):
        page = 1
    per_page = 20
    search = request.args.get("q", "")[:200].strip()   # cap length
    status_filter = request.args.get("status", "").strip()
    # Whitelist allowed status values to prevent injection via conditions list
    if status_filter not in ("ok", "warning", "error", "critical", "pending", ""):
        status_filter = ""
    offset = (page - 1) * per_page

    # Buduj zapytanie z filtrami
    params = []
    conditions = ["c.doc_type != 'artwork'", "(c.is_deleted IS NULL OR c.is_deleted=0)"]
    if not can_see_all(role):
        conditions.append("c.user_id = ?")
        params.append(uid)
    if search:
        conditions.append(
            "(c.file_a LIKE ? ESCAPE '\\' OR c.file_b LIKE ? ESCAPE '\\' "
            "OR c.po_number LIKE ? ESCAPE '\\' OR c.supplier_code LIKE ? ESCAPE '\\' "
            "OR u.username LIKE ? ESCAPE '\\' OR c.result_json LIKE ? ESCAPE '\\')"
        )
        s_esc = escape_like(search)
        like = f"%{s_esc}%"
        params.extend([like, like, like, like, like, like])
    if status_filter:
        conditions.append("c.status = ?")
        params.append(status_filter)

    where = "WHERE " + " AND ".join(conditions)

    db = get_db()
    try:
        total_count = db.execute(
            # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
            f"SELECT COUNT(*) FROM comparisons c JOIN users u ON c.user_id = u.id {where}",  # nosec B608
            params
        ).fetchone()[0]

        rows = db.execute(
            # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
            f"""SELECT c.id, c.user_id, c.doc_type, c.file_a, c.file_b,
                       c.status, c.diff_count, c.created_at, c.po_number,
                       c.supplier_code, c.total_a, c.total_b, c.comment,
                       c.approval_status, c.approved_by, c.approved_at,
                       c.is_public, c.is_deleted, c.deleted_at, c.deleted_by,
                       u.username
                FROM comparisons c
                JOIN users u ON c.user_id = u.id
                {where}
                ORDER BY c.created_at DESC
                LIMIT ? OFFSET ?""",  # nosec B608
            params + [per_page, offset]
        ).fetchall()
    finally:
        db.close()

    total_pages = max(1, (total_count + per_page - 1) // per_page)
    return render_template("history.html",
                           comparisons=rows,
                           username=session["username"],
                           role=session["role"],
                           can_see_all=can_see_all(role),
                           can_delete=can_delete(role),
                           page=page,
                           total_pages=total_pages,
                           total_count=total_count,
                           per_page=per_page,
                           search=search,
                           status_filter=status_filter)


@app.route("/scorecard")
@login_required
def scorecard_page():
    """Scorecard jakości dostawców — dla każdego dostawcy liczy wynik 0–100
    (ważony wg severity raportów), ocenę literową A–F, rozkład statusów,
    wskaźnik akceptacji i trend (ostatnie 90 dni vs całość historii).

    Uprawnienia: superuser+ widzi wszystkich dostawców; zwykły użytkownik —
    scorecard liczony tylko z jego własnych porównań (spójnie z /kpi).
    Soft-usunięte raporty (is_deleted=1) są wykluczane.
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    role    = session["role"]
    uid     = session["user_id"]
    see_all = can_see_all(role)
    since90 = (_dt.now(_tz.utc) - _td(days=90)).strftime("%Y-%m-%d")

    where = "WHERE COALESCE(c.is_deleted,0)=0"
    # 5 parametrów `since90` to 5 wyrażeń CASE okna 90-dniowego w SELECT (pozycyjne,
    # pojawiają się PRZED klauzulą WHERE), dopiero potem ewentualny filtr użytkownika.
    params: list = [since90, since90, since90, since90, since90]
    if not see_all:
        where += " AND c.user_id = ?"
        params.append(uid)

    db = get_db()
    try:
        # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
        rows = db.execute(f"""
            SELECT
                COALESCE(NULLIF(c.supplier_code,''),'Nieznany') AS supplier_code,
                MAX(s.name)    AS supplier_name,
                MAX(s.country) AS country,
                COUNT(*) AS total,
                SUM(CASE WHEN c.status IN ('ok','format') THEN 1 ELSE 0 END)            AS ok,
                SUM(CASE WHEN c.status IN ('ostrzezenie','warning') THEN 1 ELSE 0 END)  AS warnings,
                SUM(CASE WHEN c.status IN ('blad','error') THEN 1 ELSE 0 END)           AS errors,
                SUM(CASE WHEN c.status='critical' THEN 1 ELSE 0 END)                    AS critical,
                SUM(COALESCE(c.diff_count,0)) AS total_diffs,
                SUM(CASE WHEN c.approval_status='approved' THEN 1 ELSE 0 END) AS approved,
                SUM(CASE WHEN c.approval_status='rejected' THEN 1 ELSE 0 END) AS rejected,
                SUM(CASE WHEN c.created_at >= ? THEN 1 ELSE 0 END) AS recent_total,
                SUM(CASE WHEN c.created_at >= ? AND c.status IN ('ok','format') THEN 1 ELSE 0 END)           AS recent_ok,
                SUM(CASE WHEN c.created_at >= ? AND c.status IN ('ostrzezenie','warning') THEN 1 ELSE 0 END) AS recent_warn,
                SUM(CASE WHEN c.created_at >= ? AND c.status IN ('blad','error') THEN 1 ELSE 0 END)          AS recent_err,
                SUM(CASE WHEN c.created_at >= ? AND c.status='critical' THEN 1 ELSE 0 END)                   AS recent_crit,
                MAX(c.created_at) AS last_at
            FROM comparisons c
            LEFT JOIN suppliers s ON s.code = c.supplier_code
            {where}
            GROUP BY COALESCE(NULLIF(c.supplier_code,''),'Nieznany')
            ORDER BY total DESC
        """, params).fetchall()  # nosec B608
        rows = [dict(r) for r in rows]
    finally:
        db.close()

    # Wynik jakości: każdy raport waży wg severity (ok=1.0, ostrzeżenie=0.6,
    # błąd=0.25, krytyczny=0.0) → średnia ×100. Trend = wynik z 90 dni − wynik
    # ogólny (pokazywany tylko gdy w oknie jest ≥3 raporty, by uniknąć szumu).
    def _score(ok, warn, err, crit):
        tot = ok + warn + err + crit
        if tot <= 0:
            return None
        return round(100.0 * (1.0 * ok + 0.6 * warn + 0.25 * err) / tot)

    def _grade(sc):
        if sc is None:
            return "—"
        return ("A" if sc >= 90 else "B" if sc >= 75 else
                "C" if sc >= 60 else "D" if sc >= 40 else "F")

    cards = []
    for r in rows:
        ok, warn = int(r["ok"] or 0), int(r["warnings"] or 0)
        err, crit = int(r["errors"] or 0), int(r["critical"] or 0)
        total = int(r["total"] or 0)
        sc = _score(ok, warn, err, crit)
        rec_total = int(r["recent_total"] or 0)
        rec_sc = _score(int(r["recent_ok"] or 0), int(r["recent_warn"] or 0),
                        int(r["recent_err"] or 0), int(r["recent_crit"] or 0))
        trend = (rec_sc - sc) if (rec_sc is not None and sc is not None and rec_total >= 3) else None
        decided = int(r["approved"] or 0) + int(r["rejected"] or 0)
        cards.append({
            "code": r["supplier_code"],
            "name": r["supplier_name"] or "",
            "country": r["country"] or "",
            "total": total,
            "ok": ok, "warnings": warn, "errors": err, "critical": crit,
            "total_diffs": int(r["total_diffs"] or 0),
            "avg_diffs": round((int(r["total_diffs"] or 0) / total), 1) if total else 0,
            "approved": int(r["approved"] or 0),
            "rejected": int(r["rejected"] or 0),
            "approval_rate": round(100.0 * int(r["approved"] or 0) / decided) if decided else None,
            "score": sc,
            "grade": _grade(sc),
            "trend": trend,
            "last_at": r["last_at"] or "",
        })

    # Ranking: najlepszy wynik na górze; remis rozstrzyga liczba dokumentów.
    cards.sort(key=lambda c: (c["score"] if c["score"] is not None else -1, c["total"]),
               reverse=True)

    scored = [c for c in cards if c["score"] is not None]
    summary = {
        "suppliers": len(cards),
        "scored": len(scored),
        "total_docs": sum(c["total"] for c in cards),
        "avg_score": round(sum(c["score"] for c in scored) / len(scored)) if scored else None,
        "at_risk": sum(1 for c in scored if c["grade"] in ("D", "F")),
        "best": (max(scored, key=lambda c: (c["score"], c["total"]))["code"]
                 if scored else None),
    }

    return render_template("scorecard.html",
                           username=session["username"],
                           role=role,
                           can_see_all=see_all,
                           cards=cards,
                           summary=summary)


@app.route("/versions")
@login_required
def versions_page():
    """Śledzenie rewizji dokumentu — ten sam (dostawca, numer PO) zgłoszony
    wielokrotnie, ale EWOLUUJĄCY: między wersjami zmienił się status, liczba
    różnic lub kwota (np. PI v1 → PI podpisana → CI). Oś czasu pokazuje, czy
    kolejne wersje poprawiają (mniej różnic) czy pogarszają (więcej) zgodność.

    To odróżnia ten widok od /duplicates (identyczne ponowienia / ryzyko
    podwójnej płatności): tu pokazujemy WYŁĄCZNIE pary, które się zmieniły.
    Uprawnienia/zakres jak /scorecard. Soft-usunięte wykluczone.
    """
    role    = session["role"]
    uid     = session["user_id"]
    see_all = can_see_all(role)

    where = ("WHERE COALESCE(c.is_deleted,0)=0 "
             "AND TRIM(c.supplier_code) <> '' AND TRIM(c.po_number) <> ''")
    params: list = []
    if not see_all:
        where += " AND c.user_id = ?"
        params.append(uid)

    db = get_db()
    try:
        # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
        rows = db.execute(f"""
            SELECT c.id, c.supplier_code, c.po_number, c.total_a, c.total_b,
                   c.status, c.doc_type, c.diff_count, c.created_at, u.username
            FROM comparisons c JOIN users u ON u.id = c.user_id
            {where}
            ORDER BY c.created_at ASC
            LIMIT 20000
        """, params).fetchall()  # nosec B608
        rows = [dict(r) for r in rows]
    finally:
        db.close()

    from collections import OrderedDict as _OD
    groups: "dict[tuple, list]" = _OD()
    for r in rows:
        key = ((r["supplier_code"] or "").strip().upper(),
               (r["po_number"] or "").strip().upper())
        groups.setdefault(key, []).append(r)

    _ERR = ("blad", "error", "critical")
    _WARN = ("ostrzezenie", "warning")

    def _amount(m):
        return (str(m["total_a"] or "").strip() or str(m["total_b"] or "").strip())

    timelines = []
    improving = regressing = 0
    for (sup, po), members in groups.items():
        if len(members) < 2:
            continue
        # members są już w kolejności rosnącej po dacie (ORDER BY ASC)
        versions = []
        changed_any = False
        prev = None
        for m in members:
            dc = int(m["diff_count"] or 0)
            amt = _amount(m)
            delta = None
            if prev is not None:
                d_diffs = dc - int(prev["diff_count"] or 0)
                amount_changed = amt != _amount(prev)
                status_changed = (m["status"] or "") != (prev["status"] or "")
                if d_diffs != 0 or amount_changed or status_changed:
                    changed_any = True
                delta = {
                    "d_diffs": d_diffs,
                    "amount_changed": amount_changed,
                    "status_changed": status_changed,
                }
            versions.append({
                "id": m["id"],
                "date": (m["created_at"] or "")[:16],
                "user": m["username"] or "",
                "doc_type": m["doc_type"] or "",
                "status": m["status"] or "",
                "diff_count": dc,
                "amount": amt,
                "delta": delta,
            })
            prev = m
        if not changed_any:
            continue  # identyczne ponowienia → należą do /duplicates, nie tutaj

        first_dc = int(members[0]["diff_count"] or 0)
        last_dc  = int(members[-1]["diff_count"] or 0)
        trend = "flat"
        if last_dc < first_dc:
            trend = "improving"; improving += 1
        elif last_dc > first_dc:
            trend = "regressing"; regressing += 1
        timelines.append({
            "supplier": sup,
            "po": po,
            "count": len(versions),
            "trend": trend,
            "first_diffs": first_dc,
            "last_diffs": last_dc,
            "last_at": (members[-1]["created_at"] or "")[:16],
            "versions": versions,
        })

    # Najświeższe rewizje na górze.
    timelines.sort(key=lambda t: t["last_at"], reverse=True)

    summary = {
        "tracked": len(timelines),
        "improving": improving,
        "regressing": regressing,
    }

    return render_template("versions.html",
                           username=session["username"],
                           role=role,
                           can_see_all=see_all,
                           timelines=timelines,
                           summary=summary)


@app.route("/duplicates")
@login_required
def duplicates_page():
    """Wykrywanie zduplikowanych faktur/zamówień — ta sama para (dostawca, numer
    PO) przetworzona więcej niż raz, niezależnie od tego, czy plik PDF jest
    bajtowo identyczny (re-skan / ponowny eksport omija dedup po haszu pliku).
    Ryzyko podwójnej płatności/podwójnego fakturowania.

    Klaster z RÓŻNYMI kwotami jest oznaczany mocniej — to albo korekta, albo
    realny błąd. Uprawnienia jak /scorecard (superuser+ widzi wszystkich;
    użytkownik — swoje). Soft-usunięte wykluczone.
    """
    role    = session["role"]
    uid     = session["user_id"]
    see_all = can_see_all(role)

    where = ("WHERE COALESCE(c.is_deleted,0)=0 "
             "AND TRIM(c.supplier_code) <> '' AND TRIM(c.po_number) <> ''")
    params: list = []
    if not see_all:
        where += " AND c.user_id = ?"
        params.append(uid)

    db = get_db()
    try:
        # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
        rows = db.execute(f"""
            SELECT c.id, c.supplier_code, c.po_number, c.total_a, c.total_b,
                   c.status, c.doc_type, c.created_at, c.file_a, c.file_b,
                   u.username
            FROM comparisons c JOIN users u ON u.id = c.user_id
            {where}
            ORDER BY c.created_at DESC
            LIMIT 20000
        """, params).fetchall()  # nosec B608
        rows = [dict(r) for r in rows]
    finally:
        db.close()

    # Grupowanie w Pythonie (unika dialektowych GROUP_CONCAT/string_agg).
    from collections import OrderedDict as _OD
    groups: "dict[tuple, list]" = _OD()
    for r in rows:
        key = ((r["supplier_code"] or "").strip().upper(),
               (r["po_number"] or "").strip().upper())
        groups.setdefault(key, []).append(r)

    clusters = []
    for (sup, po), members in groups.items():
        if len(members) < 2:
            continue
        # „Kwota" pozycji: total_a, a gdy brak — total_b.
        amounts = set()
        for m in members:
            amt = (str(m["total_a"] or "").strip() or str(m["total_b"] or "").strip())
            if amt:
                amounts.add(amt)
        amount_mismatch = len(amounts) > 1
        clusters.append({
            "supplier": sup,
            "po": po,
            "count": len(members),
            "amount_mismatch": amount_mismatch,
            "amounts": sorted(amounts),
            "members": [{
                "id": m["id"],
                "date": (m["created_at"] or "")[:16],
                "user": m["username"] or "",
                "doc_type": m["doc_type"] or "",
                "status": m["status"] or "",
                "total_a": str(m["total_a"] or "").strip(),
                "total_b": str(m["total_b"] or "").strip(),
                "file_a": m["file_a"] or "",
                "file_b": m["file_b"] or "",
            } for m in members],
        })

    # Najpierw klastry z rozbieżną kwotą (większe ryzyko), potem najliczniejsze.
    clusters.sort(key=lambda c: (c["amount_mismatch"], c["count"]), reverse=True)

    summary = {
        "clusters": len(clusters),
        "dup_docs": sum(c["count"] for c in clusters),
        "mismatch": sum(1 for c in clusters if c["amount_mismatch"]),
    }

    return render_template("duplicates.html",
                           username=session["username"],
                           role=role,
                           can_see_all=see_all,
                           clusters=clusters,
                           summary=summary)


@app.route("/admin")
@login_required
def admin_page():
    if not is_admin(session["role"]):
        return redirect(url_for("artwork_page"))
    db = get_db()
    try:
        users = [dict(r) for r in db.execute(
            "SELECT id, username, email, role, created_at, last_login, "
            "department, allowed_modules FROM users ORDER BY role DESC, username"
        ).fetchall()]
        stats = db.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN status IN ('blad','error','critical') THEN 1 ELSE 0 END) as errors,
                   SUM(CASE WHEN status IN ('ostrzezenie','warning') THEN 1 ELSE 0 END) as warnings
            FROM comparisons
        """).fetchone()
        # Filter in Python to avoid TEXT vs TIMESTAMP comparison issue on PostgreSQL
        try:
            all_tokens = db.execute(
                """SELECT pr.token, pr.expires_at, u.username, u.email
                   FROM password_reset_tokens pr JOIN users u ON u.id=pr.user_id
                   WHERE pr.used=0
                   ORDER BY pr.created_at DESC LIMIT 20"""
            ).fetchall()
            from datetime import datetime as _dt, timezone as _tz
            _now = _dt.now(_tz.utc).strftime("%Y-%m-%d %H:%M:%S")
            reset_tokens = [t for t in all_tokens if t["expires_at"] and str(t["expires_at"]) > _now]
        except Exception:
            reset_tokens = []

        # API usage stats
        api_usage_stats = {"total_cost": 0, "total_calls": 0, "by_model": [], "by_type": [], "recent": []}
        try:
            row = db.execute(
                "SELECT COUNT(*) as calls, SUM(input_tokens) as inp, SUM(output_tokens) as out, SUM(cost_usd) as cost FROM api_usage"
            ).fetchone()
            if row:
                api_usage_stats["total_calls"] = row["calls"] or 0
                api_usage_stats["total_input"]  = row["inp"] or 0
                api_usage_stats["total_output"] = row["out"] or 0
                api_usage_stats["total_cost"]   = round(row["cost"] or 0, 4)
            api_usage_stats["by_model"] = [dict(r) for r in db.execute(
                """SELECT model, COUNT(*) as calls,
                          SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens,
                          SUM(cost_usd) as cost_usd
                   FROM api_usage GROUP BY model ORDER BY cost_usd DESC"""
            ).fetchall()]
            api_usage_stats["by_type"] = [dict(r) for r in db.execute(
                """SELECT call_type, COUNT(*) as calls, SUM(cost_usd) as cost_usd
                   FROM api_usage GROUP BY call_type ORDER BY cost_usd DESC"""
            ).fetchall()]
            api_usage_stats["by_day"] = [dict(r) for r in db.execute(
                """SELECT substr(created_at,1,10) as day, COUNT(*) as calls,
                          SUM(cost_usd) as cost_usd
                   FROM api_usage GROUP BY day ORDER BY day DESC LIMIT 30"""
            ).fetchall()]
            api_usage_stats["recent"] = [dict(r) for r in db.execute(
                """SELECT a.created_at, a.model, a.call_type, a.input_tokens,
                          a.output_tokens, a.cost_usd, u.username
                   FROM api_usage a LEFT JOIN users u ON a.user_id=u.id
                   ORDER BY a.created_at DESC LIMIT 50"""
            ).fetchall()]
        except Exception:
            pass
    finally:
        db.close()
    return render_template("admin.html", users=users, stats=stats,
                           reset_tokens=reset_tokens,
                           api_usage=api_usage_stats,
                           username=session["username"], role=session["role"],
                           can_see_all=True, ROLE_LEVEL=ROLE_LEVEL)


def _pretty_bytes(n):
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "0 B"
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return (f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}")
        n /= 1024


@app.route("/api/admin/db-usage")
@require_role("admin")
def api_admin_db_usage():
    """Zużycie bazy: rozmiar bazy, największe tabele, miejsce zajęte przez mastery
    artworków; wolne miejsce liczone, gdy ustawiono DB_SIZE_LIMIT_GB."""
    import db as _dbmod
    is_pg = bool(getattr(_dbmod, "DATABASE_URL", ""))
    out = {"engine": "postgresql" if is_pg else "sqlite"}
    db = get_db()
    try:
        try:
            if is_pg:
                row = db.execute("SELECT pg_database_size(current_database()) AS sz").fetchone()
                out["db_bytes"] = int(row["sz"] or 0)
                out["tables"] = [{"name": r["name"], "pretty": _pretty_bytes(r["bytes"])}
                                 for r in db.execute(
                    "SELECT relname AS name, pg_total_relation_size(relid) AS bytes "
                    "FROM pg_catalog.pg_statio_user_tables "
                    "ORDER BY pg_total_relation_size(relid) DESC LIMIT 10").fetchall()]
            else:
                row = db.execute(
                    "SELECT (SELECT page_count FROM pragma_page_count())*"
                    "(SELECT page_size FROM pragma_page_size()) AS sz").fetchone()
                out["db_bytes"] = int(row["sz"] or 0)
                out["tables"] = []
            out["db_pretty"] = _pretty_bytes(out["db_bytes"])
        except Exception as e:
            out["error"] = f"Nie udało się odczytać rozmiaru bazy: {e}"
        try:
            import artwork_index as _ai
            _ai.ensure_master_file_table(db)
            r = db.execute("SELECT COUNT(*) AS c, COALESCE(SUM(LENGTH(data)),0) AS b "
                           "FROM artwork_master_file WHERE data IS NOT NULL").fetchone()
            out["masters_count"] = int(r["c"] or 0)
            out["masters_bytes"] = int(r["b"] or 0)
            out["masters_pretty"] = _pretty_bytes(out["masters_bytes"])
        except Exception:
            pass
        try:
            _lr = db.execute("SELECT value FROM settings WHERE category='admin' "
                             "AND key='db_size_limit_gb'").fetchone()
            _lim_setting = _lr["value"] if _lr else None
        except Exception:
            _lim_setting = None
        try:
            _ar = db.execute("SELECT value FROM settings WHERE category='admin' "
                             "AND key='artwork_budget_gb'").fetchone()
            _art_setting = _ar["value"] if _ar else None
        except Exception:
            _art_setting = None
    finally:
        db.close()
    lim = _lim_setting or os.environ.get("DB_SIZE_LIMIT_GB")
    if lim:
        try:
            lb = float(lim) * 1024 ** 3
            out["limit_gb"] = float(lim)
            out["limit_bytes"] = lb
            out["limit_pretty"] = _pretty_bytes(lb)
            out["free_bytes"] = max(0, lb - out.get("db_bytes", 0))
            out["free_pretty"] = _pretty_bytes(out["free_bytes"])
        except ValueError:
            pass
    if _art_setting:
        try:
            ab = float(_art_setting) * 1024 ** 3
            used = out.get("masters_bytes", 0)
            out["artwork_budget_gb"] = float(_art_setting)
            out["artwork_budget_pretty"] = _pretty_bytes(ab)
            out["artwork_free_pretty"] = _pretty_bytes(max(0, ab - used))
            out["artwork_pct"] = round(used * 100.0 / ab, 1) if ab > 0 else 0
        except ValueError:
            pass
    return jsonify(out)


@app.route("/api/admin/db-limit", methods=["POST"])
@require_role("admin")
@csrf_protect
def api_admin_db_limit():
    """Zapisuje limit dysku (GB) i/lub budżet na artworki (GB) — do pasków w /admin."""
    data = request.get_json(silent=True) or {}
    saved = {}
    for field, key in (("limit_gb", "db_size_limit_gb"), ("artwork_gb", "artwork_budget_gb")):
        if field not in data:
            continue
        raw = str(data.get(field) or "").replace(",", ".").strip()
        try:
            val = float(raw)
            if val < 0:
                raise ValueError
        except ValueError:
            return jsonify({"error": f"Podaj liczbę GB w polu {field} (np. 160)"}), 400
        saved[key] = val
    if not saved:
        return jsonify({"error": "Brak wartości do zapisu"}), 400
    db = get_db()
    try:
        for key, val in saved.items():
            db.execute(
                "INSERT INTO settings(category,key,value) VALUES('admin',?,?) "
                "ON CONFLICT(category,key) DO UPDATE SET value=EXCLUDED.value",
                (key, str(val)))
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True, **saved})


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES — API COMPARE
# ─────────────────────────────────────────────────────────────────────────────

_B64_KEYS = frozenset({
    "img_a_b64", "img_b_b64", "img_diff_b64", "img_a_annot_b64", "img_b_annot_b64",
    "img_a_hires_b64", "img_b_hires_b64", "diff_overlay_b64", "page_a_b64", "page_b_b64",
    "thumb_b64", "thumb_data",
})

def _strip_images_from_result(d: dict) -> dict:
    """Recursively remove base64 image keys so result_json stays compact in DB."""
    if not isinstance(d, dict):
        return d
    out = {}
    for k, v in d.items():
        if k in _B64_KEYS:
            continue
        if isinstance(v, dict):
            out[k] = _strip_images_from_result(v)
        elif isinstance(v, list):
            out[k] = [_strip_images_from_result(i) if isinstance(i, dict) else i for i in v]
        else:
            out[k] = v
    return out


_VALID_COMPARISON_STATUSES = {"ok", "warning", "error", "critical", "blad", "ostrzezenie"}


def _save_comparison(uid, doc_type, file_a_name, file_b_name, result_dict,
                     hash_a=None, hash_b=None, path_a=None, path_b=None):
    """Helper: zapisuje porównanie do bazy."""
    # Strip large base64 image payloads before persisting (artwork images stay in session cache)
    result_dict = _strip_images_from_result(result_dict)
    # Apply length caps on caller-provided names
    file_a_name = str(file_a_name or "")[:500]
    file_b_name = str(file_b_name or "")[:500]
    # Wyciągnij dodatkowe metadane
    po_number = ''
    supplier_code = str(result_dict.get('supplier_detected') or '')[:50]
    total_a = ''
    total_b = ''

    # Szukaj numeru PO w modułach
    mods = result_dict.get('modules', {})
    tbl = mods.get('table', {})
    enh = mods.get('enhanced', {})
    # Total
    total_a = str(tbl.get('total_a') or enh.get('total_a') or '')
    total_b = str(tbl.get('total_b') or enh.get('total_b') or '')
    # Numer PO z nagłówków
    for mod in [tbl, enh]:
        for h in (mod.get('headers') or []):
            if h.get('key', '').lower() in ('numer po', 'po number', 'numer zamówienia / po'):
                v = h.get('val_a') or h.get('val_b') or ''
                if v and not po_number:
                    po_number = str(v)[:30]

    db = get_db()
    try:
        _rl = result_dict.get("risk_level", result_dict.get("status", "ok"))
        if _rl not in _VALID_COMPARISON_STATUSES:
            _rl = "ok"
        cursor = db.execute(
            """INSERT INTO comparisons
               (user_id,doc_type,file_a,file_b,result_json,status,diff_count,
                po_number,supplier_code,total_a,total_b,file_hash_a,file_hash_b)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (uid, doc_type[:50], file_a_name, file_b_name,
             json.dumps(result_dict, ensure_ascii=False),
             _rl,
             int(result_dict.get("total_errors", result_dict.get("diff_count",
                 result_dict.get("error_count", 0))) or 0),
             po_number, supplier_code, str(total_a)[:50], str(total_b)[:50], hash_a, hash_b)
        )
        cid = cursor.lastrowid
        db.commit()
        _kpi_cache_invalidate()
        status = result_dict.get("risk_level", result_dict.get("status", "ok"))
        # Fire webhook for external integrations (non-blocking)
        try:
            _fire_webhooks_bg("comparison.completed", {
                "comparison_id": cid,
                "doc_type": doc_type,
                "status": status,
                "diff_count": result_dict.get("diff_count", 0),
                "supplier": supplier_code,
                "po_number": po_number,
            })
        except Exception:
            pass
        # Email alert for critical results
        if status == "critical":
            try:
                urow = db.execute("SELECT username, email FROM users WHERE id=?", (uid,)).fetchone()
                if urow and urow["email"]:
                    _send_critical_alert(
                        username=urow["username"],
                        to_email=urow["email"],
                        cid=cid,
                        doc_type=doc_type,
                        file_a=file_a_name,
                        file_b=file_b_name,
                        diff_count=result_dict.get("diff_count", result_dict.get("total_errors", 0)),
                        po_number=po_number,
                    )
            except Exception:
                pass
        # Auto-create shipment record when PO is detected
        if po_number:
            try:
                _ensure_shipment(po_number, supplier_code, cid, uid)
            except Exception:
                pass
            # Auto-save uploaded files to shipment document folder
            if path_a and os.path.exists(path_a):
                _save_doc_to_shipment(po_number, path_a, file_a_name, doc_type, uid)
            if path_b and os.path.exists(path_b):
                _save_doc_to_shipment(po_number, path_b, file_b_name, doc_type, uid)
        # Notifications
        try:
            _fa = (file_a_name or "")[:200]
            _fb = (file_b_name or "")[:200]
            if doc_type == "SAD":
                _notify_role("manager", "sad_pending",
                             f"🛃 Nowe SAD do zatwierdzenia — {(po_number or _fa)[:200]}",
                             f"Plik: {_fa} vs {_fb}",
                             f"/sad")
            elif status in ("error", "critical"):
                diff = result_dict.get("diff_count", result_dict.get("total_errors", 0))
                _create_notification(uid, "compare_error",
                                     f"❌ Rozbieżność w porównaniu — {diff} błędów",
                                     f"{doc_type}: {_fa} vs {_fb}",
                                     f"/history")
        except Exception:
            pass
        return cid
    finally:
        db.close()


# Zadania kolejkowalne (Faza 3): ciężkie silniki porównań wołane synchronicznie.
# Wyodrębnione do funkcji modułowych, by `jobs.run(...)` mogło policzyć je w workerze
# (gdy REDIS_URL) i zwrócić wynik bez zmiany kontraktu HTTP. Argumenty lekkie
# (ścieżki/typy/teksty), zwrot to dict — serializowalne przez RQ.
@app.route("/api/comparison/<int:cid>")
@login_required
def get_comparison(cid):
    uid = session["user_id"]
    role = session["role"]
    db = get_db()
    try:
        row = db.execute("""
            SELECT c.*, u.username
            FROM comparisons c
            JOIN users u ON c.user_id = u.id
            WHERE c.id = ?
            AND (c.is_deleted = 0 OR c.is_deleted IS NULL)
        """, (cid,)).fetchone()
    finally:
        db.close()
    if not row:
        return jsonify({"error": "Nie znaleziono"}), 404
    if not can_see_all(role) and row["user_id"] != uid:
        return jsonify({"error": "Brak dostępu"}), 403
    try:
        data = json.loads(row["result_json"] or "{}")
    except (ValueError, TypeError):
        return jsonify({"error": "Dane porównania uszkodzone"}), 500
    # Dodaj metadane z bazy (mogą być nowsze/pełniejsze niż w result_json)
    data["comparison_id"]  = cid
    data["username"]       = row["username"]
    data["created_at"]     = row["created_at"]
    data["db_status"]      = row["status"]
    data["db_diff_count"]  = row["diff_count"]
    return jsonify(data)


@app.route("/api/comparison/<int:cid>", methods=["DELETE"])
@login_required
@csrf_protect
def delete_comparison(cid):
    uid = session["user_id"]
    role = session["role"]
    if not can_delete(role):
        return jsonify({"error": "Brak uprawnień do usuwania"}), 403

    db = get_db()
    try:
        row = db.execute("SELECT id, user_id FROM comparisons WHERE id = ?", (cid,)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono"}), 404
        # IDOR fix: superuser may delete only own records unless they can_see_all
        if row["user_id"] != uid and not can_see_all(role):
            return jsonify({"error": "Brak dostępu do tego zasobu"}), 403
        # Soft delete — set is_deleted flag instead of removing the row
        db.execute(
            "UPDATE comparisons SET is_deleted=1, deleted_at=datetime('now'), deleted_by=? WHERE id=?",
            (uid, cid)
        )
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True, "deleted_id": cid})


@app.route("/api/comparisons/bulk_delete", methods=["POST"])
@login_required
@csrf_protect
def bulk_delete_comparisons():
    if not can_delete(session["role"]):
        return jsonify({"error": "Brak uprawnień"}), 403
    data = request.get_json(silent=True) or {}
    raw_ids = data.get("ids", [])
    # Strictly validate: only accept positive integers — prevents SQL injection
    try:
        ids = [int(x) for x in raw_ids if str(x).strip().lstrip("-").isdigit() and int(x) > 0]
    except (ValueError, TypeError):
        return jsonify({"error": "Nieprawidłowe ID"}), 400
    if not ids:
        return jsonify({"error": "Brak ID do usunięcia"}), 400
    if len(ids) > 200:
        return jsonify({"error": "Zbyt wiele ID naraz (max 200)"}), 400

    uid = session["user_id"]
    role = session["role"]
    db = get_db()
    try:
        placeholders = ",".join("?" * len(ids))
        # Soft-delete: set is_deleted flag instead of removing rows
        if can_see_all(role):
            cursor = db.execute(
                # Bandit B608: interpolowane są tylko placeholdery ? (liczba = długość listy); wartości jako parametry.
                f"UPDATE comparisons SET is_deleted=1, deleted_at=datetime('now'), deleted_by=?"  # nosec B608
                f" WHERE id IN ({placeholders}) AND is_deleted=0",
                [uid] + ids
            )
        else:
            cursor = db.execute(
                # Bandit B608: interpolowane są tylko placeholdery ? (liczba = długość listy); wartości jako parametry.
                f"UPDATE comparisons SET is_deleted=1, deleted_at=datetime('now'), deleted_by=?"  # nosec B608
                f" WHERE id IN ({placeholders}) AND user_id=? AND is_deleted=0",
                [uid] + ids + [uid]
            )
        deleted = cursor.rowcount
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True, "deleted": deleted})


@app.route("/api/comparison/<int:cid>/restore", methods=["POST"])
@login_required
@csrf_protect
def restore_comparison(cid):
    """Przywraca miękko usuniętą pozycję z historii."""
    uid = session["user_id"]
    role = session["role"]
    db = get_db()
    try:
        row = db.execute("SELECT id, user_id, is_deleted FROM comparisons WHERE id=?", (cid,)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono"}), 404
        if not row["is_deleted"]:
            return jsonify({"error": "Pozycja nie jest usunięta"}), 400
        if row["user_id"] != uid and not can_see_all(role):
            return jsonify({"error": "Brak dostępu"}), 403
        db.execute(
            "UPDATE comparisons SET is_deleted=0, deleted_at=NULL, deleted_by=NULL WHERE id=?",
            (cid,)
        )
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True, "restored_id": cid})


@app.route("/api/comparison/<int:cid>/approve", methods=["POST"])
@require_role("manager")
@csrf_protect
def approve_comparison(cid):
    """Zatwierdza lub odrzuca porównanie (workflow compliance)."""
    data = request.get_json(silent=True) or {}
    verdict = data.get("status", "approved")
    if verdict not in ("approved", "rejected", "pending"):
        return jsonify({"error": "Nieprawidłowy status. Użyj: approved, rejected, pending"}), 400
    note = str(data.get("note") or "")[:500]
    uid = session["user_id"]
    db = get_db()
    try:
        row = db.execute("SELECT id FROM comparisons WHERE id=? AND (is_deleted IS NULL OR is_deleted=0)", (cid,)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono"}), 404
        comp = db.execute(
            "SELECT doc_type, po_number FROM comparisons WHERE id=?", (cid,)
        ).fetchone()
        db.execute(
            "UPDATE comparisons SET approval_status=?, approved_by=?, approved_at=datetime('now'), approval_note=? WHERE id=?",
            (verdict, uid, note, cid)
        )
        db.commit()
        # SAD approved → advance matching shipment to 'odprawa'
        if comp and comp["doc_type"] == "SAD" and verdict == "approved":
            try:
                po = comp["po_number"] or ""
                if po:
                    db.execute(
                        "UPDATE shipments SET status='odprawa', updated_at=datetime('now') "
                        "WHERE po_number=? AND status NOT IN ('w_magazynie','zakonczone')",
                        (po,)
                    )
                    db.commit()
            except Exception:
                pass
        _log_audit("comparison_approval", session["username"],
                   f"cid={cid} verdict={verdict} note={note[:80]}")
    finally:
        db.close()
    return jsonify({"ok": True, "status": verdict, "cid": cid})


@app.route("/profile")
@login_required
def profile_page():
    uid = session["user_id"]
    db = get_db()
    try:
        user = db.execute(
            "SELECT id, username, email, role, department, phone, language, "
            "allowed_modules, created_at, last_login FROM users WHERE id=?", (uid,)
        ).fetchone()
        activity = db.execute(
            "SELECT event, detail, ip, created_at FROM audit_log "
            "WHERE username=? ORDER BY created_at DESC LIMIT 50",
            (session["username"],)
        ).fetchall()
        comp_count = db.execute(
            "SELECT COUNT(*) FROM comparisons WHERE user_id=?", (uid,)
        ).fetchone()[0]
    finally:
        db.close()
    return render_template("profile.html",
                           user=dict(user) if user else {},
                           activity=[dict(r) for r in activity],
                           comp_count=comp_count,
                           username=session["username"],
                           role=session["role"])


@app.route("/api/profile/update", methods=["POST"])
@login_required
@csrf_protect
def api_profile_update():
    uid = session["user_id"]
    data = request.get_json(silent=True) or {}
    _profile_max_lens = {"email": 254, "phone": 40, "department": 100}
    allowed = ["email", "phone", "department"]
    fields, vals = [], []
    for k in allowed:
        if k in data:
            v = str(data[k] or "").strip()
            max_len = _profile_max_lens.get(k, 200)
            if len(v) > max_len:
                return jsonify({"error": f"Pole '{k}' jest za długie (max {max_len})"}), 400
            fields.append(f"{k}=?")
            vals.append(v or None)
    if not fields:
        return jsonify({"error": "Brak pól"}), 400
    vals.append(uid)
    db = get_db()
    try:
        # Bandit B608: nazwy kolumn z twardej białej listy w kodzie; wartości jako parametry ?.
        db.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=?", vals)  # nosec B608
        db.commit()
    except IntegrityError:
        return jsonify({"error": "Email jest już zajęty"}), 400
    finally:
        db.close()
    _log_audit("profile_updated", session["username"], f"fields={','.join(f.split('=')[0] for f in fields)}")
    return jsonify({"ok": True})


@app.route("/api/profile/change-password", methods=["POST"])
@login_required
@csrf_protect
def api_profile_change_password():
    uid = session["user_id"]
    data = request.get_json(silent=True) or {}
    current = data.get("current_password", "")
    new_pwd = data.get("new_password", "")
    if not current or not new_pwd:
        return jsonify({"error": "Podaj obecne i nowe hasło"}), 400
    if len(current) > 1000 or len(new_pwd) > 1000:
        return jsonify({"error": "Hasło jest za długie"}), 400
    if len(new_pwd) < 8:
        return jsonify({"error": "Nowe hasło musi mieć co najmniej 8 znaków"}), 400
    if not any(c.isdigit() for c in new_pwd):
        return jsonify({"error": "Nowe hasło musi zawierać cyfrę"}), 400
    db = get_db()
    try:
        user = db.execute("SELECT password_hash FROM users WHERE id=?", (uid,)).fetchone()
        if not user or not check_password_hash(user["password_hash"], current):
            return jsonify({"error": "Obecne hasło jest nieprawidłowe"}), 400
        db.execute("UPDATE users SET password_hash=? WHERE id=?",
                   (generate_password_hash(new_pwd), uid))
        db.commit()
    finally:
        db.close()
    _log_audit("password_changed", session["username"], "Własna zmiana hasła")
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────────────────────────
# ACTIVITY LOG PAGE
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/activity")
@login_required
def activity_page():
    if session.get("role", "user") not in ("manager", "superuser", "admin"):
        return redirect(url_for("artwork_page"))
    db = get_db()
    try:
        users_list = db.execute(
            "SELECT DISTINCT username FROM audit_log ORDER BY username"
        ).fetchall()
        event_types = db.execute(
            "SELECT DISTINCT event FROM audit_log ORDER BY event"
        ).fetchall()
        # Stats
        total_today = db.execute(
            "SELECT COUNT(*) FROM audit_log WHERE date(created_at)=date('now')"
        ).fetchone()[0]
        total_week = db.execute(
            "SELECT COUNT(*) FROM audit_log WHERE created_at >= ?", (_ts_ago(days=7),)
        ).fetchone()[0]
        active_users = db.execute(
            "SELECT COUNT(DISTINCT username) FROM audit_log WHERE created_at >= ?",
            (_ts_ago(days=7),)
        ).fetchone()[0]
        top_events = db.execute(
            "SELECT event, COUNT(*) as cnt FROM audit_log "
            "GROUP BY event ORDER BY cnt DESC LIMIT 10"
        ).fetchall()
        recent = db.execute(
            "SELECT event, username, detail, ip, created_at FROM audit_log "
            "ORDER BY created_at DESC LIMIT 200"
        ).fetchall()
    finally:
        db.close()
    return render_template("activity.html",
                           users_list=[r["username"] for r in users_list],
                           event_types=[r["event"] for r in event_types],
                           total_today=total_today,
                           total_week=total_week,
                           active_users=active_users,
                           top_events=[dict(r) for r in top_events],
                           recent=[dict(r) for r in recent],
                           username=session["username"],
                           role=session["role"])



# ─────────────────────────────────────────────────────────────────────────────
# ROUTE — UNIFIED ANALYZE (wszystkie 3 konteksty naraz)
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# NOTIFICATIONS SYSTEM
# ─────────────────────────────────────────────────────────────────────────────

def _create_notification(user_id: int, ntype: str, title: str, message: str = "", link: str = "") -> None:
    """Create one in-app notification for a user (silent, never raises)."""
    try:
        db = get_db()
        try:
            db.execute(
                "INSERT INTO notifications(user_id, type, title, message, link) VALUES(?,?,?,?,?)",
                (user_id, ntype, title, message, link)
            )
            db.commit()
        finally:
            db.close()
    except Exception as _e:
        logger.debug("_create_notification failed (non-fatal): %s", _e)


def _notify_role(role: str, ntype: str, title: str, message: str = "", link: str = "") -> None:
    """Broadcast a notification to all users with the given role or above."""
    try:
        db = get_db()
        try:
            roles = [r for r, lvl in ROLE_LEVEL.items() if lvl >= ROLE_LEVEL.get(role, 0)]
            users = db.execute(
                # Bandit B608: interpolowane są tylko placeholdery ? (liczba = długość listy); wartości jako parametry.
                f"SELECT id FROM users WHERE role IN ({','.join('?'*len(roles))}) AND (is_active IS NULL OR is_active=1)",  # nosec B608
                roles
            ).fetchall()
            if users:
                db.executemany(
                    "INSERT INTO notifications(user_id, type, title, message, link) VALUES(?,?,?,?,?)",
                    [(u["id"], ntype, title, message, link) for u in users]
                )
            db.commit()
        finally:
            db.close()
    except Exception as _e:
        logger.debug("_notify_role failed (non-fatal): %s", _e)


def _notify_managers(title: str, message: str = "", link: str = "", email: bool = True) -> None:
    """Powiadom managerów+ in-app oraz (best-effort, w tle) e-mailem. Nigdy nie rzuca."""
    try:
        _notify_role("manager", "delivery", title, message, link)
    except Exception:
        pass
    if not email:
        return
    def _send():
        try:
            db = get_db()
            try:
                roles = [r for r, lvl in ROLE_LEVEL.items() if lvl >= ROLE_LEVEL.get("manager", 0)]
                rows = db.execute(
                    # Bandit B608: interpolowane są tylko placeholdery ? (liczba = długość listy); wartości jako parametry.
                    f"SELECT email FROM users WHERE role IN ({','.join('?'*len(roles))}) "  # nosec B608
                    f"AND email IS NOT NULL AND email<>'' AND (is_active IS NULL OR is_active=1)",
                    roles,
                ).fetchall()
            finally:
                db.close()
            _base = os.environ.get("APP_BASE_URL", "").rstrip("/")
            _body = message + (f"\n\n{_base}{link}" if (link and _base) else "")
            for r in rows:
                try:
                    _send_email(r["email"], title, _body)
                except Exception:
                    pass
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()


def _auto_notifications() -> None:
    """No-op po usunięciu transportu/dokumentów (transport_queue, SAD, kierowcy,
    magazyn zniknęły). Zostawiony jako stub, bo woła go api_notifications;
    powiadomienia artworkowe dodać tu, gdy będą potrzebne."""
    return


@app.route("/api/notifications")
@login_required
def api_notifications():
    uid = session["user_id"]
    _auto_notifications()
    db = get_db()
    try:
        rows = db.execute(
            "SELECT id, type, title, message, link, is_read, created_at "
            "FROM notifications WHERE user_id=? "
            "ORDER BY is_read ASC, created_at DESC LIMIT 100",
            (uid,)
        ).fetchall()
        unread = db.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0", (uid,)
        ).fetchone()[0]
        return jsonify({
            "notifications": [dict(r) for r in rows],
            "unread": unread
        })
    finally:
        db.close()


@app.route("/api/notifications/<int:nid>/read", methods=["POST"])
@login_required
@csrf_protect
def api_notification_read(nid):
    uid = session["user_id"]
    db = get_db()
    try:
        db.execute("UPDATE notifications SET is_read=1 WHERE id=? AND user_id=?", (nid, uid))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/notifications/read-all", methods=["POST"])
@login_required
@csrf_protect
def api_notifications_read_all():
    uid = session["user_id"]
    db = get_db()
    try:
        db.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (uid,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/notifications/<int:nid>", methods=["DELETE"])
@login_required
@csrf_protect
def api_notification_delete(nid):
    uid = session["user_id"]
    db = get_db()
    try:
        db.execute("DELETE FROM notifications WHERE id=? AND user_id=?", (nid, uid))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# SHIPMENTS — tracking from PO to SAP MIGO
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/doc-types")
@login_required
def doc_types_page():
    if not is_admin(session["role"]):
        return redirect(url_for("artwork_page"))
    db = get_db()
    try:
        types = db.execute("SELECT * FROM doc_types ORDER BY sort_order, id").fetchall()
    finally:
        db.close()
    return render_template("doc_types.html",
                           doc_types=[dict(t) for t in types],
                           username=session["username"],
                           role=session["role"])


@app.route("/api/doc-types")
@login_required
def api_doc_types_list():
    """Zwraca aktywne typy dokumentów — używane przez analyze.html."""
    db = get_db()
    try:
        types = db.execute(
            "SELECT code, label, icon, description FROM doc_types WHERE active=1 ORDER BY sort_order"
        ).fetchall()
    finally:
        db.close()
    return jsonify([dict(t) for t in types])


@app.route("/api/doc-types/all")
@login_required
def api_doc_types_all():
    """Wszystkie typy (łącznie z nieaktywnymi) — dla panelu admin."""
    db = get_db()
    try:
        types = db.execute("SELECT * FROM doc_types ORDER BY sort_order").fetchall()
    finally:
        db.close()
    return jsonify([dict(t) for t in types])


@app.route("/api/doc-types", methods=["POST"])
@require_role("admin")
@csrf_protect
def api_doc_types_add():
    """Dodaje nowy typ dokumentu."""
    data = request.get_json(silent=True)
    if not data or not data.get("code") or not data.get("label"):
        return jsonify({"error": "Wymagane: code, label"}), 400
    code = str(data["code"]).upper().strip()
    if not code or len(code) > 12:
        return jsonify({"error": "Kod dokumentu może mieć maksymalnie 12 znaków"}), 400
    db = get_db()
    try:
        # Pobierz max sort_order
        max_order = db.execute("SELECT COALESCE(MAX(sort_order),0) FROM doc_types").fetchone()[0]
        try:
            db.execute(
                "INSERT INTO doc_types(code, label, icon, description, active, sort_order) VALUES(?,?,?,?,?,?)",
                (code, str(data["label"])[:100], str(data.get("icon") or "📄")[:10], str(data.get("description") or "")[:500], 1, max_order+1)
            )
            db.commit()
            row = db.execute("SELECT * FROM doc_types WHERE code=?", (code,)).fetchone()
            return jsonify({"ok": True, "type": dict(row)})
        except Exception as e:
            logger.exception("doc-types error: %s", e)
            return jsonify({"error": "Błąd zapisu — spróbuj ponownie."}), 400
    finally:
        db.close()


@app.route("/api/doc-types/<int:tid>", methods=["PATCH"])
@require_role("admin")
@csrf_protect
def api_doc_types_update(tid):
    """Aktualizuje typ dokumentu (label, icon, description, active, sort_order)."""
    data = request.get_json(silent=True) or {}
    db = get_db()
    try:
        row = db.execute("SELECT * FROM doc_types WHERE id=?", (tid,)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono"}), 404

        allowed = ["label", "icon", "description", "active", "sort_order"]
        _dt_max_lens = {"label": 100, "icon": 10, "description": 500}
        updates, params = [], []
        for field in allowed:
            if field in data:
                v = data[field]
                if field == "active":
                    v = 1 if v else 0
                elif field == "sort_order":
                    try:
                        v = int(v)
                    except (TypeError, ValueError):
                        continue
                elif field in _dt_max_lens:
                    v = str(v or "")[:_dt_max_lens[field]]
                updates.append(f"{field}=?")
                params.append(v)
        if not updates:
            return jsonify({"error": "Brak pól do aktualizacji"}), 400

        params.append(tid)
        # Bandit B608: nazwy kolumn z twardej białej listy w kodzie; wartości jako parametry ?.
        db.execute(f"UPDATE doc_types SET {', '.join(updates)} WHERE id=?", params)  # nosec B608
        db.commit()
        updated = db.execute("SELECT * FROM doc_types WHERE id=?", (tid,)).fetchone()
        return jsonify({"ok": True, "type": dict(updated)})
    finally:
        db.close()


@app.route("/api/doc-types/<int:tid>", methods=["DELETE"])
@require_role("admin")
@csrf_protect
def api_doc_types_delete(tid):
    """Usuwa typ dokumentu (tylko jeśli nie jest wbudowany)."""
    db = get_db()
    try:
        row = db.execute("SELECT code FROM doc_types WHERE id=?", (tid,)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono"}), 404
        # Chroń typy wbudowane
        builtin = {"auto","PO","PI","CI","PL","SAD","BL","WZ","FV","MULTI","CMR","SWIFT"}
        if row["code"] in builtin:
            return jsonify({"error": "Nie można usunąć wbudowanego typu — wyłącz go zamiast usuwać"}), 400
        db.execute("DELETE FROM doc_types WHERE id=?", (tid,))
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True})


@app.route("/api/doc-types/reorder", methods=["POST"])
@require_role("admin")
@csrf_protect
def api_doc_types_reorder():
    """Ustawia kolejność — przyjmuje listę {id, sort_order}."""
    data = request.get_json(silent=True) or {}
    items = data.get("items", [])
    db = get_db()
    try:
        for item in items:
            sid = item.get("id")
            sorder = item.get("sort_order")
            if sid is None or sorder is None:
                continue
            try:
                _sid = int(sid)
                _sorder = int(sorder)
            except (TypeError, ValueError):
                continue
            db.execute("UPDATE doc_types SET sort_order=? WHERE id=?",
                       (_sorder, _sid))
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES — EKSPORT (PDF / EXCEL)
# ─────────────────────────────────────────────────────────────────────────────

def _supplier_doc_config(mapping: dict, required=("ref", "qty", "price")) -> dict:
    """Analizuje mapowanie kolumn jednego doc-type. Zwraca {field_count, roles, ok, partial}."""
    if not mapping or not isinstance(mapping, dict):
        return {"field_count": 0, "roles": [], "ok": False, "partial": False}
    roles = set(v for v in mapping.values() if v and v != "skip")
    field_count = len(roles)
    has_all = all(r in roles for r in required)
    has_ref = "ref" in roles
    return {
        "field_count": field_count,
        "roles": sorted(roles),
        "ok": has_all,
        "partial": has_ref and not has_all,
    }


@app.route("/suppliers")
@login_required
def suppliers_page():
    from supplier_profiles import get_all_suppliers
    import json as _json
    suppliers_raw = get_all_suppliers()

    db = get_db()
    try:
        comp_rows = db.execute(
            "SELECT supplier_code, COUNT(*) as cnt FROM comparisons "
            "WHERE supplier_code IS NOT NULL AND supplier_code != '' "
            "GROUP BY supplier_code"
        ).fetchall()
    finally:
        db.close()
    comp_counts = {r["supplier_code"]: r["cnt"] for r in comp_rows}

    suppliers = []
    for s in suppliers_raw:
        s2 = dict(s)
        # Deserializuj pola JSON jeśli nadal string
        for fld in ["pi_column_mapping_json", "po_column_mapping_json",
                    "sad_column_mapping_json",
                    "detect_keywords_json", "payment_terms_json",
                    "ignore_fields_json", "custom_rules_json",
                    "column_mapping_json", "known_issues_json"]:
            v = s2.get(fld)
            if isinstance(v, str) and v:
                try:
                    s2[fld] = _json.loads(v)
                except Exception:
                    s2[fld] = {} if "{" in (v or "") else []

        # Fallback: wyciągnij PI/PO mapping z column_mapping_json dla starych profili
        col_map = s2.get("column_mapping_json") or {}
        for dtype, key in [("PI","pi_column_mapping_json"),("PO","po_column_mapping_json"),
                           ("SAD","sad_column_mapping_json"),("CI","ci_column_mapping_json"),
                           ("PL","pl_column_mapping_json")]:
            if not s2.get(key) and isinstance(col_map, dict):
                s2[key] = col_map.get(dtype, col_map.get(dtype.lower(), {}))

        kw = s2.get("detect_keywords_json")
        if not isinstance(kw, list):
            s2["detect_keywords_json"] = [kw] if isinstance(kw, str) and kw else []
        ki = s2.get("known_issues_json")
        if not isinstance(ki, list):
            s2["known_issues_json"] = []

        # Per-doc-type config analysis
        s2["_doc_config"] = {
            "PI":  _supplier_doc_config(s2.get("pi_column_mapping_json") or {}),
            "PO":  _supplier_doc_config(s2.get("po_column_mapping_json") or {}),
            "SAD": _supplier_doc_config(s2.get("sad_column_mapping_json") or {},
                                        required=("ref", "tariff_code")),
            "CI":  _supplier_doc_config(s2.get("ci_column_mapping_json") or {}),
            "PL":  _supplier_doc_config(s2.get("pl_column_mapping_json") or {}),
        }
        # Overall config status
        pi_ok = s2["_doc_config"]["PI"]["ok"]
        po_ok = s2["_doc_config"]["PO"]["ok"]
        any_mapped = any(v["field_count"] > 0 for v in s2["_doc_config"].values())
        s2["_config_status"] = "full" if (pi_ok and po_ok) else ("partial" if any_mapped else "none")

        # Comparison count
        s2["comparison_count"] = comp_counts.get(s2.get("code", ""), 0)

        suppliers.append(s2)

    return render_template("suppliers.html",
                           suppliers=suppliers,
                           username=session["username"],
                           role=session["role"],
                           is_admin=is_admin(session["role"]))


# ── Checklists ──────────────────────────────────────────────────────────────

@app.route("/checklists")
@login_required
def checklists_page():
    db = get_db()
    try:
        # All delivery checklists grouped by PO
        deliveries = db.execute("""
            SELECT po_number, supplier_name, container_number,
                   COUNT(*) as total_docs,
                   SUM(CASE WHEN status='zatwierdzono' THEN 1 ELSE 0 END) as approved,
                   SUM(CASE WHEN status='brak' THEN 1 ELSE 0 END) as missing,
                   MAX(updated_at) as last_update
            FROM delivery_checklists
            GROUP BY po_number, supplier_name, container_number
            ORDER BY last_update DESC LIMIT 100
        """).fetchall()
        deliveries = [dict(r) for r in deliveries]

        # Supplier checklist templates
        templates_raw = db.execute("""
            SELECT * FROM supplier_checklists ORDER BY supplier_name, doc_type
        """).fetchall()
        templates = [dict(r) for r in templates_raw]

        # Recent comparisons for quick import
        recent_pos = db.execute("""
            SELECT DISTINCT po_number, supplier_code, created_at
            FROM comparisons
            WHERE po_number != '' AND po_number IS NOT NULL
            AND (is_deleted IS NULL OR is_deleted=0)
            ORDER BY created_at DESC LIMIT 30
        """).fetchall()
        recent_pos = [dict(r) for r in recent_pos]

        try:
            suppliers = db.execute("SELECT id, name FROM suppliers ORDER BY name").fetchall()
            suppliers = [dict(s) for s in suppliers]
        except Exception:
            suppliers = []
    finally:
        db.close()
    return render_template("checklists.html",
                           deliveries=deliveries,
                           templates=templates,
                           recent_pos=recent_pos,
                           suppliers=suppliers,
                           username=session["username"],
                           role=session["role"])


@app.route("/api/checklists/delivery", methods=["POST"])
@login_required
@csrf_protect
def api_checklist_create():
    data = request.get_json(silent=True) or {}
    po = str(data.get("po_number") or "").strip()[:100]
    if not po:
        return jsonify({"error": "Numer PO jest wymagany"}), 400
    docs = data.get("docs", [])
    if not docs:
        # Default doc types
        docs = ["PO", "PI", "CI", "PL", "SAD", "BL"]
    _VALID_DOC_TYPES_CL = {"PO", "PI", "CI", "PL", "SAD", "BL", "CMR", "COO", "COA", "INV", "PACK", "BL", "AWB"}
    uid = session["user_id"]
    db = get_db()
    try:
        for doc in docs:
            doc = str(doc).strip().upper()[:20]
            if doc not in _VALID_DOC_TYPES_CL:
                continue
            db.execute(
                "INSERT INTO delivery_checklists(po_number, supplier_name, container_number, doc_type, status, created_by) "
                "VALUES(?,?,?,?,?,?)",
                (po, str(data.get("supplier_name") or "")[:200], str(data.get("container_number") or "")[:100], doc, "brak", uid)
            )
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True})


@app.route("/api/checklists/delivery/<po>")
@login_required
def api_checklist_get(po):
    db = get_db()
    try:
        items = db.execute(
            "SELECT * FROM delivery_checklists WHERE po_number=? ORDER BY doc_type",
            (po,)
        ).fetchall()
        return jsonify({"items": [dict(r) for r in items]})
    finally:
        db.close()


@app.route("/api/checklists/delivery/<int:did>", methods=["PATCH"])
@require_role("manager")
@csrf_protect
def api_checklist_update(did):
    data = request.get_json(silent=True) or {}
    _VALID_CL_STATUSES = {"brak", "oczekuje", "wgrano", "zatwierdzono"}
    allowed = ["status", "notes", "file_path", "comparison_id", "due_date"]
    _cl_max_lens = {"status": 50, "notes": 500, "file_path": 500, "due_date": 20}
    fields, vals = [], []
    for k in allowed:
        if k in data:
            v = data[k]
            if k == "status":
                v = str(v or "").strip()
                if v not in _VALID_CL_STATUSES:
                    continue
            elif k == "comparison_id":
                # Wymuś int/None — inaczej dict/list z JSON trafiłby surowy do SQL (500).
                try:
                    v = int(v) if v not in (None, "") else None
                except (TypeError, ValueError):
                    continue
            elif k in _cl_max_lens:
                v = str(v or "")[:_cl_max_lens[k]]
            fields.append(f"{k}=?")
            vals.append(v)
    if not fields:
        return jsonify({"error": "Brak pól"}), 400
    fields.append("updated_at=datetime('now')")
    vals.append(did)
    db = get_db()
    try:
        # Bandit B608: nazwy kolumn z twardej białej listy w kodzie; wartości jako parametry ?.
        db.execute(f"UPDATE delivery_checklists SET {', '.join(fields)} WHERE id=?", vals)  # nosec B608
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/checklists/templates", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_checklist_template_create():
    data = request.get_json(silent=True) or {}
    supplier = str(data.get("supplier_name") or "").strip()
    doc_type = str(data.get("doc_type") or "").strip().upper()
    if not supplier or not doc_type:
        return jsonify({"error": "Dostawca i typ dokumentu są wymagane"}), 400
    try:
        required = int(data.get("required", 1))
    except (ValueError, TypeError):
        required = 1
    required = 1 if required else 0
    db = get_db()
    try:
        db.execute(
            "INSERT INTO supplier_checklists(supplier_name, doc_type, required, notes) VALUES(?,?,?,?)",
            (supplier[:200], doc_type[:12], required, str(data.get("notes") or "")[:500])
        )
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True})


@app.route("/api/checklists/templates/<int:tid>", methods=["DELETE"])
@require_role("manager")
@csrf_protect
def api_checklist_template_delete(tid):
    db = get_db()
    try:
        db.execute("DELETE FROM supplier_checklists WHERE id=?", (tid,))
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True})


@app.route("/api/suppliers")
@login_required
def api_suppliers_list():
    from supplier_profiles import get_all_suppliers
    return jsonify(get_all_suppliers())


@app.route("/api/suppliers", methods=["POST"])
@require_role("admin")
@csrf_protect
def api_suppliers_add():
    from supplier_profiles import save_supplier
    data = request.get_json(silent=True)
    if not data or not data.get("code") or not data.get("name"):
        return jsonify({"error": "Wymagane: code, name"}), 400
    try:
        result = save_supplier(data)
        return jsonify({"ok": True, "supplier": result})
    except Exception as e:
        logger.exception("save_supplier error: %s", e)
        return jsonify({"error": "Błąd zapisu dostawcy — sprawdź dane."}), 400


@app.route("/api/suppliers/<int:sid>", methods=["PATCH"])
@require_role("admin")
@csrf_protect
def api_suppliers_update(sid):
    from supplier_profiles import get_all_suppliers, save_supplier
    import sqlite3 as _sq
    db = get_db()
    try:
        row = db.execute("SELECT * FROM suppliers WHERE id=?", (sid,)).fetchone()
    finally:
        db.close()
    if not row:
        return jsonify({"error": "Nie znaleziono"}), 404
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Brak danych JSON"}), 400
    data["code"] = dict(row)["code"]  # zachowaj oryginalny kod
    try:
        result = save_supplier(data)
        return jsonify({"ok": True, "supplier": result})
    except Exception as e:
        logger.exception("update_supplier error: %s", e)
        return jsonify({"error": "Błąd aktualizacji dostawcy — sprawdź dane."}), 400


@app.route("/api/suppliers/<int:sid>", methods=["DELETE"])
@require_role("admin")
@csrf_protect
def api_suppliers_delete(sid):
    from supplier_profiles import delete_supplier
    ok = delete_supplier(sid)
    if not ok:
        return jsonify({"error": "Nie można usunąć wbudowanego profilu"}), 400
    return jsonify({"ok": True})


@app.route("/api/suppliers/detect", methods=["POST"])
@login_required
@csrf_protect
def api_suppliers_detect():
    """Wykrywa dostawcę z tekstu PDF."""
    data = request.get_json(silent=True) or {}
    text = data.get("text", "")
    if not text:
        return jsonify({"supplier": None})
    from supplier_profiles import detect_supplier
    s = detect_supplier(text)
    return jsonify({"supplier": s})


# ─────────────────────────────────────────────────────────────────────────────
# WIZARD PROFILI DOSTAWCÓW
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/suppliers/wizard/new")
@require_role("admin")
def supplier_wizard_new():
    return render_template("supplier_wizard.html",
                           mode="new", supplier=None, supplier_id=None,
                           supplier_name="",
                           username=session["username"],
                           role=session["role"])


@app.route("/suppliers/wizard/<int:sid>")
@require_role("admin")
def supplier_wizard_edit(sid):
    db = get_db()
    try:
        row = db.execute("SELECT * FROM suppliers WHERE id=?", (sid,)).fetchone()
    finally:
        db.close()
    if not row:
        return "Nie znaleziono dostawcy", 404
    s = dict(row)
    # Deserializuj JSON pola
    import json as _json
    for fld in ["pi_column_mapping_json", "po_column_mapping_json",
                "sad_column_mapping_json",
                "detect_keywords_json", "payment_terms_json",
                "ignore_fields_json", "custom_rules_json"]:
        if s.get(fld) and isinstance(s[fld], str):
            try:
                s[fld] = _json.loads(s[fld])
            except Exception:
                pass
    # Zbuduj payment_terms_text
    pt = s.get("payment_terms_json") or {}
    if isinstance(pt, dict):
        s["payment_terms_text"] = "\n".join(f"{k}={v}" for k,v in pt.items())
    else:
        s["payment_terms_text"] = ""
    # ignore_fields jako string
    ig = s.get("ignore_fields_json") or []
    s["ignore_fields"] = ", ".join(ig) if isinstance(ig, list) else str(ig)
    return render_template("supplier_wizard.html",
                           mode="edit", supplier=s,
                           supplier_id=sid,
                           supplier_name=s.get("name",""),
                           username=session["username"],
                           role=session["role"])


@app.route("/api/suppliers/preview_table", methods=["POST"])
@require_role("admin")
@csrf_protect
def api_suppliers_preview_table():
    """
    Przyjmuje plik PDF, zwraca podgląd tabeli produktów z auto-detekcją kolumn.
    Używany przez wizard mapowania.
    """
    import traceback
    import tempfile, os
    file = request.files.get("file")
    side = request.form.get("side", "pi")  # 'pi' lub 'po'
    if not file:
        return jsonify({"error": "Brak pliku"}), 400
    err = _validate_pdf_upload(file)
    if err:
        return jsonify({"error": err}), 400
    # Zapisz tymczasowo
    suffix = ".pdf"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name
    try:
        from supplier_profiles import extract_table_preview
        result = extract_table_preview(tmp_path, doc_type=side.upper())
        # Dodaj przykłady REF jeśli znaleziono kolumnę ref
        if result.get("table_found") and result.get("rows"):
            ref_col = None
            dm = result.get("detected_mappings", {})
            # Znajdź indeks kolumny ref
            for hdr, role in dm.items():
                if role == "ref":
                    try:
                        ref_col = result["headers"].index(hdr)
                    except ValueError:
                        pass
                    break
            if ref_col is not None:
                samples = []
                for row in result["rows"][:4]:
                    if ref_col < len(row) and row[ref_col]:
                        samples.append(str(row[ref_col])[:30])
                result["ref_samples"] = samples
        return jsonify(result)
    except Exception as e:
        tb = traceback.format_exc()
        app.logger.error("preview_table error: %s", tb)
        return jsonify({"error": "Błąd podglądu tabeli.", "table_found": False}), 500
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


@app.route("/api/suppliers/<int:sid>/audit")
@require_role("admin")
def api_suppliers_audit(sid):
    """Historia zmian profilu dostawcy."""
    from supplier_profiles import get_audit_log
    log = get_audit_log(sid)
    # Dodaj pole 'summary' dla template
    result = []
    for entry in log:
        diff = entry.get('diff_json', {})
        if isinstance(diff, dict):
            keys = list(diff.keys())[:3]
            summary = ', '.join(keys) + (' …' if len(diff) > 3 else '')
        else:
            summary = str(diff)[:80]
        result.append({
            'changed_at': entry.get('changed_at',''),
            'changed_by': entry.get('changed_by','system'),
            'action':     entry.get('action','update'),
            'summary':    summary,
            'changes_json': diff,
        })
    return jsonify(result)


@app.route("/api/suppliers/<int:sid>/stats")
@login_required
def api_suppliers_stats(sid):
    """Statystyki użycia profilu — ile porównań, % błędów."""
    db = get_db()
    try:
        row = db.execute("SELECT * FROM suppliers WHERE id=?", (sid,)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono"}), 404
        supplier_code = dict(row).get("code", "")
        try:
            stats = db.execute(
                """SELECT COUNT(*) as total,
                          SUM(CASE WHEN diff_count > 0 THEN 1 ELSE 0 END) as with_errors,
                          MAX(created_at) as last_used
                   FROM comparisons
                   WHERE supplier_code=? AND doc_type != 'artwork'""",
                (supplier_code,)
            ).fetchone()
            total = stats["total"] or 0
            errors = stats["with_errors"] or 0
            return jsonify({
                "total_comparisons": total,
                "error_rate_pct": round(errors / total * 100, 1) if total else 0,
                "last_used": stats["last_used"],
            })
        except Exception:
            return jsonify({"total_comparisons": 0, "error_rate_pct": 0, "last_used": None})
    finally:
        db.close()


# ════════════════════════════════════════════════════════════
# ARTWORK COMPARATOR + AI ENDPOINTS
# ════════════════════════════════════════════════════════════

from collections import namedtuple as _namedtuple
_CacheEntry = _namedtuple("_CacheEntry", "result inserted_at owner_id")

_artwork_cache: dict = {}        # {cache_id: _CacheEntry}
_cache_lock = threading.RLock()  # protects _artwork_cache from concurrent access
_rpt_cache_lock = threading.Lock()  # protects app._artwork_report_cache
_CACHE_TTL = 7200               # 2 hours in seconds
_CACHE_MAX = 50


def _owner_forbidden(owner_id, uid, role, is_public=False) -> bool:
    """True gdy `uid` NIE ma dostępu do zasobu należącego do `owner_id`. Wpisy cache
    zawsze mają owner_id (None = nieznany → traktowany jak prywatny); is_public oraz
    can_see_all omijają kontrolę. Jedno źródło prawdy dla bramek IDOR artworku."""
    if is_public:
        return False
    return owner_id != uid and not can_see_all(role)


def _report_owner(report):
    """(owner_id, is_public) z raportu — działa na dict i na obiekcie ArtworkReport."""
    if isinstance(report, dict):
        return report.get("_owner_id"), report.get("_is_public")
    return getattr(report, "_owner_id", None), getattr(report, "_is_public", None)


def _tag_report_owner(report, uid) -> None:
    """Otaguj raport właścicielem (dla kontroli dostępu) — dict lub obiekt."""
    try:
        if isinstance(report, dict):
            report["_owner_id"] = uid
        else:
            setattr(report, "_owner_id", uid)
    except Exception:
        pass


def _cache_put(cache_id: str, result, owner_id=None) -> None:
    with _cache_lock:
        _artwork_cache[cache_id] = _CacheEntry(result, time.time(), owner_id)
        if len(_artwork_cache) > _CACHE_MAX:
            oldest = min(_artwork_cache, key=lambda k: _artwork_cache[k].inserted_at)
            _artwork_cache.pop(oldest, None)

def _cache_get(cache_id: str):
    with _cache_lock:
        entry = _artwork_cache.get(cache_id)
        if entry and (time.time() - entry.inserted_at) < _CACHE_TTL:
            return entry.result
        if entry:
            del _artwork_cache[cache_id]
    return None

def _cache_get_owner(cache_id: str):
    """Return the owner user_id stored for a cache entry, or None if absent/expired."""
    with _cache_lock:
        entry = _artwork_cache.get(cache_id)
        if entry and (time.time() - entry.inserted_at) < _CACHE_TTL:
            return entry.owner_id
    return None

def _cache_evict(cache_id: str) -> None:
    """Remove a specific entry (e.g. on corrupt build)."""
    with _cache_lock:
        _artwork_cache.pop(cache_id, None)

def _cache_cleanup():
    while True:
        time.sleep(1800)
        now = time.time()
        with _cache_lock:
            expired = [k for k, v in list(_artwork_cache.items())
                       if (now - v.inserted_at) >= _CACHE_TTL]
            for k in expired:
                _artwork_cache.pop(k, None)

threading.Thread(target=_cache_cleanup, daemon=True).start()

# Optional background warm-up of the heavy artwork models (DINOv2 + LightGlue) so
# the first real comparison doesn't pay their cold-start. Off by default (boot
# stays fast / no extra memory unless wanted); enable with ARTWORK_WARMUP=1 on
# artwork-heavy deployments. Runs in a daemon thread — never blocks startup.
if os.environ.get("ARTWORK_WARMUP", "0").strip().lower() in ("1", "true", "yes", "on"):
    def _warmup_artwork_models():
        try:
            from artwork_comparator import warmup_models
            warmup_models()
        except Exception as _e:
            app.logger.warning("artwork warmup failed: %s", _e)
    threading.Thread(target=_warmup_artwork_models, daemon=True).start()

_HEAVY_SEM = threading.Semaphore(3)  # max 3 concurrent heavy comparisons


# ═══════════════════════════════════════════════════════════════════════════════
# MODUŁ 3: SKANY DOSTAW — EKSTRAKCJA DANYCH AI
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/artwork")
@login_required
def artwork_page():
    return render_template("artwork.html",
                           username=session["username"],
                           role=session["role"],
                           can_see_all=can_see_all(session["role"]))


@app.route("/artwork/library")
@login_required
def artwork_library_page():
    """Biblioteka 3D — ta sama strona co /artwork, ale auto-otwiera przeglądarkę
    biblioteki (3D / grupy / render z REF) bez przechodzenia przez „wybierz wzorzec A"."""
    return render_template("artwork.html",
                           username=session["username"],
                           role=session["role"],
                           can_see_all=can_see_all(session["role"]),
                           auto_open_library=True)


@app.route("/artwork/history")
@login_required
def artwork_history_page():
    uid  = session["user_id"]
    role = session["role"]
    per_page = 50
    try:
        page = max(1, min(10000, int(request.args.get("page", 1))))
    except (ValueError, TypeError):
        page = 1
    offset = (page - 1) * per_page
    search = request.args.get("q", "")[:200].strip()
    status_filter = request.args.get("status", "").strip()
    if status_filter not in ("ok", "warning", "error", "critical", ""):
        status_filter = ""

    conditions = ["c.doc_type = 'artwork'", "(c.is_deleted IS NULL OR c.is_deleted=0)"]
    params: list = []
    if not can_see_all(role):
        conditions.append("(c.user_id = ? OR c.is_public = 1)")
        params.append(uid)
    if search:
        conditions.append("(c.file_a LIKE ? ESCAPE '\\' OR c.file_b LIKE ? ESCAPE '\\' OR u.username LIKE ? ESCAPE '\\')")
        esc = search.replace("\\","\\\\").replace("%","\\%").replace("_","\\_")
        like = f"%{esc}%"
        params.extend([like, like, like])
    if status_filter:
        conditions.append("c.status = ?")
        params.append(status_filter)

    where = "WHERE " + " AND ".join(conditions)

    db = get_db()
    try:
        # Single aggregate query for stats — avoids N+1
        stats_row = db.execute(
            # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
            f"""SELECT COUNT(*) as total,
                       SUM(CASE WHEN c.status IN ('critical','error') THEN 1 ELSE 0 END) as critical,
                       SUM(CASE WHEN c.status='ok' THEN 1 ELSE 0 END) as ok_cnt,
                       COUNT(DISTINCT c.user_id) as users
                FROM comparisons c JOIN users u ON c.user_id = u.id {where}""",  # nosec B608
            params
        ).fetchone()

        total_count = stats_row["total"] or 0
        stats = {
            "total": total_count,
            "critical": stats_row["critical"] or 0,
            "ok": stats_row["ok_cnt"] or 0,
            "users": stats_row["users"] or 0,
        }

        rows = db.execute(
            # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
            f"""SELECT c.id, c.file_a, c.file_b, c.status, c.diff_count,
                       c.created_at, c.user_id, c.is_public, u.username
                FROM comparisons c JOIN users u ON c.user_id = u.id
                {where}
                ORDER BY c.created_at DESC LIMIT ? OFFSET ?""",  # nosec B608
            params + [per_page, offset]
        ).fetchall()

        rows = [dict(r) | {"db_id": r["id"]} for r in rows]
        all_users = list(dict.fromkeys(r["username"] for r in rows))
        total_pages = max(1, (total_count + per_page - 1) // per_page)

        return render_template("artwork_history.html",
                               username=session["username"],
                               role=role,
                               rows=rows,
                               stats=stats,
                               all_users=all_users,
                               can_see_all=can_see_all(role),
                               current_uid=uid,
                               page=page,
                               total_pages=total_pages,
                               total_count=total_count,
                               per_page=per_page,
                               search=search,
                               status_filter=status_filter)
    finally:
        db.close()


@app.route("/artwork/kpi")
@login_required
def artwork_kpi_page():
    from datetime import datetime as _dt, timedelta, timezone as _tz_dt
    uid = session["user_id"]
    role = session.get("role", "user")
    db = get_db()
    try:
        if can_see_all(role):
            all_rows = db.execute("""
                SELECT c.status, c.diff_count, c.created_at, u.username, u.role
                FROM comparisons c JOIN users u ON c.user_id = u.id
                WHERE c.doc_type = 'artwork'
                ORDER BY c.created_at DESC
            """).fetchall()
        else:
            all_rows = db.execute("""
                SELECT c.status, c.diff_count, c.created_at, u.username, u.role
                FROM comparisons c JOIN users u ON c.user_id = u.id
                WHERE c.doc_type = 'artwork' AND c.user_id = ?
                ORDER BY c.created_at DESC
            """, (uid,)).fetchall()
        all_rows = [dict(r) for r in all_rows]

        total = len(all_rows)
        now = _dt.now(_tz_dt.utc)
        month_str = now.strftime("%Y-%m")
        this_month = sum(1 for r in all_rows if str(r.get("created_at") or "").startswith(month_str))
        total_errors = sum((r.get("diff_count") or 0) for r in all_rows)
        ok_count = sum(1 for r in all_rows if str(r.get("status") or "").lower() == "ok")
        ok_rate = round(ok_count / total * 100) if total else 0

        # Per-user stats
        from collections import defaultdict
        user_map = defaultdict(lambda: {"count": 0, "last_activity": "", "role": "user"})
        for r in all_rows:
            u = r["username"]
            user_map[u]["count"] += 1
            user_map[u]["role"] = r.get("role", "user")
            dt = r.get("created_at") or ""
            if dt > user_map[u]["last_activity"]:
                user_map[u]["last_activity"] = dt
        max_c = max((v["count"] for v in user_map.values()), default=1)
        user_stats = sorted([
            {"username": u, "count": v["count"], "role": v["role"],
             "last_activity": v["last_activity"],
             "pct": round(v["count"] / max_c * 100)}
            for u, v in user_map.items()
        ], key=lambda x: -x["count"])

        # Status breakdown
        from collections import Counter
        status_c = Counter(str(r.get("status") or "ok").lower() for r in all_rows)
        status_breakdown = [
            {"status": s, "count": c, "pct": round(c / max(total, 1) * 100)}
            for s, c in status_c.most_common()
        ]

        # Weekly (last 7 days)
        weekly = []
        for i in range(6, -1, -1):
            day = now - timedelta(days=i)
            day_str = day.strftime("%Y-%m-%d")
            cnt = sum(1 for r in all_rows if str(r.get("created_at") or "").startswith(day_str))
            weekly.append({"label": day.strftime("%d.%m"), "count": cnt})

        kpi = {
            "total": total, "this_month": this_month,
            "total_errors": total_errors, "ok_rate": ok_rate,
            "month_name": now.strftime("%B %Y"),
            "user_stats": user_stats,
            "status_breakdown": status_breakdown,
            "weekly": weekly,
        }
        return render_template("artwork_kpi.html",
                               username=session["username"],
                               role=session["role"],
                               kpi=kpi)
    finally:
        db.close()


@app.route("/artwork/design-preview")
@login_required
def artwork_design_preview():
    from flask import send_from_directory as _sfd
    import os as _os
    design_dir = _os.path.join(app.root_path, "static", "design")
    return _sfd(design_dir, "index.html")


@app.route("/artwork/design-preview/<path:filename>")
@login_required
def artwork_design_preview_assets(filename):
    from flask import send_file as _sf, abort as _abort
    from werkzeug.utils import safe_join as _safe_join
    import os as _os
    design_dir = _os.path.join(app.root_path, "static", "design")
    safe_path = _safe_join(design_dir, filename)
    if not safe_path or not _os.path.isfile(safe_path):
        _abort(404)
    return _sf(safe_path)


@app.route("/artwork/admin")
@login_required
def artwork_admin_page():
    if not can_see_all(session["role"]):
        return redirect(url_for("artwork_page"))
    db = get_db()
    try:
        users = db.execute("""
            SELECT u.id, u.username, u.email, u.role, u.created_at,
                   COUNT(c.id) as cmp_count
            FROM users u
            LEFT JOIN comparisons c ON c.user_id = u.id AND c.doc_type = 'artwork'
            GROUP BY u.id ORDER BY cmp_count DESC
        """).fetchall()
        users = [dict(r) for r in users]

        recent = db.execute("""
            SELECT c.file_a, c.file_b, c.status, c.diff_count, c.created_at, u.username
            FROM comparisons c JOIN users u ON c.user_id = u.id
            WHERE c.doc_type = 'artwork'
            ORDER BY c.created_at DESC LIMIT 15
        """).fetchall()
        recent = [dict(r) for r in recent]

        audit = db.execute("""
            SELECT event, username, detail, created_at
            FROM audit_log ORDER BY created_at DESC LIMIT 20
        """).fetchall()
        audit = [dict(r) for r in audit]

        from datetime import datetime as _dt, timezone as _tz
        today = _dt.now(_tz.utc).strftime("%Y-%m-%d")
        active_today = db.execute(
            "SELECT COUNT(DISTINCT user_id) FROM comparisons WHERE doc_type='artwork' AND created_at LIKE ?",
            (today + "%",)
        ).fetchone()[0]

        admin = {
            "total_users": len(users),
            "total_comparisons": db.execute("SELECT COUNT(*) FROM comparisons WHERE doc_type='artwork'").fetchone()[0],
            "active_today": active_today,
            "users": users,
            "recent": recent,
            "audit_log": audit,
        }
        return render_template("artwork_admin.html",
                               username=session["username"],
                               role=session["role"],
                               admin=admin)
    finally:
        db.close()


@app.route("/api/artwork/preview-crops", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_preview_crops():
    """Extract field crops for preview/validation before running the actual comparison.

    Accepts the same FormData as split-compare (supplier_pdf, masters[],
    master_configs_json, pairs_json) but only renders and crops — no diff analysis.

    Returns:
      {pairs: [{pair_idx, master_name, profile_id, fields: [...], page_b_b64, page_b_w, page_b_h}]}
    """
    import traceback as _tb
    supplier_path = None
    master_paths = []
    comp_paths = []
    try:
        supplier_file = request.files.get("supplier_pdf")
        masters_files = request.files.getlist("masters[]")
        pairs_json_str  = request.form.get("pairs_json", "")
        master_configs_str = request.form.get("master_configs_json", "")

        if not supplier_file:
            return jsonify({"error": "Wymagany plik PDF dostawcy"}), 400

        uid = session["user_id"]
        upload_dir = app.config["UPLOAD_FOLDER"]
        os.makedirs(upload_dir, exist_ok=True)

        supplier_path = os.path.join(upload_dir,
                                     f"{uid}_pv_sup_{secure_filename(supplier_file.filename) or 'supplier.pdf'}")
        supplier_file.save(supplier_path)

        # Build master_paths (same logic as split-compare)
        if master_configs_str:
            try:
                master_configs = json.loads(master_configs_str)
            except Exception:
                return jsonify({"error": "Błąd parsowania master_configs_json"}), 400
            db_mc = get_db()
            _ensure_artwork_profile_tables(db_mc)
            try:
                for cfg in master_configs:
                    if cfg.get("type") == "profile":
                        pid = cfg.get("profile_id")
                        if not pid:
                            return jsonify({"error": "Brak profile_id w konfiguracji mastera"}), 400
                        row = db_mc.execute(
                            "SELECT name, master_pdf_path FROM artwork_profiles WHERE id=?", (pid,)
                        ).fetchone()
                        if not row or not row["master_pdf_path"]:
                            return jsonify({"error": f"Master z biblioteki (id={pid}) nie ma pliku PDF"}), 404
                        resolved = _resolve_master_path(row["master_pdf_path"], profile_id=pid, db=db_mc)
                        if not os.path.exists(resolved):
                            return jsonify({"error": f"Plik mastera nie istnieje: {resolved}"}), 404
                        master_paths.append({"name": row["name"], "path": resolved, "profile_id": pid, "is_temp": False})
                    else:
                        fi = cfg.get("file_idx")
                        if fi is None or not isinstance(fi, int) or fi < 0 or fi >= len(masters_files):
                            return jsonify({"error": "Brakujący plik mastera"}), 400
                        mf = masters_files[fi]
                        mp = os.path.join(upload_dir, f"{uid}_pv_mst_{len(master_paths)}_{secure_filename(mf.filename or '') or 'master.pdf'}")
                        mf.save(mp)
                        master_paths.append({"name": mf.filename, "path": mp, "profile_id": None, "is_temp": True})
            finally:
                db_mc.close()
        else:
            for i, mf in enumerate(masters_files):
                mp = os.path.join(upload_dir, f"{uid}_pv_mst_{i}_{secure_filename(mf.filename or '') or 'master.pdf'}")
                mf.save(mp)
                master_paths.append({"name": mf.filename, "path": mp, "profile_id": None, "is_temp": True})

        if pairs_json_str:
            try:
                pairs = json.loads(pairs_json_str)
            except Exception:
                return jsonify({"error": "Błąd parsowania pairs_json"}), 400
        else:
            pairs = [{"master_idx": i, "page": i} for i in range(len(master_paths))]

        from artwork_comparator import extract_field_crops_for_preview, _load_artwork_profile_by_id
        import fitz as _fitz_pv
        from pypdf import PdfReader, PdfWriter

        doc = _fitz_pv.open(supplier_path)
        reader = None
        try:
            reader = PdfReader(supplier_path)
        except Exception:
            pass

        result_pairs = []
        try:
            for i, pair in enumerate(pairs):
                try:
                    page_idx = int(pair.get("page", 0))
                    master_idx = int(pair.get("master_idx", 0))
                except (TypeError, ValueError):
                    continue
                if page_idx < 0 or master_idx < 0:
                    return jsonify({"error": "page_idx / master_idx musí být >= 0"}), 400
                if master_idx >= len(master_paths):
                    continue
                pid = master_paths[master_idx].get("profile_id")
                if not pid:
                    # No profile → no field crops to preview
                    result_pairs.append({
                        "pair_idx": i,
                        "master_name": master_paths[master_idx]["name"],
                        "profile_id": None,
                        "fields": [],
                        "page_b_b64": None,
                    })
                    continue

                profile = _load_artwork_profile_by_id(pid)
                if not profile or not profile.get("fields"):
                    result_pairs.append({
                        "pair_idx": i,
                        "master_name": master_paths[master_idx]["name"],
                        "profile_id": pid,
                        "fields": [],
                        "page_b_b64": None,
                    })
                    continue

                # Extract the supplier page as a separate file for rendering
                comp_path = None
                try:
                    if page_idx < len(doc):
                        if reader and page_idx < len(reader.pages):
                            writer = PdfWriter()
                            writer.add_page(reader.pages[page_idx])
                            comp_path = os.path.join(upload_dir, f"{uid}_pv_pg{page_idx}_{i}.pdf")
                            with open(comp_path, "wb") as fp:
                                writer.write(fp)
                        else:
                            pix = doc[page_idx].get_pixmap(matrix=_fitz_pv.Matrix(3.0, 3.0), alpha=False)
                            comp_path = os.path.join(upload_dir, f"{uid}_pv_pg{page_idx}_{i}.png")
                            pix.save(comp_path)
                        comp_paths.append(comp_path)
                    else:
                        comp_path = supplier_path

                    with _HEAVY_SEM:
                        preview_data = extract_field_crops_for_preview(
                            master_paths[master_idx]["path"],
                            comp_path,
                            profile,
                            page_a=0,
                            page_b=0,
                        )
                    result_pairs.append({
                        "pair_idx":    i,
                        "master_name": master_paths[master_idx]["name"],
                        "profile_id":  pid,
                        **preview_data,
                    })
                except Exception as _pe:
                    logger.warning("preview-crops pair %d error: %s", i, _pe)
                    result_pairs.append({
                        "pair_idx": i,
                        "master_name": master_paths[master_idx]["name"],
                        "profile_id": pid,
                        "fields": [],
                        "page_b_b64": None,
                        "error": str(_pe)[:200],
                    })
        finally:
            doc.close()

        return jsonify({"pairs": result_pairs})

    except Exception as e:
        logger.exception("preview-crops error")
        return jsonify({"error": "Błąd generowania podglądu"}), 500
    finally:
        temp_master_paths = [m["path"] for m in master_paths if m.get("is_temp", True)]
        for p in ([supplier_path] + temp_master_paths + comp_paths):
            try:
                if p:
                    os.remove(p)
            except Exception:
                pass


@app.route("/api/artwork/split-compare", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_split_compare():
    """
    Wielostronicowy PDF dostawcy → porównuje z masterami wg jawnych par.

    Form fields:
    - supplier_pdf: PDF file
    - masters[]: N master files (indexed 0..N-1)
    - pairs_json: JSON array [{master_idx, page, crop, label}, ...]
      crop format: {x0,y0,x1,y1} as fractions 0-1, or null for full page
    - use_ai: "0" | "1"

    Fallback (no pairs_json): auto-pair by index order (old behaviour).
    """
    import traceback as _tb
    supplier_path = None
    master_paths = []
    comp_paths = []
    try:
        supplier_file = request.files.get("supplier_pdf")
        masters_files = request.files.getlist("masters[]")
        use_ai = request.form.get("use_ai", "0") == "1"
        pairs_json_str = request.form.get("pairs_json", "")
        master_configs_str = request.form.get("master_configs_json", "")
        field_overrides_str = request.form.get("field_overrides_json", "")

        if not supplier_file or not supplier_file.filename:
            return jsonify({"error": "Wymagany plik PDF dostawcy"}), 400
        if not masters_files and not master_configs_str:
            return jsonify({"error": "Wymagane pliki masterów"}), 400
        if not supplier_file.filename.lower().endswith(".pdf"):
            return jsonify({"error": "Plik dostawcy musi być PDF"}), 400

        uid = session["user_id"]
        upload_dir = app.config["UPLOAD_FOLDER"]
        os.makedirs(upload_dir, exist_ok=True)

        supplier_path = os.path.join(upload_dir,
                                     f"{uid}_spl_sup_{secure_filename(supplier_file.filename) or 'supplier.pdf'}")
        supplier_file.save(supplier_path)

        # Build master_paths from either uploaded files or library profiles
        if master_configs_str:
            try:
                master_configs = json.loads(master_configs_str)
            except Exception:
                return jsonify({"error": "Błąd parsowania master_configs_json"}), 400
            db_mc = get_db()
            _ensure_artwork_profile_tables(db_mc)
            _mc_error = None
            try:
                for cfg in master_configs:
                    if cfg.get("type") == "profile":
                        pid = cfg.get("profile_id")
                        if not pid:
                            _mc_error = ({"error": "Brakujący profile_id w konfiguracji mastera"}, 400)
                            break
                        row = db_mc.execute(
                            "SELECT name, master_pdf_path FROM artwork_profiles WHERE id=?",
                            (pid,)
                        ).fetchone()
                        if not row or not row["master_pdf_path"]:
                            _mc_error = ({"error": f"Master z biblioteki (id={pid}) nie ma pliku PDF — dodaj plik w edytorze szablonów"}, 404)
                            break
                        resolved = _resolve_master_path(row["master_pdf_path"], profile_id=pid, db=db_mc)
                        if not os.path.exists(resolved):
                            _mc_error = ({"error": f"Plik mastera nie istnieje na dysku: {resolved}"}, 404)
                            break
                        master_paths.append({"name": row["name"], "path": resolved, "profile_id": pid, "is_temp": False})
                    else:
                        fi = cfg.get("file_idx")
                        if fi is None or not isinstance(fi, int) or fi < 0 or fi >= len(masters_files):
                            _mc_error = ({"error": "Brakujący plik mastera — odśwież stronę"}, 400)
                            break
                        mf = masters_files[fi]
                        mp = os.path.join(upload_dir, f"{uid}_spl_mst_{len(master_paths)}_{secure_filename(mf.filename or '') or 'master.pdf'}")
                        mf.save(mp)
                        master_paths.append({"name": mf.filename, "path": mp, "profile_id": None, "is_temp": True})
            finally:
                db_mc.close()
            if _mc_error:
                return jsonify(_mc_error[0]), _mc_error[1]
        else:
            # Legacy: all uploaded files
            for i, mf in enumerate(masters_files):
                mp = os.path.join(upload_dir, f"{uid}_spl_mst_{i}_{secure_filename(mf.filename or '') or 'master.pdf'}")
                mf.save(mp)
                master_paths.append({"name": mf.filename, "path": mp, "profile_id": None, "is_temp": True})

        # Parse or generate pairs
        if pairs_json_str:
            try:
                pairs = json.loads(pairs_json_str)
            except Exception:
                return jsonify({"error": "Błąd parsowania pairs_json"}), 400
        else:
            # Fallback: auto-pair by index order
            try:
                from pypdf import PdfReader
                page_count = len(PdfReader(supplier_path).pages)
            except Exception:
                page_count = len(master_paths)
            if page_count != len(master_paths):
                return jsonify({
                    "error": (f"PDF dostawcy ma {page_count} stron, "
                              f"ale wgrano {len(master_paths)} masterów.")
                }), 400
            pairs = [{"master_idx": i, "page": i, "crop": None,
                      "label": f"Strona {i+1}"} for i in range(page_count)]

        # Validate
        for pair in pairs:
            mi = pair.get("master_idx")
            if not isinstance(mi, int) or mi < 0 or mi >= len(master_paths):
                return jsonify({"error": f"Nieprawidłowy master_idx: {mi}"}), 400

        import fitz
        from artwork_comparator import compare_artworks, set_progress as _set_progress
        import hashlib as _hl, time as _t
        _progress_id = (request.form.get("progress_id") or "").strip() or None
        # field_overrides_json: {pair_idx: {field_name: {x1_pct,y1_pct,x2_pct,y2_pct}|null}}
        try:
            _field_overrides_all = json.loads(field_overrides_str) if field_overrides_str else {}
        except Exception:
            _field_overrides_all = {}

        try:
            from pypdf import PdfReader, PdfWriter
            reader = PdfReader(supplier_path)
        except Exception:
            reader = None

        doc = fitz.open(supplier_path)
        try:
            results = []
            for i, pair in enumerate(pairs):
                try:
                    page_idx = int(pair.get("page", 0))
                    master_idx = int(pair.get("master_idx", 0))
                except (TypeError, ValueError):
                    continue
                if page_idx < 0 or master_idx < 0:
                    return jsonify({"error": "page_idx / master_idx musí być >= 0"}), 400
                if master_idx >= len(master_paths):
                    return jsonify({"error": "master_idx poza zakresem"}), 400
                crop = pair.get("crop")   # None or {x0,y0,x1,y1} in 0-1 coords
                unit_label = pair.get("label") or f"Strona {page_idx + 1}"
                master_name = master_paths[master_idx]["name"]
                master_path_val = master_paths[master_idx]["path"]

                if page_idx >= len(doc):
                    results.append({
                        "pair_index": i + 1, "master_name": master_name,
                        "supplier_page": page_idx + 1, "unit_label": unit_label,
                        "risk_level": "error",
                        "error": f"Strona {page_idx + 1} nie istnieje w PDF.",
                    })
                    continue

                comp_path = None
                try:
                    page = doc[page_idx]
                    r = page.rect

                    if crop and isinstance(crop, dict) and all(k in crop for k in ("x0", "y0", "x1", "y1")):
                        try:
                            cx0, cy0, cx1, cy1 = (float(crop[k]) for k in ("x0", "y0", "x1", "y1"))
                        except (TypeError, ValueError):
                            return jsonify({"error": "Wartości crop muszą być liczbami"}), 400
                        if not all(0.0 <= v <= 1.0 for v in (cx0, cy0, cx1, cy1)):
                            return jsonify({"error": "Wartości crop muszą mieścić się w zakresie [0.0, 1.0]"}), 400
                        # Render cropped region as PNG at high resolution
                        clip = fitz.Rect(
                            cx0 * r.width,  cy0 * r.height,
                            cx1 * r.width,  cy1 * r.height,
                        )
                        pix = page.get_pixmap(matrix=fitz.Matrix(3.0, 3.0),
                                              clip=clip, alpha=False)
                        comp_path = os.path.join(upload_dir, f"{uid}_spl_crop_{i}.png")
                        pix.save(comp_path)
                    elif reader:
                        writer = PdfWriter()
                        writer.add_page(reader.pages[page_idx])
                        comp_path = os.path.join(upload_dir,
                                                 f"{uid}_spl_pg{page_idx}_{i}.pdf")
                        with open(comp_path, "wb") as fp:
                            writer.write(fp)
                    else:
                        pix = page.get_pixmap(matrix=fitz.Matrix(3.0, 3.0), alpha=False)
                        comp_path = os.path.join(upload_dir,
                                                 f"{uid}_spl_pg{page_idx}_{i}.png")
                        pix.save(comp_path)

                    comp_paths.append(comp_path)

                    _fob = _field_overrides_all.get(str(i)) or _field_overrides_all.get(i)
                    if _progress_id and len(pairs) > 1:
                        _set_progress(_progress_id, 8, f"Para {i+1}/{len(pairs)} — start…")
                    with _HEAVY_SEM:
                        result = compare_artworks(master_path_val, comp_path,
                                                  use_ai=use_ai,
                                                  profile_id=master_paths[master_idx].get("profile_id"),
                                                  field_overrides_b=_fob,
                                                  progress_id=_progress_id)

                    cache_id = _hl.sha256(f"{uid}_{_t.time()}_{i}".encode()).hexdigest()[:12]
                    _cache_put(cache_id, result, owner_id=uid)

                    rd = result.to_dict(include_images=False)
                    rd.update({
                        "cache_id": cache_id,
                        "pair_index": i + 1,
                        "master_name": master_name,
                        "supplier_page": page_idx + 1,
                        "unit_label": unit_label,
                    })

                    try:
                        rdsave = dict(rd)
                        rdsave["status"] = result.risk_level
                        rdsave["diff_count"] = result.critical_count + result.important_count
                        _save_comparison(uid, "artwork", master_name,
                                         f"{supplier_file.filename} — {unit_label}", rdsave)
                    except Exception:
                        pass

                    results.append(rd)

                except Exception as e:
                    results.append({
                        "pair_index": i + 1,
                        "master_name": master_name,
                        "supplier_page": page_idx + 1,
                        "unit_label": unit_label,
                        "risk_level": "error",
                        "error": str(e)[:200],
                    })
                    if comp_path and comp_path not in comp_paths:
                        comp_paths.append(comp_path)

            _set_progress(_progress_id, 99, "Generowanie raportu…")
            return jsonify({"results": results, "total": len(pairs),
                            "supplier_name": supplier_file.filename})
        finally:
            doc.close()

    except Exception as e:
        logger.exception("artwork split-compare error")
        return jsonify({"error": "Błąd porównania artworków"}), 500
    finally:
        temp_master_paths = [m["path"] for m in master_paths if m.get("is_temp", True)]
        for p in ([supplier_path] + temp_master_paths + comp_paths):
            if p:
                try:
                    os.remove(p)
                except Exception:
                    pass



@app.route("/api/artwork/compare-progress/<pid>", methods=["GET"])
@login_required
def api_artwork_compare_progress(pid):
    """Bieżący etap porównania (do paska postępu). Frontend odpytuje co ~0.7 s."""
    try:
        from artwork_comparator import get_progress
        p = get_progress(pid) or {}
    except Exception:
        p = {}
    return jsonify({"pct": p.get("pct", 0), "label": p.get("label", "")})


def _magic_ok(head: bytes) -> bool:
    """True, gdy nagłówek (magic bytes) pasuje do dozwolonego typu (PDF/obraz)."""
    return (head.startswith(b"%PDF-") or                    # PDF
            head.startswith(b"\x89PNG\r\n\x1a\n") or         # PNG
            head.startswith(b"\xff\xd8\xff") or              # JPEG
            head[:4] in (b"II*\x00", b"MM\x00*") or          # TIFF (LE/BE)
            head.startswith(b"BM") or                        # BMP
            (head[:4] == b"RIFF" and head[8:12] == b"WEBP")) # WEBP


def _validate_upload_magic(path: str) -> bool:
    """True, gdy TREŚĆ pliku pasuje do dozwolonego typu (PDF/obraz). Walidacja samego
    rozszerzenia to za mało — plik z rozszerzeniem .pdf może zawierać cokolwiek. Czyta
    tylko nagłówek (magic bytes)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return False
    return _magic_ok(head)


@app.route("/api/artwork/compare", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_compare():
    _uid = session["user_id"]
    if _check_and_record_api_rate(_uid):
        return jsonify({"error": "Zbyt wiele żądań. Odczekaj minutę i spróbuj ponownie."}), 429
    import traceback as _tb
    path_a = path_b = None
    uploaded_path_a = False  # track if we saved file_a ourselves (so we can delete it)
    try:
        file_a = request.files.get("file_a")
        file_b = request.files.get("file_b")
        use_ai = request.form.get("use_ai", "0") == "1"
        master_profile_id = request.form.get("master_profile_id", type=int)
        master_name = None

        ALLOWED = {".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".webp"}
        uid = session["user_id"]
        os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

        # --- File A: either uploaded or from master library ---
        if master_profile_id:
            db = get_db()
            try:
                _ensure_artwork_profile_tables(db)
                row = db.execute(
                    "SELECT name, master_pdf_path FROM artwork_profiles WHERE id=?",
                    (master_profile_id,)
                ).fetchone()
            finally:
                db.close()
            if not row or not row["master_pdf_path"]:
                return jsonify({"error": "Master nie znaleziony w bibliotece lub brak pliku PDF"}), 404
            resolved_a = _resolve_master_path(row["master_pdf_path"], profile_id=master_profile_id)
            if not os.path.exists(resolved_a):
                return jsonify({"error": f"Plik mastera nie istnieje na dysku: {resolved_a}"}), 404
            path_a = resolved_a
            master_name = row["name"]
        elif file_a and file_a.filename:
            ext_a = os.path.splitext(file_a.filename.lower())[1]
            if ext_a not in ALLOWED:
                return jsonify({"error": "Dozwolone: PDF, JPG, PNG, TIFF"}), 400
            path_a = os.path.join(app.config["UPLOAD_FOLDER"],
                                  f"{uid}_aw_a_{secure_filename(file_a.filename) or 'art_a'}")
            file_a.save(path_a)
            uploaded_path_a = True
        else:
            return jsonify({"error": "Wymagany plik A lub wybór mastera z biblioteki"}), 400

        # --- File B: always uploaded ---
        if not file_b or not file_b.filename:
            return jsonify({"error": "Wymagany plik B (wersja fabryczna)"}), 400
        ext_b = os.path.splitext(file_b.filename.lower())[1]
        if ext_b not in ALLOWED:
            return jsonify({"error": "Dozwolone: PDF, JPG, PNG, TIFF"}), 400
        path_b = os.path.join(app.config["UPLOAD_FOLDER"],
                              f"{uid}_aw_b_{secure_filename(file_b.filename) or 'art_b'}")
        file_b.save(path_b)

        # Reject unreasonably large or spoofed files early. Magic-bytes tylko dla
        # plików, które sami zapisaliśmy z uploadu (master z biblioteki jest zaufany).
        for p, label, is_upload in [(path_a, "A", bool(uploaded_path_a)), (path_b, "B", True)]:
            size_mb = os.path.getsize(p) / 1024 / 1024
            if size_mb > 150:
                return jsonify({"error": f"Plik {label} jest zbyt duży ({size_mb:.0f} MB). Maksimum 150 MB."}), 400
            if is_upload and not _validate_upload_magic(p):
                return jsonify({"error": f"Plik {label} nie jest prawidłowym PDF/obrazem "
                                         "(rozszerzenie nie zgadza się z treścią)."}), 400
        from artwork_comparator import compare_artworks, set_ai_model, AVAILABLE_MODELS
        page_a = request.form.get("page_a", type=int)
        page_b = request.form.get("page_b", type=int)
        ai_model = request.form.get("ai_model", "claude-sonnet-4-6")
        valid_ids = {m[0] for m in AVAILABLE_MODELS}
        if ai_model not in valid_ids:
            return jsonify({"error": f"Nieznany model AI: {ai_model}. Dostępne: {sorted(valid_ids)}"}), 400
        set_ai_model(ai_model)
        # On-demand performance profiling — checkbox in UI or ?perf=1; ARTWORK_PROFILE
        # env forces it on globally. Adds a detailed "Wydajność" section to the report.
        _profile_perf = str(request.form.get("profile_perf")
                            or request.args.get("perf") or "").strip().lower() in ("1", "true", "on", "yes")
        _progress_id = (request.form.get("progress_id") or "").strip() or None
        import time as _time
        _t0 = _time.monotonic()
        with _HEAVY_SEM:
            result = compare_artworks(path_a, path_b, use_ai=use_ai,
                                      use_ai_sections=use_ai, max_pages=6,
                                      page_a=page_a, page_b=page_b,
                                      profile_id=master_profile_id,
                                      profile_perf=_profile_perf,
                                      progress_id=_progress_id)
        _dur = int((_time.monotonic() - _t0) * 1000)
        import hashlib as _hl
        cache_id = _hl.sha256(f"{uid}_{_time.time()}".encode()).hexdigest()[:12]
        _cache_put(cache_id, result, owner_id=uid)
        data = result.to_dict(include_images=False)
        data["cache_id"] = cache_id
        name_a = master_name or (file_a.filename if file_a else os.path.basename(path_a))
        name_b = file_b.filename if file_b else ""
        try:
            full_data = result.to_dict(include_images=False)
            full_data["cache_id"] = cache_id
            full_data["status"] = result.risk_level
            full_data["diff_count"] = result.critical_count + result.important_count
            _save_comparison(uid, "artwork", name_a, name_b, full_data)
            if master_profile_id:
                _db = get_db()
                try:
                    _db.execute(
                        "UPDATE artwork_profiles SET use_count=COALESCE(use_count,0)+1 WHERE id=?",
                        (master_profile_id,)
                    )
                    _db.commit()
                finally:
                    _db.close()
        except Exception:
            pass
        _log_audit("artwork_compare", detail=f"{name_a} vs {name_b}",
                   duration_ms=_dur,
                   extra={"file_a": name_a, "file_b": name_b,
                          "profile_id": master_profile_id,
                          "risk_level": result.risk_level,
                          "critical": result.critical_count,
                          "important": result.important_count,
                          "use_ai": use_ai, "duration_ms": _dur})
        return jsonify(data)
    except Exception as e:
        logger.exception("artwork compare error")
        _log_audit("artwork_compare_error", detail=str(e)[:200])
        return jsonify({"error": "Błąd porównania artwork"}), 500
    finally:
        if uploaded_path_a and path_a:
            try: os.remove(path_a)
            except Exception: pass
        if path_b:
            try: os.remove(path_b)
            except Exception: pass


@app.route("/api/artwork/pdf-page-count", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_pdf_page_count():
    """Return page count for an uploaded PDF without storing it."""
    f = request.files.get("file")
    if not f or not f.filename or not f.filename.lower().endswith(".pdf"):
        return jsonify({"pages": 1})
    uid = session["user_id"]
    tmp_path = os.path.join(app.config["UPLOAD_FOLDER"],
                            f"{uid}_tmp_pgcount_{secure_filename(f.filename) or 'count.pdf'}")
    try:
        os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
        f.save(tmp_path)
        from artwork_comparator import count_pdf_pages
        n = count_pdf_pages(tmp_path)
        return jsonify({"pages": n or 1})
    except Exception as e:
        return jsonify({"pages": 1, "error": str(e)[:200]})
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


@app.route("/artwork/3d")
@login_required
def artwork_3d_page():
    """Strona generatora modeli 3D opakowań z artworków (dieline PDF)."""
    return render_template("artwork_3d.html",
                           username=session["username"],
                           role=session["role"],
                           max_upload_mb=MAX_UPLOAD_MB)


@app.route("/api/artwork/3d", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_3d():
    """Generuje model GLB opakowania z wgranego artworku PDF (dieline).

    Form-data: file (PDF), opcjonalnie w_mm/h_mm/d_mm (ręczne wymiary,
    nadpisują odczytane z tabelki artworku).
    """
    import uuid as _uuid_3d

    f = request.files.get("file")
    if not f or not f.filename or not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Wgraj plik PDF z dieline opakowania."}), 400
    if f.mimetype and f.mimetype not in _ALLOWED_PDF_MIMES:
        return jsonify({"error": f"Niedozwolony typ pliku: {f.mimetype}"}), 400

    dims_override = None
    raw_dims = [request.form.get(k, "").strip() for k in ("w_mm", "h_mm", "d_mm")]
    if any(raw_dims):
        try:
            vals = [float(v.replace(",", ".")) for v in raw_dims]
            if not all(5.0 <= v <= 2000.0 for v in vals):
                raise ValueError
            dims_override = tuple(vals)
        except ValueError:
            return jsonify({"error": "Nieprawidłowe wymiary — podaj trzy wartości 5–2000 mm."}), 400

    uid = session["user_id"]
    out_dir = os.path.join(app.config["UPLOAD_FOLDER"], "artwork3d")
    os.makedirs(out_dir, exist_ok=True)
    token = _uuid_3d.uuid4().hex[:12]
    src_name = secure_filename(f.filename) or "artwork.pdf"
    tmp_pdf = os.path.join(out_dir, f"{uid}_src_{token}_{src_name}")
    glb_name = f"{uid}_{token}.glb"
    png_name = f"{uid}_{token}.png"
    # Zapis PDF musi być w kontekście żądania; ciężką generację (PyMuPDF + pyvista/VTK)
    # oddajemy do wątku, by nie blokować workera — front odpytuje /status.
    f.save(tmp_pdf)
    # Podpowiedź orientacji z wcześniejszych akceptacji — czytamy w kontekście żądania,
    # żeby wątek roboczy nie musiał sam trzymać połączenia do bazy.
    a3d_meta = dict(_a3d_key(src_name))
    a3d_meta["suggested_orientation"] = _a3d_learned(a3d_meta)
    jid = _async_job_create(uid)
    threading.Thread(
        target=_artwork_3d_worker,
        args=(jid, tmp_pdf, os.path.join(out_dir, glb_name), out_dir, glb_name, png_name,
              dims_override, session.get("username"), src_name, a3d_meta),
        daemon=True,
    ).start()
    return jsonify({"job_id": jid})


@app.route("/artwork/3d/photo")
@login_required
def artwork_3d_photo_page():
    """Model 3D ze zdjęć — dla materiałów bez artworku (zdjęcia z internetu/telefonu)."""
    return render_template("artwork_3d_photo.html",
                           username=session["username"],
                           role=session["role"],
                           max_upload_mb=MAX_UPLOAD_MB)


@app.route("/artwork/3d/marm")
@login_required
def artwork_3d_marm_page():
    """Model 3D z danych MARM — wybierz REF + jednostkę, wymiary wypełniają się
    z master daty SAP; opcjonalnie dograj zdjęcia ścian dla tekstur.

    MARM-02: gdy otwarto dla konkretnego artworku (?artwork=REF) i mamy zapamiętane
    powiązanie, podpowiadamy wcześniej ustawione REF materiału + jednostkę."""
    artwork = (request.args.get("artwork") or "").strip()
    prefill = None
    if artwork:
        import artwork_marm_link as _aml
        prefill = _aml.get_link(get_db(), artwork)
    return render_template("artwork_3d_marm.html",
                           username=session["username"],
                           role=session["role"],
                           max_upload_mb=MAX_UPLOAD_MB,
                           artwork=artwork,
                           prefill=prefill)


_A3D_PHOTO_PANELS = ("front", "back", "left", "right", "top", "bottom")


def _a3d_photo_unwarp(img, quad: str):
    """Prostowanie perspektywy: quad = 'x0,y0 x1,y1 x2,y2 x3,y3' (znormalizowane 0-1,
    kolejność NW SW SE NE — konwencja PIL QUAD). Zwraca wyprostowany prostokąt."""
    from PIL import Image
    pts = []
    for pair in quad.split():
        x, y = pair.split(",")
        pts.extend([max(0.0, min(1.0, float(x))) * img.width,
                    max(0.0, min(1.0, float(y))) * img.height])
    if len(pts) != 8:
        raise ValueError("quad musi mieć 4 punkty x,y")
    # rozmiar wyjścia z geometrii quada (średnie długości boków)
    import math
    d = lambda i, j: math.hypot(pts[i] - pts[j], pts[i + 1] - pts[j + 1])
    out_w = max(2, int((d(0, 6) + d(2, 4)) / 2))   # NW-NE, SW-SE
    out_h = max(2, int((d(0, 2) + d(6, 4)) / 2))   # NW-SW, NE-SE
    return img.transform((out_w, out_h), Image.Transform.QUAD, tuple(pts),
                         resample=Image.Resampling.BILINEAR)


# Kolory gładkiego boxu (ściany bez zdjęcia). Domyślnie kartonowy jasny brąz.
_A3D_FILL_COLORS = {"carton": (198, 166, 120), "white": (255, 255, 255)}


def _a3d_photo_panels(imgs: dict, w_mm: float, h_mm: float, d_mm: float,
                      fill_color: str | None = None) -> dict:
    """Komplet 6 paneli z częściowego (lub pustego) zestawu zdjęć: brakujące ściany
    dostają jednolity kolor w proporcjach właściwej ściany.

    fill_color=None (domyślnie, zakładka „ze zdjęć") → kolor tła z rogu pierwszego
    zdjęcia, a gdy brak zdjęć — kartonowy. fill_color='carton'/'white' (flow z MARM)
    → jawnie wybrana barwa gładkiego boxu."""
    from artwork_3d import _blank_panel, _corner_color
    sizes = {"front": (w_mm, h_mm), "back": (w_mm, h_mm),
             "left": (d_mm, h_mm), "right": (d_mm, h_mm),
             "top": (w_mm, d_mm), "bottom": (w_mm, d_mm)}
    if fill_color is None:
        fill = _corner_color(next(iter(imgs.values()))) if imgs else _A3D_FILL_COLORS["carton"]
    else:
        fill = _A3D_FILL_COLORS.get(fill_color, _A3D_FILL_COLORS["carton"])
    return {name: imgs.get(name) or _blank_panel(sizes[name], fill)
            for name in _A3D_PHOTO_PANELS}


@app.route("/api/artwork/3d/photo", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_3d_photo():
    """Buduje GLB pudełka z przypisanych zdjęć ścian (panel_front..panel_bottom)
    + wymiarów W×H×D. Zdjęcia są już wykadrowane po stronie przeglądarki.
    Brakujące ściany dostają kolor tła pierwszego zdjęcia. Synchronicznie —
    bez PDF/OCR/VTK budowa trwa sekundy."""
    import uuid as _u
    from PIL import Image, UnidentifiedImageError

    from normalizer import normalize_number
    vals = [normalize_number(request.form.get(k, "")) for k in ("w_mm", "h_mm", "d_mm")]
    if not all(v is not None and 5.0 <= v <= 2000.0 for v in vals):
        return jsonify({"error": "Podaj wymiary W×H×D (5–2000 mm)."}), 400
    # normalize_number zwraca Decimal — artwork_3d.build_glb liczy na float (Decimal/float
    # rzuca TypeError), więc rzutujemy tu, na granicy wejścia.
    w_mm, h_mm, d_mm = (float(v) for v in vals)

    imgs = {}
    raw_photo_bytes = []
    for name in _A3D_PHOTO_PANELS:
        f = request.files.get(f"panel_{name}")
        if not f or not f.filename:
            continue
        try:
            raw = f.read()
            raw_photo_bytes.append(raw)
            img = Image.open(io.BytesIO(raw))
            img.load()
            img = img.convert("RGB")
            quad = (request.form.get(f"quad_{name}") or "").strip()
            if quad:
                img = _a3d_photo_unwarp(img, quad)
            imgs[name] = img
        except (UnidentifiedImageError, OSError, ValueError):
            return jsonify({"error": f"Nieczytelny obraz/kadr dla ściany: {name}"}), 400
    # Brak zdjęć jest OK — renderujemy gładki box w wybranym kolorze (flow z MARM).

    # fill_color podany (flow z MARM) → jawna barwa gładkiego boxu; brak → kolor
    # tła zdjęcia (zachowanie zakładki „ze zdjęć"). Bez zdjęć zawsze wymagany kolor.
    fill_color = (request.form.get("fill_color") or "").strip().lower() or None
    if fill_color is not None and fill_color not in _A3D_FILL_COLORS:
        fill_color = "carton"
    if not imgs and fill_color is None:
        fill_color = "carton"
    from artwork_3d import DielineInfo, build_glb
    panels = _a3d_photo_panels(imgs, w_mm, h_mm, d_mm, fill_color)

    uid = session["user_id"]
    out_dir = os.path.join(app.config["UPLOAD_FOLDER"], "artwork3d")
    os.makedirs(out_dir, exist_ok=True)
    glb_name = f"{uid}_{_u.uuid4().hex[:12]}.glb"
    info = DielineInfo(w_mm=w_mm, h_mm=h_mm, d_mm=d_mm, package_type="")
    try:
        build_glb(panels, info, os.path.join(out_dir, glb_name))
    except Exception as e:
        logger.exception("artwork 3d photo build failed")
        return jsonify({"error": f"Błąd budowy modelu: {str(e)[:200]}"}), 500
    _log_audit("artwork_3d_photo", session.get("username"),
               f"{glb_name}: {w_mm}×{h_mm}×{d_mm} mm, ściany: {', '.join(sorted(imgs))}")

    # REG-04/Pitfall 2+3: source_hash liczony TERAZ, w scope źródła — te bajty/dane
    # nie przetrwają do accept-time. Rozróżnienie po tym, co faktycznie przyszło
    # (nie po heurystyce): zdjęcia → photo_source_hash; inaczej ref+unit (MARM) →
    # marm_source_hash. `variant` NIE jest ustalany tu — przyjdzie jawnym literałem
    # z szablonu w accept-POST (Pitfall 3: /photo jest wspólny dla photo i marm).
    ref = (request.form.get("ref") or "").strip()
    unit = (request.form.get("unit") or "").strip()
    import artwork_render_registry as _arr
    if raw_photo_bytes:
        source_hash = _arr.photo_source_hash(raw_photo_bytes)
    elif ref and unit:
        source_hash = _arr.marm_source_hash(ref, unit, (w_mm, h_mm, d_mm))
    else:
        source_hash = ""

    # MARM-01: gdy generowano 3D z MARM dla konkretnego artworku, zapamiętaj wybór
    # (REF materiału + jednostka) — propose/correct/remember, każde generowanie
    # nadpisuje. Tylko flow MARM (ref+unit, bez zdjęć); artwork z pola formularza.
    artwork = (request.form.get("artwork") or "").strip()
    if artwork and ref and unit and not raw_photo_bytes:
        import artwork_marm_link as _aml
        if _aml.set_link(get_db(), artwork, ref, unit, session.get("username")):
            _log_audit("marm_link_set", session.get("username"),
                       f"{artwork} → {ref}/{unit}")

    return jsonify({
        "glb_name": glb_name,
        "glb_url": url_for("artwork_3d_model", fname=glb_name),
        "dims_mm": [w_mm, h_mm, d_mm],
        "panels_found": sorted(imgs),
        "ref": ref,
        "packaging_type": (request.form.get("packaging_type") or "").strip(),
        "accepted": False,
        "suggested_orientation": [0, 0, 0],
        "source_hash": source_hash,
    })


def _a3d_key(src_name):
    """Klucz uczenia orientacji z nazwy pliku artworku, wzbogacony o master datę
    materiałową. REF i typ opakowania czyta artwork_naming; grupę/podgrupę produktową
    i opis dokłada material_master — dzięki temu wiedza zebrana na jednym REF-ie
    uogólnia się na cały asortyment o tym samym charakterze."""
    from artwork_naming import parse_master_filename
    from material_master import get_material

    p = parse_master_filename(src_name or "")
    out = {"ref": p.get("ref") or "", "packaging_type": p.get("packaging_type") or "",
           "grupa": "", "podgrupa": "", "opis": ""}
    if out["ref"]:
        try:
            mat = get_material(get_db(), out["ref"])
        except Exception:
            mat = None
        if mat:
            out["grupa"] = mat["grupa"] or mat["rodzina"] or ""
            out["podgrupa"] = mat["podgrupa"] or ""
            out["opis"] = mat["opis_pl"] or mat["opis_en"] or ""
    return out


def _a3d_learned(key):
    """Podpowiedź orientacji z wcześniejszych akceptacji — od najbardziej szczegółowej
    reguły do najogólniejszej: dokładnie ten REF → podgrupa → grupa → sam typ opakowania.
    Zwraca [roll, pitch, yaw]."""
    db = get_db()
    ptype = key.get("packaging_type") or ""
    cascade = (
        ("ref = ? AND packaging_type = ?", (key.get("ref"), ptype)),
        ("podgrupa = ? AND packaging_type = ?", (key.get("podgrupa"), ptype)),
        ("grupa = ? AND packaging_type = ?", (key.get("grupa"), ptype)),
        ("packaging_type = ?", (ptype,)),
    )
    for where, params in cascade:
        if not all(params):          # pusty człon klucza = nie ma czego dopasowywać
            continue
        row = db.execute(
            # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
            f"SELECT roll, pitch, yaw FROM artwork_3d_orientation WHERE {where} "  # nosec B608
            f"ORDER BY accepted_at DESC LIMIT 1", params).fetchone()
        if row:
            return [int(row["roll"]), int(row["pitch"]), int(row["yaw"])]
    return [0, 0, 0]


def _a3d_accepted(glb_name):
    """Czy model został zatwierdzony przez użytkownika (warunek pobrania/eksportu)."""
    if not glb_name:
        return False
    return get_db().execute(
        "SELECT 1 FROM artwork_3d_orientation WHERE glb_name = ?",
        (glb_name,)).fetchone() is not None


def _artwork_3d_worker(jid, tmp_pdf, glb_path, out_dir, glb_name, png_name,
                       dims_override, username, src_name, a3d_meta=None):
    """Wątek generacji 3D: dieline → GLB → PNG podglądu → wynik do async_jobs.

    Orientacji NIE wypalamy tutaj — podpowiedź (a3d_meta) idzie do podglądu jako
    ustawienie startowe, a w plik trafia dopiero to, co user zatwierdzi. Dzięki temu
    zapisany kąt jest zawsze bezwzględny i nie składamy dwóch obrotów po sobie."""
    from artwork_3d import generate_artwork_3d, render_glb_preview
    try:
        _async_job_set(jid, "running", step_label="Analiza dielinu i budowa modelu…", pct=25)
        # _HEAVY_SEM: jak w batch — VTK/pyvista bywa nietrwały współbieżnie
        with _HEAVY_SEM:
            result = generate_artwork_3d(tmp_pdf, glb_path, dims_override=dims_override)
            _async_job_set(jid, "running", step_label="Render podglądu…", pct=70)
            preview_ok = render_glb_preview(glb_path, os.path.join(out_dir, png_name))
        _log_audit("artwork_3d", username, f"{src_name} → {glb_name}, dims={result['dims_mm']}")
        # REG-04/Pitfall 2: source_hash MUSI być policzony TU, bajty tmp_pdf są
        # jeszcze na dysku — `finally` niżej je usuwa zanim user zobaczy podgląd.
        # Liczony z bajtów źródłowego PDF (D-01), nie z wynikowego GLB.
        import hashlib as _hashlib
        source_hash = ""
        try:
            _h = _hashlib.md5(usedforsecurity=False)
            with open(tmp_pdf, "rb") as _fh:
                for _chunk in iter(lambda: _fh.read(65536), b""):
                    _h.update(_chunk)
            source_hash = _h.hexdigest()
        except OSError:
            pass
        _async_job_set(jid, "done", result={
            "glb_name": glb_name,
            "png_name": png_name if preview_ok else None,
            "dims_mm": result["dims_mm"],
            "package_type": result["package_type"],
            "ean": result["ean"],
            "panels_found": result["panels_found"],
            "warnings": result["warnings"],
            "source_hash": source_hash,
            "variant": RenderVariant.DIELINE.value,
            **(a3d_meta or {}),
        })
    except ValueError as e:
        _async_job_set(jid, "error", error=str(e))
    except Exception as e:
        logger.exception("artwork 3d generation failed")
        _async_job_set(jid, "error",
                       error=f"Generowanie modelu nie powiodło się: {str(e)[:200]}")
    finally:
        try:
            os.remove(tmp_pdf)
        except OSError:
            pass


@app.route("/api/artwork/3d/status/<jid>")
@login_required
def api_artwork_3d_status(jid):
    """Status generacji 3D. Zwraca postęp; po 'done' — glb_url/preview_url + metadane."""
    job = _async_job_get(jid)
    if not job:
        return jsonify({"status": "expired"}), 404
    if _owner_forbidden(job.get("user_id"), session["user_id"], session["role"]):
        return jsonify({"error": "Brak dostępu"}), 403
    out = {"status": job["status"], "pct": job.get("pct", 0),
           "step_label": job.get("step_label", "")}
    if job["status"] == "error":
        out["error"] = job.get("error") or "Błąd generowania"
    elif job["status"] == "done":
        r = job.get("result") or {}
        out["glb_url"] = url_for("artwork_3d_model", fname=r["glb_name"]) if r.get("glb_name") else None
        out["preview_url"] = (url_for("artwork_3d_model", fname=r["png_name"])
                              if r.get("png_name") else None)
        for k in ("dims_mm", "package_type", "ean", "panels_found", "warnings",
                  "ref", "packaging_type", "grupa", "podgrupa", "opis",
                  "suggested_orientation", "source_hash", "variant"):
            out[k] = r.get(k)
        out["glb_name"] = r.get("glb_name")
        out["accepted"] = _a3d_accepted(r.get("glb_name"))
    return jsonify(out)


def _spawn_palviz_push(registry_id, user):
    """PUSH-01: wypycha wiersz rejestru do PalViz w daemon-threadzie (bez nowej
    infry). No-op gdy push niewłączony (brak PALVIZ_PUSH_URL) — bez zakładania
    połączenia DB. Wątek bierze własne połączenie (get_db poza request contextem
    trzeba zamknąć samemu) i nigdy nie wywala procesu."""
    import palviz_push as _pp
    if not _pp.is_enabled():
        return

    def _worker():
        db = None
        try:
            db = get_db()
            _pp.push_row(db, registry_id, user=user)
        except Exception:
            logger.exception("palviz push failed for registry %s", registry_id)
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    threading.Thread(target=_worker, daemon=True).start()


@app.route("/api/artwork/3d/accept", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_3d_accept():
    """Finalna akceptacja modelu przez użytkownika: zatwierdza układ i typ opakowania.

    Wypala wskazaną orientację na trwałe w GLB (pobrany plik = to, co user widział),
    odświeża PNG podglądu i zapisuje regułę, z której skorzystają kolejne artworki
    o tym samym REF / typie opakowania. Bez tego wpisu pobranie modelu jest zablokowane.
    """
    data = request.get_json(silent=True) or {}
    glb_name = secure_filename(data.get("glb_name") or "")
    if not glb_name.endswith(".glb"):
        return jsonify({"error": "Brak modelu do zatwierdzenia."}), 400
    if not glb_name.startswith(f"{session['user_id']}_") and not can_see_all(session["role"]):
        return jsonify({"error": "Brak dostępu"}), 403

    try:
        roll, pitch, yaw = (int(data.get(k, 0)) % 360 for k in ("roll", "pitch", "yaw"))
    except (TypeError, ValueError):
        return jsonify({"error": "Nieprawidłowy kąt orientacji."}), 400

    out_dir = os.path.abspath(os.path.join(app.config["UPLOAD_FOLDER"], "artwork3d"))
    glb_path = os.path.join(out_dir, glb_name)
    if not os.path.exists(glb_path):
        return jsonify({"error": "Model wygasł — wygeneruj go ponownie."}), 404

    png_name = glb_name[:-4] + ".png"
    try:
        from artwork_3d import bake_orientation, render_glb_preview
        if bake_orientation(glb_path, roll, pitch, yaw):
            render_glb_preview(glb_path, os.path.join(out_dir, png_name))
    except Exception as e:
        logger.exception("artwork 3d bake_orientation failed")
        return jsonify({"error": f"Nie udało się zapisać układu: {str(e)[:200]}"}), 500

    db = get_db()
    db.execute("DELETE FROM artwork_3d_orientation WHERE glb_name = ?", (glb_name,))
    db.execute(
        "INSERT INTO artwork_3d_orientation "
        "(glb_name, ref, packaging_type, package_type, grupa, podgrupa, "
        "roll, pitch, yaw, accepted_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (glb_name, (data.get("ref") or "").strip(), (data.get("packaging_type") or "").strip(),
         (data.get("package_type") or "").strip(), (data.get("grupa") or "").strip(),
         (data.get("podgrupa") or "").strip(), roll, pitch, yaw, session.get("username")))
    db.commit()
    _log_audit("artwork_3d_accept", session.get("username"),
               f"{glb_name}: orientacja {roll}/{pitch}/{yaw}, typ={data.get('package_type') or '—'}")

    # REG-04: drugi (i ostatni) choke-point rejestru — accept-path (manual/photo/
    # marm). variant MUSI być jawnym literałem z szablonu (Pitfall 3 — ten sam
    # endpoint obsługuje photo i marm, nieodróżnialne po polach payloadu); walidacja
    # przed register_render/SQL (V5), nie ufamy stringowi klienta.
    variant = (data.get("variant") or "").strip()
    if variant not in {v.value for v in RenderVariant}:
        return jsonify({"error": "Nieznany wariant renderu."}), 400
    source_hash = (data.get("source_hash") or "").strip()
    import uom as _uom
    ref_norm = _uom.normalize_ref(data.get("ref") or "")
    import artwork_render_registry as _arr
    _reg = _arr.register_render(
        db, fid=None, ref_norm=ref_norm, variant=variant, source_hash=source_hash,
        glb_path=glb_path, created_by=session.get("user_id"))

    # PUSH-01: auto-push nowego renderu do PalViz (env-gated, daemon-thread, brak
    # nowej infry). Tylko świeży wiersz ('inserted') — 'bumped' to render bez zmian
    # (idempotency i tak by zdedupował, ale nie zawracamy sieci). No-op bez env.
    if _reg.get("action") == "inserted":
        _spawn_palviz_push(_reg["id"], session.get("username"))

    return jsonify({"accepted": True,
                    "glb_url": url_for("artwork_3d_model", fname=glb_name),
                    "preview_url": url_for("artwork_3d_model", fname=png_name)})


@app.route("/artwork/3d/model/<path:fname>")
@login_required
def artwork_3d_model(fname):
    """Serwuje wygenerowany model GLB. User widzi tylko swoje modele.

    `?download=1` = wyniesienie pliku poza aplikację — wymaga akceptacji układu przez
    użytkownika. Podgląd w przeglądarce zostaje otwarty, bo bez obejrzenia modelu nie
    da się go zatwierdzić.
    """
    from flask import send_from_directory, abort
    uid = session["user_id"]
    safe = secure_filename(fname)
    if safe.endswith(".glb"):
        mime = "model/gltf-binary"
    elif safe.endswith(".png"):          # statyczny podgląd 3D (pyvista)
        mime = "image/png"
    else:
        abort(404)
    if not safe.startswith(f"{uid}_") and not can_see_all(session["role"]):
        abort(403)
    download = request.args.get("download") == "1"
    if download and not _a3d_accepted(safe[:-4] + ".glb"):
        abort(403, "Model nie został zatwierdzony — potwierdź układ i typ opakowania.")
    out_dir = os.path.abspath(os.path.join(app.config["UPLOAD_FOLDER"], "artwork3d"))
    return send_from_directory(out_dir, safe, mimetype=mime, as_attachment=download)


def _a3d_copy_to_dir(glb_path: str, dest_dir: str) -> list:
    """Kopiuje zatwierdzony model (GLB + PNG, jeśli jest) do wskazanego katalogu.
    Zwraca listę zapisanych ścieżek. Katalog musi istnieć (nie tworzymy — literówka
    w ścieżce ma być błędem, nie nowym folderem). Gdy env ARTWORK_COPY_ROOTS jest
    ustawione (ścieżki rozdzielone os.pathsep), cel musi leżeć pod jednym z korzeni."""
    import shutil
    dest_dir = os.path.abspath(dest_dir)
    roots = [r for r in (os.environ.get("ARTWORK_COPY_ROOTS") or "").split(os.pathsep) if r.strip()]

    def _under(root):
        try:  # commonpath rzuca ValueError przy różnych dyskach (Windows)
            root = os.path.abspath(root)
            return os.path.commonpath([dest_dir, root]) == root
        except ValueError:
            return False

    if roots and not any(_under(r) for r in roots):
        raise ValueError("Katalog poza dozwolonymi lokalizacjami (ARTWORK_COPY_ROOTS).")
    if not os.path.isdir(dest_dir):
        raise ValueError(f"Katalog nie istnieje: {dest_dir}")
    saved = []
    for src in (glb_path, glb_path[:-4] + ".png"):
        if os.path.isfile(src):
            dst = os.path.join(dest_dir, os.path.basename(src))
            shutil.copy2(src, dst)
            saved.append(dst)
    return saved


@app.route("/api/artwork/3d/save-copy", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_3d_save_copy():
    """Zapisuje kopię zatwierdzonego modelu (GLB + PNG) do katalogu wskazanego przez
    użytkownika (ścieżka na serwerze, np. Z:\\... lub katalog lokalny)."""
    data = request.get_json(silent=True) or {}
    glb_name = secure_filename(data.get("glb_name") or "")
    dest_dir = str(data.get("dest_dir") or "").strip()
    if not glb_name.endswith(".glb") or not dest_dir:
        return jsonify({"error": "Wymagane: glb_name i dest_dir."}), 400
    if not glb_name.startswith(f"{session['user_id']}_") and not can_see_all(session["role"]):
        return jsonify({"error": "Brak dostępu"}), 403
    if not _a3d_accepted(glb_name):
        return jsonify({"error": "Model nie został zatwierdzony — potwierdź układ."}), 403
    glb_path = os.path.abspath(os.path.join(app.config["UPLOAD_FOLDER"], "artwork3d", glb_name))
    if not os.path.isfile(glb_path):
        return jsonify({"error": "Model wygasł — wygeneruj go ponownie."}), 404
    try:
        saved = _a3d_copy_to_dir(glb_path, dest_dir)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        return jsonify({"error": f"Błąd zapisu: {str(e)[:200]}"}), 500
    _log_audit("artwork_3d_save_copy", session.get("username"),
               f"{glb_name} → {dest_dir} ({len(saved)} plików)")
    return jsonify({"saved": saved})


# ─────────────────────────────────────────────────────────────────────────────
# BATCH 3D — masowa generacja modeli GLB (wiele dielineów naraz)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/artwork/3d/batch")
@login_required
def artwork_3d_batch_page():
    return render_template("artwork_3d_batch.html",
                           username=session.get("username"), role=session.get("role"))


@app.route("/api/artwork/3d/batch/start", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_3d_batch_start():
    """Masowa generacja 3D: wiele PDF (files[]) → jeden job w tle, model po modelu."""
    import uuid as _u
    files = request.files.getlist("files[]")
    files = [f for f in files if f and f.filename and f.filename.lower().endswith(".pdf")]
    if not files:
        return jsonify({"error": "Wgraj co najmniej jeden plik PDF (dieline)."}), 400
    if len(files) > 50:
        return jsonify({"error": "Maksimum 50 modeli na paczkę."}), 400
    uid = session["user_id"]
    out_dir = os.path.join(app.config["UPLOAD_FOLDER"], "artwork3d")
    os.makedirs(out_dir, exist_ok=True)
    items = []
    for f in files:
        if not _validate_upload_magic_stream(f):
            continue
        token = _u.uuid4().hex[:12]
        src_name = secure_filename(f.filename) or "artwork.pdf"
        tmp_pdf = os.path.join(out_dir, f"{uid}_src_{token}_{src_name}")
        f.save(tmp_pdf)
        items.append({"tmp_pdf": tmp_pdf, "glb_name": f"{uid}_{token}.glb",
                      "png_name": f"{uid}_{token}.png", "src_name": src_name})
    if not items:
        return jsonify({"error": "Żaden plik nie przeszedł walidacji PDF."}), 400
    jid = _async_job_create(uid)
    threading.Thread(target=_artwork_3d_batch_worker,
                     args=(jid, items, out_dir, session.get("username")),
                     daemon=True).start()
    return jsonify({"job_id": jid, "count": len(items)})


def _validate_upload_magic_stream(f) -> bool:
    """Magic-bytes dla FileStorage bez zapisu na dysk (batch — zanim zapiszemy)."""
    try:
        head = f.stream.read(16)
        f.stream.seek(0)
    except Exception:
        return False
    return _magic_ok(head)


def _artwork_3d_batch_worker(jid, items, out_dir, username):
    """Wątek batch 3D: dla każdego dielinu GLB + PNG podglądu, sekwencyjnie (VTK),
    z limitem _HEAVY_SEM. Wynik (lista modeli) aktualizowany przyrostowo do /status."""
    from artwork_3d import generate_artwork_3d, render_glb_preview
    results, total = [], len(items)
    for i, it in enumerate(items):
        _async_job_set(jid, "running",
                       step_label=f"Model {i + 1}/{total}: {it['src_name']}",
                       pct=int(i * 100 / total),
                       result={"items": results, "done": i, "total": total})
        try:
            with _HEAVY_SEM:
                glb_path = os.path.join(out_dir, it["glb_name"])
                r = generate_artwork_3d(it["tmp_pdf"], glb_path)
                ok = render_glb_preview(glb_path, os.path.join(out_dir, it["png_name"]))
            results.append({"src_name": it["src_name"], "glb_name": it["glb_name"],
                            "png_name": it["png_name"] if ok else None,
                            "dims_mm": r["dims_mm"], "package_type": r["package_type"],
                            "ean": r["ean"], "panels_found": r["panels_found"]})
        except Exception as e:
            logger.exception("batch 3d item failed")
            results.append({"src_name": it["src_name"], "error": str(e)[:160]})
        finally:
            try:
                os.remove(it["tmp_pdf"])
            except OSError:
                pass
    _log_audit("artwork_3d_batch", username, f"{total} modeli")
    _async_job_set(jid, "done", result={"items": results, "done": total, "total": total})


def _batch3d_item_urls(it):
    """Dodaj glb_url/preview_url do pozycji wyniku (nazwy → URL-e)."""
    out = dict(it)
    if it.get("glb_name"):
        out["glb_url"] = url_for("artwork_3d_model", fname=it["glb_name"])
    out["preview_url"] = url_for("artwork_3d_model", fname=it["png_name"]) if it.get("png_name") else None
    return out


@app.route("/api/artwork/3d/batch/from-library", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_3d_batch_from_library():
    """Batch 3D dla profili masterów z biblioteki, filtrowanych po rodzinie (REF/opakowanie)
    lub po jawnej liście profile_ids. Bierze tylko profile z DOSTĘPNYM plikiem mastera."""
    import uuid as _u, shutil as _sh
    data = request.get_json(silent=True) or {}
    ref_prefix = str(data.get("ref_prefix") or "").strip()
    packaging = str(data.get("packaging") or "").strip()
    ids = data.get("profile_ids") or []
    uid = session["user_id"]
    out_dir = os.path.join(app.config["UPLOAD_FOLDER"], "artwork3d")
    os.makedirs(out_dir, exist_ok=True)
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        where, params = ["is_active=1", "master_pdf_path<>''"], []
        if ids:
            qs = ",".join("?" for _ in ids)
            where.append(f"id IN ({qs})")
            params += [int(x) for x in ids]
        else:
            if ref_prefix:
                from db import escape_like
                where.append("ref_code LIKE ? ESCAPE '\\'")
                params.append(escape_like(ref_prefix) + "%")
            if packaging:
                where.append("packaging_type=?")
                params.append(packaging)
        rows = db.execute(
            # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
            "SELECT id, name, ref_code, master_pdf_path FROM artwork_profiles WHERE "  # nosec B608
            + " AND ".join(where) + " ORDER BY ref_code LIMIT 50", params).fetchall()
        items, skipped = [], 0
        for r in rows:
            resolved = _resolve_master_path(r["master_pdf_path"], profile_id=r["id"], db=db)
            if not resolved or not os.path.exists(resolved):
                skipped += 1
                continue
            token = _u.uuid4().hex[:12]
            tmp_pdf = os.path.join(out_dir, f"{uid}_lib_{token}.pdf")
            try:
                _sh.copy(resolved, tmp_pdf)
            except OSError:
                skipped += 1
                continue
            nm = r["name"] or r["ref_code"] or "master"
            items.append({"tmp_pdf": tmp_pdf, "glb_name": f"{uid}_{token}.glb",
                          "png_name": f"{uid}_{token}.png", "src_name": f"{nm}.pdf"})
        db.commit()
    finally:
        db.close()
    if not items:
        return jsonify({"error": "Brak profili z dostępnym plikiem mastera dla tych filtrów."}), 400
    jid = _async_job_create(uid)
    threading.Thread(target=_artwork_3d_batch_worker,
                     args=(jid, items, out_dir, session.get("username")), daemon=True).start()
    return jsonify({"job_id": jid, "count": len(items), "skipped": skipped})


@app.route("/api/artwork/3d/batch/status/<jid>")
@login_required
def api_artwork_3d_batch_status(jid):
    job = _async_job_get(jid)
    if not job:
        return jsonify({"status": "expired"}), 404
    if _owner_forbidden(job.get("user_id"), session["user_id"], session["role"]):
        return jsonify({"error": "Brak dostępu"}), 403
    r = job.get("result") or {}
    return jsonify({
        "status": job["status"], "pct": job.get("pct", 0),
        "step_label": job.get("step_label", ""),
        "error": job.get("error") if job["status"] == "error" else None,
        "done": r.get("done", 0), "total": r.get("total", 0),
        "items": [_batch3d_item_urls(it) for it in (r.get("items") or [])],
    })


@app.route("/api/artwork/3d/batch/zip/<jid>")
@login_required
def api_artwork_3d_batch_zip(jid):
    import io, zipfile
    from flask import send_file
    job = _async_job_get(jid)
    if not job:
        return jsonify({"error": "Zadanie wygasło"}), 404
    if _owner_forbidden(job.get("user_id"), session["user_id"], session["role"]):
        return jsonify({"error": "Brak dostępu"}), 403
    out_dir = os.path.abspath(os.path.join(app.config["UPLOAD_FOLDER"], "artwork3d"))
    items = (job.get("result") or {}).get("items") or []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for it in items:
            base = os.path.splitext(it.get("src_name") or "model")[0]
            for key, ext in (("glb_name", ".glb"), ("png_name", ".png")):
                name = it.get(key)
                if not name:
                    continue
                fpath = os.path.join(out_dir, secure_filename(name))
                if os.path.isfile(fpath):
                    zf.write(fpath, arcname=f"{base}{ext}")
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name="modele_3d.zip")


@app.route("/api/artwork/report/<int:cid>")
@login_required
def api_artwork_report_by_id(cid):
    uid  = session["user_id"]
    role = session["role"]
    db   = get_db()
    try:
        row = db.execute(
            "SELECT c.*, u.username FROM comparisons c JOIN users u ON c.user_id=u.id "
            "WHERE c.id=? AND c.doc_type='artwork' AND (c.is_deleted IS NULL OR c.is_deleted=0)",
            (cid,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono raportu"}), 404
        if row["user_id"] != uid and not row["is_public"] and not can_see_all(role):
            return jsonify({"error": "Brak dostępu"}), 403
        try:
            d = json.loads(row["result_json"] or "{}")
        except Exception:
            d = {}
        d["db_id"]      = cid
        d["file_a"]     = row["file_a"]
        d["file_b"]     = row["file_b"]
        d["username"]   = row["username"]
        d["created_at"] = row["created_at"]
        d["is_public"]  = row["is_public"]
        # Attach per-field comments
        comments_rows = db.execute(
            "SELECT field_name, comment, updated_at FROM artwork_field_comments WHERE comparison_id=?",
            (cid,)
        ).fetchall()
        d["field_comments"] = {r["field_name"]: {"comment": r["comment"], "updated_at": r["updated_at"]}
                               for r in comments_rows}
        return jsonify(d)
    finally:
        db.close()


@app.route("/api/artwork/report/<int:cid>/field-comment", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_field_comment_save(cid):
    """Upsert a comment on a single artwork field row."""
    uid  = session["user_id"]
    role = session["role"]
    db   = get_db()
    try:
        row = db.execute(
            "SELECT user_id, is_public FROM comparisons WHERE id=? AND doc_type='artwork'", (cid,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono raportu"}), 404
        if row["user_id"] != uid and not can_see_all(role):
            return jsonify({"error": "Brak dostępu"}), 403
        data = request.get_json(silent=True) or {}
        field_name = str(data.get("field_name") or "").strip()[:200]
        comment    = str(data.get("comment") or "").strip()[:2000]
        if not field_name:
            return jsonify({"error": "Wymagane: field_name"}), 400
        if not comment:
            db.execute(
                "DELETE FROM artwork_field_comments WHERE comparison_id=? AND field_name=?",
                (cid, field_name)
            )
        else:
            db.execute(
                """INSERT INTO artwork_field_comments(comparison_id, field_name, comment, created_by, updated_at)
                   VALUES(?,?,?,?,datetime('now'))
                   ON CONFLICT(comparison_id, field_name)
                   DO UPDATE SET comment=excluded.comment, updated_at=excluded.updated_at""",
                (cid, field_name, comment, uid)
            )
        db.commit()
        return jsonify({"ok": True, "field_name": field_name, "comment": comment})
    finally:
        db.close()


@app.route("/api/artwork/report/<int:cid>/visibility", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_report_visibility(cid):
    uid = session["user_id"]
    db  = get_db()
    try:
        row = db.execute("SELECT user_id, is_public FROM comparisons WHERE id=? AND doc_type='artwork'", (cid,)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono"}), 404
        if row["user_id"] != uid and not can_see_all(session["role"]):
            return jsonify({"error": "Brak dostępu"}), 403
        new_val = 0 if row["is_public"] else 1
        db.execute("UPDATE comparisons SET is_public=? WHERE id=?", (new_val, cid))
        db.commit()
        return jsonify({"ok": True, "is_public": new_val})
    finally:
        db.close()


@app.route("/api/artwork/page/<cache_id>/<int:page_idx>")
@login_required
def api_artwork_page_images(cache_id, page_idx):
    result = _cache_get(cache_id)
    if not result:
        return jsonify({"error": "Sesja wygasła — wgraj pliki ponownie"}), 404
    # IDOR: wpis w cache zawsze ma owner_id → odmawiamy też przy None/nieznanym.
    if _owner_forbidden(_cache_get_owner(cache_id), session["user_id"], session["role"]):
        return jsonify({"error": "Brak dostępu"}), 403
    return jsonify(result.page_images(page_idx))


@app.route("/api/artwork/pdf-pages", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_pdf_pages():
    """Zwraca liczbę stron i miniatury dla wgranego PDF.
    Jeśli page_idx podany → zwraca tylko jedną stronę (hires dla crop modal).
    """
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "Brak pliku"}), 400
    uid = session["user_id"]
    fname = secure_filename(f.filename) or "pages.pdf"
    path = os.path.join(app.config["UPLOAD_FOLDER"], f"{uid}_pages_{fname}")
    try:
        os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
        f.save(path)
        try:
            import fitz, base64
            try:
                scale_param = float(request.form.get("scale", 0.45))
            except (ValueError, TypeError):
                scale_param = 0.45
            scale = max(0.1, min(4.0, scale_param))
            page_idx_param = request.form.get("page_idx")
            doc = fitz.open(path)
            try:
                pages = []
                mat = fitz.Matrix(scale, scale)
                indices = range(len(doc))
                if page_idx_param is not None:
                    try:
                        idx = int(page_idx_param)
                    except (ValueError, TypeError):
                        idx = 0
                    indices = [idx] if 0 <= idx < len(doc) else []
                quality = 90 if scale >= 1.0 else 75
                for i in indices:
                    page = doc[i]
                    pix = page.get_pixmap(matrix=mat, alpha=False)
                    thumb = base64.b64encode(pix.tobytes("jpeg", jpg_quality=quality)).decode()
                    w_mm = round(page.rect.width / 72 * 25.4)
                    h_mm = round(page.rect.height / 72 * 25.4)
                    pages.append({
                        "index": i, "thumb": thumb,
                        "size_mm": f"{w_mm}×{h_mm}",
                        "width_px": pix.width,
                        "height_px": pix.height,
                    })
            finally:
                doc.close()
            return jsonify({"count": len(pages), "pages": pages})
        except Exception as e:
            return jsonify({"count": 1, "pages": [], "error": str(e)[:200]})
    finally:
        try:
            os.remove(path)
        except Exception:
            pass



@app.route("/api/artwork/history")
@login_required
def api_artwork_history():
    uid = session["user_id"]
    role = session["role"]
    db = get_db()
    try:
        if can_see_all(role):
            rows = db.execute("""
                SELECT c.id, c.file_a, c.file_b, c.status, c.diff_count,
                       c.created_at, u.username
                FROM comparisons c
                JOIN users u ON c.user_id = u.id
                WHERE c.doc_type = 'artwork' AND (c.is_deleted IS NULL OR c.is_deleted=0)
                ORDER BY c.created_at DESC LIMIT 50
            """).fetchall()
        else:
            rows = db.execute("""
                SELECT c.id, c.file_a, c.file_b, c.status, c.diff_count,
                       c.created_at, u.username
                FROM comparisons c
                JOIN users u ON c.user_id = u.id
                WHERE c.user_id = ? AND c.doc_type = 'artwork'
                  AND (c.is_deleted IS NULL OR c.is_deleted=0)
                ORDER BY c.created_at DESC LIMIT 20
            """, (uid,)).fetchall()
    finally:
        db.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/artwork/compare-multi", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_compare_multi():
    """Porownuje N artworkow — buduje macierz rozbieznosci pol."""
    files = request.files.getlist("files[]")
    if len(files) < 2:
        return jsonify({"error": "Wymagane co najmniej 2 pliki"}), 400
    if len(files) > 6:
        return jsonify({"error": "Maksymalnie 6 plikow naraz"}), 400

    ALLOWED = {".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif"}
    paths = []
    try:
        os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
        uid = session["user_id"]
        for i, f in enumerate(files):
            if not f or not f.filename:
                return jsonify({"error": "Pusty plik w żądaniu"}), 400
            ext = os.path.splitext(f.filename.lower())[1]
            if ext not in ALLOWED:
                return jsonify({"error": f"Plik '{f.filename}': nieobslugiwany format '{ext}'"}), 400
            fname = secure_filename(f.filename) or f"multi_{i}.pdf"
            path = os.path.join(app.config["UPLOAD_FOLDER"], f"{uid}_multi_{i}_{fname}")
            f.save(path)
            paths.append(path)

        from artwork_comparator import compare_multi_artworks
        with _HEAVY_SEM:
            result = compare_multi_artworks(paths)
        return jsonify(result)

    except Exception as e:
        import traceback as _tb
        app.logger.error("Multi-artwork compare error: %s", _tb.format_exc())
        logger.exception("Unexpected error %s", request.path)
        return jsonify({"error": "Błąd serwera"}), 500
    finally:
        for p in paths:
            try:
                os.remove(p)
            except Exception:
                pass


def _build_report_from_cache_or_db(cache_id, comp_id=None, user_id=None, role=None):
    """Próbuje zbudować raport z cache (obiekt) lub DB (result_json).

    Accepts either a cache_id (in-session string) or a comp_id (DB row integer).
    When comp_id is given, user_id and role are used for ownership enforcement.
    """
    from artwork_report_engine import build_report_from_comparison, build_demo_report
    # Batch pre-built report cache
    with _rpt_cache_lock:
        _rpt_cache = getattr(app, "_artwork_report_cache", {})
        if cache_id and cache_id in _rpt_cache:
            rpt = _rpt_cache[cache_id]
            # Enforce ownership on the pre-built report cache (mirrors artwork_report_page).
            if user_id is not None:
                _owner, _public = _report_owner(rpt)
                if _owner_forbidden(_owner, user_id, role, _public):
                    return None
            return rpt
    _cached_result = _cache_get(cache_id) if cache_id else None
    if _cached_result:
        # Enforce ownership on the in-memory result cache (IDOR guard for exports).
        if cache_id and user_id is not None:
            if _owner_forbidden(_cache_get_owner(cache_id), user_id, role):
                return None
        try:
            return build_report_from_comparison(_cached_result)
        except Exception:
            _cache_evict(cache_id)
    # Direct DB lookup by comparison ID
    if comp_id:
        try:
            db = get_db()
            try:
                row = db.execute(
                    "SELECT file_a, file_b, result_json, user_id AS owner_id, is_public"
                    " FROM comparisons WHERE id=? AND doc_type='artwork' AND (is_deleted IS NULL OR is_deleted=0)",
                    (comp_id,)
                ).fetchone()
            finally:
                db.close()
            if row:
                if user_id is not None and not can_see_all(role) and not row["is_public"] and row["owner_id"] != user_id:
                    return None
                d = json.loads(row["result_json"] or "{}")
                d["file_a"] = row["file_a"]
                d["file_b"] = row["file_b"]
                return build_report_from_comparison(d)
        except Exception:
            pass
    if cache_id:
        # Szukaj w DB po cache_id zapisanym w result_json
        try:
            db = get_db()
            try:
                row = db.execute(
                    "SELECT file_a, file_b, result_json, user_id, is_public FROM comparisons "
                    "WHERE doc_type='artwork' AND (is_deleted IS NULL OR is_deleted=0) "
                    "ORDER BY created_at DESC LIMIT 100"
                ).fetchall()
            finally:
                db.close()
            for r in row:
                try:
                    d = json.loads(r["result_json"] or "{}")
                    if d.get("cache_id") == cache_id:
                        d["file_a"] = r["file_a"]
                        d["file_b"] = r["file_b"]
                        d["_owner_id"] = r["user_id"]
                        d["_is_public"] = r["is_public"]
                        return build_report_from_comparison(d)
                except Exception:
                    pass
        except Exception:
            pass
    return None


@app.route("/artwork/report")
@login_required
def artwork_report_page():
    """Renderuje strukturalny raport porownania artworkow (z cache, DB lub demo)."""
    cache_id = request.args.get("cache_id")
    comp_id  = request.args.get("id", type=int)
    uid  = session["user_id"]
    role = session["role"]
    report = _build_report_from_cache_or_db(cache_id)
    # Enforce ownership on cache_id path (report to dict lub obiekt ArtworkReport).
    if report and cache_id:
        owner_id, is_public = _report_owner(report)
        if _owner_forbidden(owner_id, uid, role, is_public):
            report = None

    # Fallback: load from DB by comparison id
    if not report and comp_id:
        db   = get_db()
        try:
            row = db.execute(
                "SELECT * FROM comparisons WHERE id=? AND doc_type='artwork'", (comp_id,)
            ).fetchone()
            if row and (row["user_id"] == uid or row["is_public"] or can_see_all(role)):
                try:
                    d = json.loads(row["result_json"] or "{}")
                    d["file_a"] = row["file_a"]
                    d["file_b"] = row["file_b"]
                    from artwork_report_engine import build_report_from_comparison
                    report = build_report_from_comparison(d)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            db.close()

    if report is None:
        if cache_id or comp_id:
            return render_template(
                "artwork_report.html",
                report=None,
                cache_id=cache_id or "",
                comp_id=comp_id or 0,
                username=session["username"],
                role=session["role"],
                expired=True,
            )
        from artwork_report_engine import build_demo_report
        report = build_demo_report()
    return render_template(
        "artwork_report.html",
        report=report,
        cache_id=cache_id or "",
        comp_id=comp_id or 0,
        username=session["username"],
        role=session["role"],
        expired=False,
    )


@app.route("/artwork/report/export-html")
@login_required
def artwork_report_export_html():
    """Pobiera raport jako plik HTML."""
    from artwork_report_engine import build_demo_report, render_report_html
    cache_id = request.args.get("cache_id")
    comp_id  = request.args.get("id", type=int)
    report = _build_report_from_cache_or_db(cache_id, comp_id, user_id=session["user_id"], role=session["role"])
    if report is None:
        report = build_demo_report()
    html = render_report_html(report)
    safe_code = secure_filename(report.product_code or "artwork")
    filename = f"raport_{safe_code}_{report.analysis_date.replace('.', '')}.html"
    return (
        html,
        200,
        {
            "Content-Type": "text/html; charset=utf-8",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@app.route("/artwork/report/export-docx")
@login_required
def artwork_report_export_docx():
    """Pobiera raport jako plik Word (.docx)."""
    from artwork_report_engine import build_demo_report, export_to_docx
    cache_id = request.args.get("cache_id")
    comp_id  = request.args.get("id", type=int)
    report = _build_report_from_cache_or_db(cache_id, comp_id, user_id=session["user_id"], role=session["role"])
    if report is None:
        report = build_demo_report()
    docx_bytes = export_to_docx(report)
    safe_code = secure_filename(report.product_code or "artwork")
    filename = f"raport_{safe_code}_{report.analysis_date.replace('.', '')}.docx"
    return (
        docx_bytes,
        200,
        {
            "Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@app.route("/artwork/report/export-pdf")
@login_required
def artwork_report_export_pdf():
    """Pobiera raport artworku jako PDF (ReportLab) z wycinkami i kodami."""
    from artwork_report_engine import build_demo_report, export_to_pdf
    cache_id = request.args.get("cache_id")
    comp_id  = request.args.get("id", type=int)
    report = _build_report_from_cache_or_db(cache_id, comp_id, user_id=session["user_id"], role=session["role"])
    if report is None:
        report = build_demo_report()
    pdf_bytes = export_to_pdf(report)
    safe_code = secure_filename(report.product_code or "artwork")
    filename = f"raport_{safe_code}_{report.analysis_date.replace('.', '')}.pdf"
    return (
        pdf_bytes,
        200,
        {
            "Content-Type": "application/pdf",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )



# ── Artwork: Batch ────────────────────────────────────────────────────────────

@app.route("/artwork/batch")
@login_required
def artwork_batch_page():
    return render_template("artwork_batch.html",
                           username=session["username"], role=session["role"])


@app.route("/api/artwork/batch/pair", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_batch_pair():
    """Wgrywa pliki i zwraca wykryte pary."""
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "Brak plików"}), 400

    import hashlib
    _MAX_FILE_BYTES = 50 * 1024 * 1024
    uid = session.get("user_id", 0)
    ALLOWED_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".webp"}
    saved = []
    seen_hashes: set = set()
    for f in files[:100]:
        if not f or not f.filename:
            continue
        ext = os.path.splitext(f.filename.lower())[1]
        if ext not in ALLOWED_EXT:
            continue
        data = f.read(_MAX_FILE_BYTES + 1)
        if len(data) > _MAX_FILE_BYTES:
            continue
        # Walidacja zawartości: dla PDF wymagamy magic %PDF + brak hasła —
        # samo rozszerzenie .pdf nie wystarcza (można podrzucić nie-PDF do uploads/).
        # Obrazy (jpg/png/tiff/...) są dozwolone i nie mają nagłówka %PDF.
        if ext == ".pdf" and (not data.startswith(b"%PDF") or b"/Encrypt" in data[-4096:]):
            continue
        md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
        if md5 in seen_hashes:
            continue
        seen_hashes.add(md5)
        safe_name = secure_filename(f.filename) or "file"
        dest_a = os.path.join(app.config["UPLOAD_FOLDER"], f"{uid}_aw_a_{safe_name}")
        dest_b = os.path.join(app.config["UPLOAD_FOLDER"], f"{uid}_aw_b_{safe_name}")
        with open(dest_a, "wb") as fh:
            fh.write(data)
        import shutil; shutil.copy2(dest_a, dest_b)
        saved.append(f.filename)

    from artwork_batch_processor import auto_pair_artwork_files
    pairs = auto_pair_artwork_files(saved)
    return jsonify({"pairs": pairs, "total_files": len(saved)})


def _artwork_batch_worker(job_id, pairs, upload_dir, uid, use_ai, ai_model="claude-sonnet-4-6"):
    """Background thread: processes artwork batch and writes results to DB."""
    import uuid as _uuid
    from artwork_batch_processor import run_artwork_batch
    from artwork_comparator import set_ai_model
    set_ai_model(ai_model)
    results = []
    try:
        _db_start = get_db()
        try:
            _db_start.execute(
                "UPDATE artwork_batch_jobs SET status='running', updated_at=datetime('now') WHERE id=?",
                (job_id,)
            )
            _db_start.commit()
        finally:
            _db_start.close()

        for result in run_artwork_batch(pairs, upload_dir, uid, use_ai=use_ai):
            # Check for cancellation between pairs
            _db_chk = get_db()
            try:
                _job_status = _db_chk.execute(
                    "SELECT status FROM artwork_batch_jobs WHERE id=?", (job_id,)
                ).fetchone()
            finally:
                _db_chk.close()
            if _job_status and _job_status["status"] == "cancelled":
                return

            report_obj = result.pop("report_obj", None)
            result.pop("docx_bytes", None)
            cmp_data   = result.pop("cmp_data", None)

            # Cache report object for fast in-session access
            if report_obj:
                # Oznacz właściciela — bez tego raport z cache (owner_id None) omijał
                # kontrolę własności w artwork_report_page (IDOR po cache_id).
                _tag_report_owner(report_obj, uid)
                cache_id = _uuid.uuid4().hex[:16]
                with _rpt_cache_lock:
                    _rpt_cache = getattr(app, "_artwork_report_cache", {})
                    _rpt_cache[cache_id] = report_obj
                    if len(_rpt_cache) > 200:
                        del _rpt_cache[next(iter(_rpt_cache))]
                    app._artwork_report_cache = _rpt_cache
                result["cache_id"] = cache_id

            # Persist to comparisons table so report survives session expiry
            if result.get("status") == "done" and cmp_data:
                try:
                    cmp_data["cache_id"] = result.get("cache_id")
                    cmp_data["status"]   = result.get("risk", "ok")
                    cmp_data["diff_count"] = result.get("critical", 0) + result.get("warnings", 0)
                    cid = _save_comparison(
                        uid, "artwork",
                        result["file_a"], result["file_b"],
                        cmp_data
                    )
                    result["db_id"] = cid
                    result["db_save_failed"] = False
                except Exception as _se:
                    logger.warning(f"Could not save batch result to DB: {_se}")
                    result["db_save_failed"] = True

            results.append(result)
            failed = sum(1 for r in results if r.get("status") == "error")
            import gzip as _gz, base64 as _b64
            _raw = json.dumps(results, ensure_ascii=False, default=str).encode()
            _compressed = "gz:" + _b64.b64encode(_gz.compress(_raw, compresslevel=6)).decode()
            _db_upd = get_db()
            try:
                _db_upd.execute(
                    """UPDATE artwork_batch_jobs
                       SET done=?, failed=?, results_json=?, updated_at=datetime('now')
                       WHERE id=?""",
                    (len(results), failed, _compressed, job_id)
                )
                _db_upd.commit()
            finally:
                _db_upd.close()

        _db_done = get_db()
        try:
            _db_done.execute(
                "UPDATE artwork_batch_jobs SET status='done', updated_at=datetime('now') WHERE id=?",
                (job_id,)
            )
            _db_done.commit()
        finally:
            _db_done.close()
    except Exception as e:
        logger.error(f"_artwork_batch_worker job {job_id} failed: {e}")
        try:
            _db_err = get_db()
            try:
                _db_err.execute(
                    "UPDATE artwork_batch_jobs SET status='failed', updated_at=datetime('now') WHERE id=?",
                    (job_id,)
                )
                _db_err.commit()
            finally:
                _db_err.close()
        except Exception:
            pass


@app.route("/api/artwork/batch/start", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_batch_start():
    """Tworzy asynchroniczne zadanie batch i uruchamia je w tle."""
    import threading
    pairs_raw = request.form.get("pairs", "[]")
    use_ai = request.form.get("use_ai", "1") == "1"
    from artwork_comparator import AVAILABLE_MODELS
    ai_model = request.form.get("ai_model", "claude-sonnet-4-6")
    _valid_batch_models = {m[0] for m in AVAILABLE_MODELS}
    if ai_model not in _valid_batch_models:
        return jsonify({"error": f"Nieznany model AI: {ai_model}"}), 400
    if len(pairs_raw) > 500_000:
        return jsonify({"error": "Za duże dane wejściowe"}), 400
    try:
        pairs = json.loads(pairs_raw)
    except Exception:
        return jsonify({"error": "Błąd parsowania par"}), 400
    if not pairs:
        return jsonify({"error": "Brak par do porównania"}), 400
    if len(pairs) > 500:
        return jsonify({"error": "Zbyt wiele par — limit wynosi 500"}), 400

    uid = session.get("user_id", 0)
    upload_dir = app.config["UPLOAD_FOLDER"]

    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO artwork_batch_jobs(user_id, status, total, pairs_json) VALUES(?,?,?,?)",
            (uid, "pending", len(pairs), json.dumps(pairs))
        )
        db.commit()
        job_id = cur.lastrowid
    finally:
        db.close()

    # Poziom 1 kolejkowania: jeśli REDIS_URL jest ustawiony, zadanie liczy osobny
    # worker (web wolny). Bez Redis — fallback na wątek (dotychczasowe zachowanie).
    # Kontrakt postępu (artwork_batch_jobs) bez zmian. Patrz docs/KOLEJKOWANIE.md.
    import jobs as _jobs
    _jobs.enqueue(_artwork_batch_worker, job_id, pairs, upload_dir, uid, use_ai, ai_model)

    return jsonify({"job_id": job_id, "total": len(pairs)})


@app.route("/api/artwork/batch/status/<int:job_id>", methods=["GET"])
@login_required
def api_artwork_batch_status(job_id):
    """Zwraca aktualny stan zadania batch."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT status, total, done, failed, results_json FROM artwork_batch_jobs WHERE id=? AND user_id=?",
            (job_id, session["user_id"])
        ).fetchone()
    finally:
        db.close()
    if not row:
        return jsonify({"error": "Nie znaleziono zadania"}), 404
    _rj = row["results_json"] or "[]"
    try:
        if _rj.startswith("gz:"):
            import gzip as _gz, base64 as _b64
            _rj = _gz.decompress(_b64.b64decode(_rj[3:])).decode()
        results = json.loads(_rj)
    except Exception:
        results = []
    import hashlib as _hl2
    checksum = _hl2.sha256(_rj.encode()).hexdigest()[:16] if row["status"] in ("done", "failed") else None
    return jsonify({
        "status": row["status"],
        "total": row["total"],
        "done": row["done"],
        "failed": row["failed"],
        "last_result": results[-1] if results else None,
        "results": results if row["status"] in ("done", "failed") else [],
        "checksum": checksum,
    })


@app.route("/api/artwork/batch/cancel/<int:job_id>", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_batch_cancel(job_id):
    """Anuluje zadanie batch — worker zatrzymuje się po bieżącej parze."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT status FROM artwork_batch_jobs WHERE id=? AND user_id=?",
            (job_id, session["user_id"])
        ).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono zadania"}), 404
        if row["status"] not in ("pending", "running"):
            return jsonify({"error": "Zadanie już zakończone"}), 400
        db.execute(
            "UPDATE artwork_batch_jobs SET status='cancelled', updated_at=datetime('now') WHERE id=?",
            (job_id,)
        )
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/artwork/batch/run", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_batch_run():
    """Uruchamia batch porównanie artworków. Streamuje NDJSON."""
    import json as _json

    pairs_raw = request.form.get("pairs", "[]")
    use_ai    = request.form.get("use_ai", "1") == "1"
    if len(pairs_raw) > 500_000:
        return jsonify({"error": "Za duże dane wejściowe"}), 400
    try:
        pairs = _json.loads(pairs_raw)
    except Exception:
        return jsonify({"error": "Błąd parsowania par"}), 400
    if len(pairs) > 500:
        return jsonify({"error": "Zbyt wiele par — limit wynosi 500"}), 400

    uid = session.get("user_id", 0)
    upload_dir = app.config["UPLOAD_FOLDER"]

    from artwork_batch_processor import run_artwork_batch

    def generate():
        for result in run_artwork_batch(pairs, upload_dir, uid, use_ai=use_ai):
            # Cache the pre-built report for individual report view + docx export
            report_obj = result.pop("report_obj", None)
            result.pop("docx_bytes", None)
            if report_obj:
                import uuid
                cache_id = str(uuid.uuid4()).replace("-", "")[:16]
                # Tag właściciela dla kontroli dostępu (IDOR guard w _build_report_from_cache_or_db).
                _tag_report_owner(report_obj, uid)
                with _rpt_cache_lock:
                    _rpt_cache = getattr(app, "_artwork_report_cache", {})
                    _rpt_cache[cache_id] = report_obj
                    if len(_rpt_cache) > 200:
                        del _rpt_cache[next(iter(_rpt_cache))]
                    app._artwork_report_cache = _rpt_cache
                result["cache_id"] = cache_id
            yield _json.dumps(result, ensure_ascii=False, default=str) + "\n"

    return app.response_class(generate(), mimetype="application/x-ndjson")


@app.route("/api/artwork/batch/zip", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_batch_zip():
    """Pobiera ZIP ze wszystkimi raportami Word batch."""
    import json as _json
    results_raw = request.form.get("results", "[]")
    if len(results_raw) > 500_000:
        return jsonify({"error": "Za duże dane wejściowe"}), 400
    try:
        results_meta = _json.loads(results_raw)
    except Exception:
        return jsonify({"error": "Błąd parsowania"}), 400
    if len(results_meta) > 500:
        return jsonify({"error": "Zbyt wiele wyników — limit wynosi 500"}), 400

    with _rpt_cache_lock:
        report_cache = dict(getattr(app, "_artwork_report_cache", {}))
    results_full = []
    for meta in results_meta:
        cid = meta.get("cache_id")
        report = report_cache.get(cid) if cid else None
        results_full.append({**meta, "report_obj": report})

    from artwork_batch_processor import build_artwork_batch_zip
    from artwork_report_engine import export_to_docx

    # Generate docx for each
    for r in results_full:
        if r.get("report_obj") and not r.get("docx_bytes"):
            try:
                r["docx_bytes"] = export_to_docx(r["report_obj"])
            except Exception:
                pass

    zip_bytes = build_artwork_batch_zip(results_full)
    return (
        zip_bytes, 200,
        {
            "Content-Type": "application/zip",
            "Content-Disposition": "attachment; filename=raporty_artworkow.zip",
        },
    )


# ── Artwork: manualne zaznaczanie stref ───────────────────────────────────────

@app.route("/artwork/zone-select")
@login_required
def artwork_zone_select():
    """Interaktywny selektor stref do manualnego porównania regionów artworku."""
    cache_id = request.args.get("cache_id", "")
    result   = _cache_get(cache_id)
    if not result:
        flash("Brak danych porównania — najpierw wykonaj porównanie artworków.", "error")
        # Endpoint strony /artwork nazywa się artwork_page — "artwork_compare_page"
        # nie istnieje i url_for rzucał BuildError, czyli 500 zamiast powrotu.
        return redirect(url_for("artwork_page"))

    page0    = result.page_diffs[0] if result.page_diffs else None
    img_a_b64 = page0.img_a_b64 if page0 else ""
    img_b_b64 = page0.img_b_b64 if page0 else ""

    from artwork_zone_comparator import list_zone_templates
    templates = list_zone_templates()

    return render_template(
        "artwork_zone_select.html",
        cache_id  = cache_id,
        file_a    = result.file_a,
        file_b    = result.file_b,
        img_a_b64 = img_a_b64 or "",
        img_b_b64 = img_b_b64 or "",
        templates = templates,
    )


@app.route("/api/artwork/zone-compare", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_zone_compare():
    """Porównuje ręcznie zaznaczone pary stref na artworkach."""
    data      = request.get_json(silent=True) or {}
    cache_id  = data.get("cache_id", "")
    zones     = data.get("zones", [])
    save_tpl  = data.get("save_template", False)
    tpl_name  = str(data.get("template_name") or "").strip()[:200]

    result = _cache_get(cache_id)
    if not result:
        return jsonify({"error": "Brak danych porównania w cache"}), 404
    if not zones:
        return jsonify({"error": "Brak zdefiniowanych stref"}), 400

    page0     = result.page_diffs[0] if result.page_diffs else None
    img_a_b64 = page0.img_a_b64 if page0 else ""
    img_b_b64 = page0.img_b_b64 if page0 else ""

    from artwork_zone_comparator import compare_zone_pairs, save_zone_template
    results = compare_zone_pairs(img_a_b64, img_b_b64, zones)

    if save_tpl and tpl_name:
        try:
            save_zone_template(tpl_name, zones, result.file_a, result.file_b)
        except Exception:
            pass

    return jsonify({"ok": True, "results": results})


@app.route("/api/artwork/field-queue/init", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_field_queue_init():
    """
    Initialise a field comparison queue for one artwork pair.
    Accepts same FormData as split-compare (supplier_pdf + master_configs_json
    + pairs_json), but only processes the FIRST pair.
    Returns {queue_id, fields: [{field_name, display_name, crop_a_b64, ...}]}
    """
    import traceback as _tb
    supplier_path = None
    comp_path = None
    master_path_val = None
    is_temp_master = True

    def _cleanup_temps():
        for _p in [supplier_path, comp_path,
                   master_path_val if is_temp_master else None]:
            try:
                if _p and os.path.exists(_p):
                    os.remove(_p)
            except Exception:
                pass

    try:
        supplier_file = request.files.get("supplier_pdf")
        master_configs_str = request.form.get("master_configs_json", "")
        pairs_json_str = request.form.get("pairs_json", "")
        masters_files = request.files.getlist("masters[]")

        if not supplier_file:
            return jsonify({"error": "Brak pliku dostawcy"}), 400

        uid = session["user_id"]
        upload_dir = app.config["UPLOAD_FOLDER"]
        os.makedirs(upload_dir, exist_ok=True)

        supplier_path = os.path.join(upload_dir,
                                     f"{uid}_fq_sup_{secure_filename(supplier_file.filename) or 'supplier.pdf'}")
        supplier_file.save(supplier_path)

        # Resolve master
        profile_id = None
        if master_configs_str:
            try:
                configs = json.loads(master_configs_str)
            except Exception:
                return jsonify({"error": "Błąd parsowania master_configs_json"}), 400
            cfg = configs[0] if configs else {}
            if cfg.get("type") == "profile":
                profile_id = cfg.get("profile_id")
                if not profile_id:
                    _cleanup_temps()
                    return jsonify({"error": "Brakujący profile_id w konfiguracji mastera"}), 400
                db2 = get_db()
                try:
                    row = db2.execute(
                        "SELECT name, master_pdf_path FROM artwork_profiles WHERE id=?",
                        (profile_id,)).fetchone()
                    if not row or not row["master_pdf_path"]:
                        _cleanup_temps()
                        return jsonify({"error": "Master z biblioteki nie ma pliku PDF"}), 404
                    master_path_val = _resolve_master_path(row["master_pdf_path"],
                                                           profile_id=profile_id, db=db2)
                    is_temp_master = False
                finally:
                    db2.close()
            else:
                fi = cfg.get("file_idx", 0)
                try:
                    fi = int(fi)
                except (TypeError, ValueError):
                    fi = 0
                if 0 <= fi < len(masters_files):
                    mf = masters_files[fi]
                    master_path_val = os.path.join(upload_dir,
                                                   f"{uid}_fq_mst_{secure_filename(mf.filename or '') or 'master.pdf'}")
                    mf.save(master_path_val)
        elif masters_files:
            mf = masters_files[0]
            master_path_val = os.path.join(upload_dir,
                                           f"{uid}_fq_mst_{secure_filename(mf.filename or '') or 'master.pdf'}")
            mf.save(master_path_val)

        if not master_path_val or not os.path.exists(master_path_val):
            _cleanup_temps()
            return jsonify({"error": "Nie znaleziono pliku mastera"}), 404

        # Resolve supplier page
        page_a = 0
        page_b = 0
        if pairs_json_str:
            try:
                pairs = json.loads(pairs_json_str)
            except Exception:
                pairs = []
            if pairs:
                try:
                    page_b = max(0, int(pairs[0].get("page", 0)))
                except (TypeError, ValueError):
                    page_b = 0

        # Extract supplier page as separate PDF
        from pypdf import PdfReader, PdfWriter
        reader = PdfReader(supplier_path)
        num_pages = len(reader.pages)
        if page_b >= num_pages:
            _cleanup_temps()
            return jsonify({"error": f"Strona {page_b} nie istnieje w pliku dostawcy ({num_pages} stron)"}), 400
        writer = PdfWriter()
        writer.add_page(reader.pages[page_b])
        comp_path = os.path.join(upload_dir, f"{uid}_fq_pg{page_b}.pdf")
        with open(comp_path, "wb") as fp:
            writer.write(fp)

        # Load profile
        from artwork_comparator import (field_queue_init, _load_artwork_profile_by_id)
        profile = _load_artwork_profile_by_id(profile_id) if profile_id else None
        if not profile or not profile.get("fields"):
            _cleanup_temps()
            return jsonify({"error": "Profil nie ma zdefiniowanych pól"}), 400

        # Pull effective per-field supplier coords for pair 0 (auto-located + manual)
        field_overrides_str = request.form.get("field_overrides_json", "")
        try:
            field_overrides_all = json.loads(field_overrides_str) if field_overrides_str else {}
        except Exception:
            field_overrides_all = {}
        field_overrides = (field_overrides_all.get("0")
                           or field_overrides_all.get(0)
                           or {})

        _temp_files = [supplier_path, comp_path,
                       master_path_val if is_temp_master else None]
        import time as _time
        _t0 = _time.monotonic()
        _fq_db = get_db()
        try:
            _ensure_artwork_profile_tables(_fq_db)
            _fq_stats = _load_artwork_field_stats(_fq_db)
        finally:
            _fq_db.close()
        result = field_queue_init(master_path_val, comp_path, profile,
                                  page_a=page_a, page_b=0,
                                  field_overrides=field_overrides,
                                  temp_files=_temp_files,
                                  field_stats=_fq_stats)
        _dur = int((_time.monotonic() - _t0) * 1000)
        if "error" in result:
            for p in _temp_files:
                try:
                    if p and os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
            return jsonify(result), 400

        _log_audit("field_queue_init",
                   detail=f"profil={profile.get('name','')} pól={len(result.get('fields',[]))}",
                   duration_ms=_dur,
                   extra={"profile_id": profile_id,
                          "profile_name": profile.get("name"),
                          "field_count": len(result.get("fields", [])),
                          "queue_id": result.get("queue_id"),
                          "duration_ms": _dur})
        result["profile_name"] = profile.get("name", "")
        return jsonify(result)

    except Exception as exc:
        logger.error("field_queue_init error: %s\n%s", exc, _tb.format_exc())
        _cleanup_temps()
        return jsonify({"error": "Błąd inicjalizacji kolejki pól — spróbuj ponownie."}), 500


@app.route("/api/artwork/field-queue/compare", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_field_queue_compare():
    """
    Compare a single field from an initialised queue.
    Body: {queue_id, field_name, override_b: {x1_pct,y1_pct,x2_pct,y2_pct}|null}
    Returns the comparison row dict.
    """
    field_name = None
    try:
        data = request.get_json(force=True) or {}
        queue_id = data.get("queue_id")
        field_name = data.get("field_name")
        override_b = data.get("override_b")  # coords or null

        if not queue_id or not field_name:
            return jsonify({"error": "queue_id i field_name są wymagane"}), 400

        import time as _time
        _t0 = _time.monotonic()
        from artwork_comparator import field_queue_compare_one
        result = field_queue_compare_one(queue_id, field_name, override_b)
        _dur = int((_time.monotonic() - _t0) * 1000)
        if isinstance(result, dict) and "error" in result:
            return jsonify(result), 400
        _log_audit("field_compare",
                   detail=f"{field_name}: {'RÓŻNICA' if result.get('changed') else 'OK'}",
                   duration_ms=_dur,
                   extra={"field": field_name,
                          "changed": result.get("changed"),
                          "severity": result.get("severity"),
                          "val_a": str(result.get("val_a",""))[:80],
                          "val_b": str(result.get("val_b",""))[:80],
                          "duration_ms": _dur})
        return jsonify(result)
    except Exception as exc:
        logger.error("field_queue_compare error: %s", exc)
        _log_audit("field_compare_error", detail=f"{field_name}: {str(exc)[:150]}")
        return jsonify({"error": "Błąd porównania pola — spróbuj ponownie."}), 500


@app.route("/api/artwork/field-queue/<queue_id>", methods=["DELETE"])
@login_required
@csrf_protect
def api_artwork_field_queue_destroy(queue_id):
    """Release queue cache when operator is done."""
    from artwork_comparator import field_queue_destroy
    field_queue_destroy(queue_id)
    return jsonify({"ok": True})


@app.route("/api/artwork/field-queue/finalize", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_field_queue_finalize():
    """Build + save the full report from the queue's ALREADY-computed field rows,
    without re-running the comparison. Body: {queue_id, fields:[row…], file_a, file_b}.
    Returns {cache_id} → frontend opens /artwork/report?cache_id=…"""
    uid = session["user_id"]
    data = request.get_json(force=True) or {}
    queue_id = data.get("queue_id")
    field_rows = data.get("fields") or []
    file_a = (data.get("file_a") or "").strip()
    file_b = (data.get("file_b") or "").strip()
    if not queue_id:
        return jsonify({"error": "queue_id wymagany"}), 400
    try:
        from artwork_comparator import field_queue_finalize
        result = field_queue_finalize(queue_id, field_rows, file_a=file_a, file_b=file_b)
    except Exception:
        logger.exception("field_queue_finalize error queue=%s", queue_id)
        return jsonify({"error": "Błąd budowy raportu z kolejki."}), 500
    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 400
    import hashlib as _hl, time as _t
    cache_id = _hl.sha256(f"{uid}_{_t.time()}_fqf".encode()).hexdigest()[:12]
    _cache_put(cache_id, result, owner_id=uid)
    try:
        full = result.to_dict(include_images=False)
        full["cache_id"] = cache_id
        full["status"] = result.risk_level
        full["diff_count"] = result.critical_count + result.important_count
        _save_comparison(uid, "artwork", file_a or "wzorzec", file_b or "dostawca", full)
    except Exception:
        logger.warning("finalize save_comparison failed queue=%s", queue_id, exc_info=True)
    _log_audit("artwork_field_queue_finalize", session.get("username"),
               f"queue={queue_id} crit={result.critical_count} imp={result.important_count}")
    return jsonify({"cache_id": cache_id, "risk_level": result.risk_level,
                    "critical_count": result.critical_count,
                    "important_count": result.important_count,
                    "ok_count": result.ok_count})


@app.route("/api/artwork/zone-templates", methods=["GET", "DELETE"])
@login_required
@csrf_protect
def api_artwork_zone_templates():
    """Zarządza zapisanymi szablonami stref."""
    from artwork_zone_comparator import list_zone_templates, delete_zone_template
    if request.method == "DELETE":
        if role_level(session.get("role", "user")) < role_level("manager"):
            return jsonify({"error": "Brak uprawnień"}), 403
        tid = request.args.get("id", "")
        ok  = delete_zone_template(tid)
        return jsonify({"ok": ok})
    return jsonify({"templates": list_zone_templates()})



@app.route("/api/ai/status")
@login_required
def ai_status():
    try:
        from ai_validator import is_api_key_set
        return jsonify(is_api_key_set())
    except Exception as e:
        logger.exception("Unexpected error %s", request.path)
        return jsonify({"ok": False, "error": "Błąd serwera"}), 500


@app.route("/api/ai/config", methods=["GET", "POST"])
@require_role("admin")
@csrf_protect
def ai_config():
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        api_key = str(data.get("api_key") or "").strip()
        if not api_key or not api_key.startswith("sk-ant-") or len(api_key) > 500:
            return jsonify({"error": "Nieprawidłowy klucz"}), 400
        db = get_db()
        try:
            db.execute(
                "INSERT INTO settings(category,key,value) VALUES(?,?,?) "
                "ON CONFLICT(category,key) DO UPDATE SET value=EXCLUDED.value",
                ("ai", "anthropic_api_key", api_key))
            db.commit()
        finally:
            db.close()
        os.environ["ANTHROPIC_API_KEY"] = api_key
        return jsonify({"ok": True})
    key_env = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    return jsonify({"api_key_set": key_env, "source": "env" if key_env else "none",
                    "model": "claude-sonnet-4-20250514"})


@app.route("/api/ai/budget", methods=["GET", "POST"])
@login_required
@csrf_protect
def api_ai_budget():
    """Miesięczny budżet AI: GET zwraca status (wydane/limit/%/flagi),
    POST (admin) ustawia limit USD (0 = brak limitu)."""
    from api_usage_tracker import get_budget_status, set_budget_limit
    if request.method == "POST":
        if session.get("role") != "admin":
            return jsonify({"error": "Tylko administrator może zmienić limit"}), 403
        data = request.get_json(silent=True) or {}
        try:
            limit = max(0.0, float(data.get("limit_usd", 0)))
        except (TypeError, ValueError):
            return jsonify({"error": "Nieprawidłowa kwota"}), 400
        if limit > 100000:
            return jsonify({"error": "Kwota zbyt duża"}), 400
        set_budget_limit(limit)
        _log_audit("ai_budget_set", session.get("username"), f"limit={limit:.2f} USD/mc")
        return jsonify({"ok": True, **get_budget_status()})
    return jsonify(get_budget_status())


@app.route("/api/ai/learn-document", methods=["POST"])
@login_required
@csrf_protect
def ai_learn_document():
    f = request.files.get("file")
    _pdf_err = _validate_pdf_upload(f)
    if _pdf_err:
        return jsonify({"error": _pdf_err}), 400
    uid = session["user_id"]
    if _check_and_record_api_rate(uid):
        return jsonify({"error": "Przekroczono limit zapytań. Poczekaj chwilę."}), 429
    _sname = secure_filename(f.filename) or "document.pdf"
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    path = os.path.join(app.config["UPLOAD_FOLDER"],
                        f"{uid}_learn_{_sname}")
    f.save(path)
    try:
        from ai_learning import learn_from_document
        _learn_doc_type = request.form.get("doc_type", "auto")
        if _learn_doc_type not in ("auto", "PO", "PI", "CI", "PL", "SAD", "BL", "WZ", "FV"):
            _learn_doc_type = "auto"
        return jsonify(learn_from_document(path, _learn_doc_type))
    except Exception as e:
        logger.exception("Unexpected error %s", request.path)
        return jsonify({"error": "Błąd serwera"}), 500
    finally:
        try: os.remove(path)
        except Exception: pass


@app.route("/api/ai/apply-profile", methods=["POST"])
@require_role("manager")
@csrf_protect
def ai_apply_profile():
    data = request.get_json(force=True, silent=True) or {}
    code = str(data.get("supplier_code") or "").upper()
    proposal = data.get("proposal")
    if not code or not proposal:
        return jsonify({"error": "Wymagane: supplier_code, proposal"}), 400
    try:
        from ai_learning import apply_learned_profile
        return jsonify(apply_learned_profile(code, proposal,
                       approved_by=session.get("username", "unknown")))
    except Exception as e:
        logger.exception("Unexpected error %s", request.path)
        return jsonify({"error": "Błąd serwera"}), 500


@app.route("/api/ai/learn-comparison/<int:cid>", methods=["POST"])
@login_required
@csrf_protect
def ai_learn_comparison(cid):
    uid = session["user_id"]
    role = session.get("role", "user")
    db = get_db()
    try:
        row = db.execute("SELECT * FROM comparisons WHERE id=?", (cid,)).fetchone()
    finally:
        db.close()
    if not row:
        return jsonify({"error": "Nie znaleziono porównania"}), 404
    if row["user_id"] != uid and not can_see_all(role):
        return jsonify({"error": "Brak dostępu"}), 403
    try:
        cr = json.loads(row["result_json"] or "{}")
    except (ValueError, TypeError):
        return jsonify({"error": "Dane porównania uszkodzone"}), 500
    sc = row["supplier_code"] or cr.get("supplier_detected", "")
    if not sc:
        return jsonify({"status": "no_supplier", "message": "Brak kodu dostawcy"}), 200
    try:
        from ai_learning import learn_from_comparison
        return jsonify(learn_from_comparison(cr, sc))
    except Exception as e:
        logger.exception("Unexpected error %s", request.path)
        return jsonify({"error": "Błąd serwera"}), 500


@app.route("/api/ai/apply-suggestions", methods=["POST"])
@require_role("manager")
@csrf_protect
def ai_apply_suggestions():
    data = request.get_json(force=True, silent=True) or {}
    sc = str(data.get("supplier_code") or "").upper()
    suggestions = data.get("suggestions", {})
    approved = data.get("approved_fields", None)
    if not sc or not suggestions:
        return jsonify({"error": "Wymagane: supplier_code, suggestions"}), 400
    try:
        from ai_learning import apply_comparison_suggestions
        return jsonify(apply_comparison_suggestions(sc, suggestions,
                       approved_fields=approved,
                       approved_by=session.get("username", "unknown")))
    except Exception as e:
        logger.exception("Unexpected error %s", request.path)
        return jsonify({"error": "Błąd serwera"}), 500


@app.route("/api/ai/pending-updates")
@require_role("manager")
def ai_pending_updates():
    try:
        from ai_learning import get_pending_updates
        return jsonify(get_pending_updates())
    except Exception as e:
        logger.exception("Unexpected error %s", request.path)
        return jsonify({"error": "Błąd serwera"}), 500


@app.route("/api/ai/pending-updates/<pending_id>", methods=["DELETE"])
@require_role("manager")
@csrf_protect
def ai_dismiss_pending(pending_id):
    try:
        from ai_learning import dismiss_pending
        return jsonify({"ok": dismiss_pending(pending_id)})
    except Exception as e:
        logger.exception("Unexpected error %s", request.path)
        return jsonify({"error": "Błąd serwera"}), 500


@app.route("/api/ai/learning-stats")
@login_required
def ai_learning_stats():
    try:
        from ai_learning import get_learning_stats
        return jsonify(get_learning_stats())
    except Exception as e:
        logger.exception("Unexpected error %s", request.path)
        return jsonify({"error": "Błąd serwera"}), 500


def _trigger_background_learning(result_dict: dict):
    supplier = result_dict.get("supplier_detected", "")
    if not supplier or result_dict.get("total_errors", 0) == 0:
        return
    try:
        import threading
        from ai_learning import learn_from_comparison
        threading.Thread(target=lambda: learn_from_comparison(result_dict, supplier),
                         daemon=True).start()
    except Exception:
        pass



# ─────────────────────────────────────────────────────────────────────────────
# KOMENTARZE I AKCEPTACJA RAPORTÓW
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/comparison/<int:cid>/comment", methods=["POST"])
@login_required
@csrf_protect
def add_comment(cid):
    uid = session["user_id"]
    role = session.get("role", "user")
    data = request.get_json(silent=True) or {}
    comment = str(data.get("comment") or "").strip()[:2000]
    ctype = (data.get("type") or "note")
    if ctype not in ("note", "issue", "resolved"):
        ctype = "note"
    if not comment:
        return jsonify({"error": "Brak treści komentarza"}), 400
    db = get_db()
    try:
        row = db.execute("SELECT id, user_id FROM comparisons WHERE id=?", (cid,)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono"}), 404
        if not can_see_all(role) and row["user_id"] != uid:
            return jsonify({"error": "Brak dostępu"}), 403
        _cur = db.execute(
            "INSERT INTO comparison_comments(comparison_id,user_id,comment,comment_type) VALUES(?,?,?,?)",
            (cid, uid, comment, ctype)
        )
        db.commit()
        # Zwróć komentarz z username
        new_id = _cur.lastrowid
        row2 = db.execute("""
            SELECT cc.*, u.username
            FROM comparison_comments cc JOIN users u ON cc.user_id=u.id
            WHERE cc.id=?
        """, (new_id,)).fetchone()
    finally:
        db.close()
    return jsonify({"ok": True, "comment": dict(row2) if row2 else {}})


@app.route("/api/comparison/<int:cid>/comments")
@login_required
def get_comments(cid):
    uid = session["user_id"]
    role = session["role"]
    db = get_db()
    try:
        row = db.execute("SELECT user_id FROM comparisons WHERE id=?", (cid,)).fetchone()
        if not row:
            return jsonify([])
        if not can_see_all(role) and row["user_id"] != uid:
            return jsonify({"error": "Brak dostępu"}), 403
        rows = db.execute("""
            SELECT cc.*, u.username
            FROM comparison_comments cc JOIN users u ON cc.user_id=u.id
            WHERE cc.comparison_id=?
            ORDER BY cc.created_at ASC
        """, (cid,)).fetchall()
    finally:
        db.close()
    return jsonify([dict(r) for r in rows])


# ─────────────────────────────────────────────────────────────────────────────
# WYSZUKIWANIE PO NUMERZE PO
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/search")
@login_required
def api_search():
    q = request.args.get("q", "").strip()[:200]
    uid = session["user_id"]
    role = session["role"]
    if not q or len(q) < 3:
        return jsonify([])
    db = get_db()
    q_esc = escape_like(q)
    pattern = f"%{q_esc}%"
    try:
        if can_see_all(role):
            rows = db.execute("""
                SELECT c.id, c.user_id, c.doc_type, c.file_a, c.file_b, c.status,
                       c.diff_count, c.created_at, c.po_number, c.supplier_code, u.username
                FROM comparisons c JOIN users u ON c.user_id=u.id
                WHERE c.po_number LIKE ? ESCAPE '\\'
                   OR c.file_a LIKE ? ESCAPE '\\' OR c.file_b LIKE ? ESCAPE '\\'
                   OR c.supplier_code LIKE ? ESCAPE '\\'
                ORDER BY c.created_at DESC LIMIT 30
            """, (pattern, pattern, pattern, pattern)).fetchall()
        else:
            rows = db.execute("""
                SELECT c.id, c.user_id, c.doc_type, c.file_a, c.file_b, c.status,
                       c.diff_count, c.created_at, c.po_number, c.supplier_code, u.username
                FROM comparisons c JOIN users u ON c.user_id=u.id
                WHERE c.user_id=? AND (
                    c.po_number LIKE ? ESCAPE '\\'
                    OR c.file_a LIKE ? ESCAPE '\\' OR c.file_b LIKE ? ESCAPE '\\'
                    OR c.supplier_code LIKE ? ESCAPE '\\')
                ORDER BY c.created_at DESC LIMIT 30
            """, (uid, pattern, pattern, pattern, pattern)).fetchall()
    finally:
        db.close()
    # result_json (pełny raport, często wielomegabajtowy) nie jest już pobierany —
    # wcześniej SELECT c.* ciągnął go dla 30 wierszy tylko po to, żeby go tu usunąć.
    return jsonify([dict(r) for r in rows])


# ─────────────────────────────────────────────────────────────────────────────
# WALIDACJA SUM KONTROLNYCH PER POZYCJA
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/email/config", methods=["GET", "POST"])
@require_role("admin")
@csrf_protect
def email_config():
    """Konfiguracja SMTP dla powiadomień email."""
    db = get_db()
    try:
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            _email_max_lens = {"smtp_host": 255, "smtp_port": 10, "smtp_user": 254,
                               "smtp_pass": 500, "smtp_from": 254, "notify_on_error": 10,
                               "notify_recipients": 2000}
            for key, val in data.items():
                if key in _email_max_lens:
                    db.execute(
                        "INSERT INTO settings(category,key,value) VALUES('email',?,?) "
                        "ON CONFLICT(category,key) DO UPDATE SET value=EXCLUDED.value",
                        (key, str(val)[:_email_max_lens[key]])
                    )
            db.commit()
            return jsonify({"ok": True})
        # GET
        rows = db.execute("SELECT key,value FROM settings WHERE category='email'").fetchall()
    finally:
        db.close()
    cfg = {r["key"]: r["value"] for r in rows}
    cfg.pop("smtp_pass", None)  # nie zwracaj hasła
    return jsonify(cfg)


@app.route("/api/email/test", methods=["POST"])
@require_role("admin")
@csrf_protect
def email_test():
    """Wyślij testowy email."""
    db = get_db()
    try:
        rows = db.execute("SELECT key,value FROM settings WHERE category='email'").fetchall()
    finally:
        db.close()
    cfg = {r["key"]: r["value"] for r in rows}
    recipient = str((request.get_json(silent=True) or {}).get("recipient") or cfg.get("smtp_from") or "").strip()[:254]
    if not recipient:
        return jsonify({"error": "Brak adresu odbiorcy"}), 400
    try:
        _send_email(
            to=recipient,
            subject="DocCompare — test powiadomień",
            body="Jeśli widzisz tę wiadomość, konfiguracja SMTP działa poprawnie.",
            cfg=cfg
        )
        return jsonify({"ok": True, "sent_to": recipient})
    except Exception as e:
        logger.exception("Email send error: %s", e)
        return jsonify({"error": "Błąd wysyłki e-mail — sprawdź konfigurację SMTP."}), 400


@app.route("/settings/email")
@require_role("admin")
def page_email_settings():
    """Strona konfiguracji SMTP (nadawca powiadomień i wysyłki do dostawców)."""
    return render_template("email_settings.html",
                           username=session.get("username"), role=session.get("role"))


def _send_email(to: str, subject: str, body: str, cfg: dict = None):
    """Wysyła email przez SMTP."""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    if not cfg:
        db = get_db()
        try:
            rows = db.execute("SELECT key,value FROM settings WHERE category='email'").fetchall()
        finally:
            db.close()
        cfg = {r["key"]: r["value"] for r in rows}

    host = cfg.get("smtp_host", "")
    if not host:
        raise ValueError("Brak konfiguracji SMTP. Skonfiguruj w Ustawieniach → Email.")

    try:
        port = int(cfg.get("smtp_port", 587))
    except (ValueError, TypeError):
        port = 587
    user = cfg.get("smtp_user", "")
    pwd  = cfg.get("smtp_pass", "")
    frm  = cfg.get("smtp_from", user)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = frm
    msg["To"]      = to
    msg.attach(MIMEText(body, "plain", "utf-8"))

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=10) as srv:
            srv.ehlo()
            if user and pwd:
                srv.login(user, pwd)
            srv.sendmail(frm, [to], msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=10) as srv:
            srv.ehlo()
            if port != 25:
                srv.starttls()
            if user and pwd:
                srv.login(user, pwd)
            srv.sendmail(frm, [to], msg.as_string())


@app.route("/api/suppliers/<int:sid>/rules", methods=["GET"])
@login_required
def get_supplier_rules(sid):
    db = get_db()
    try:
        row = db.execute("SELECT custom_rules_json FROM suppliers WHERE id=?", (sid,)).fetchone()
    finally:
        db.close()
    if not row:
        return jsonify({"error": "Nie znaleziono"}), 404
    try:
        rules = json.loads(row["custom_rules_json"] or "[]")
    except (ValueError, TypeError):
        rules = []
    return jsonify({"rules": rules})


@app.route("/api/suppliers/<int:sid>/rules", methods=["POST"])
@require_role("admin")
@csrf_protect
def save_supplier_rules(sid):
    data = request.get_json(silent=True) or {}
    rules = data.get("rules", [])
    if not isinstance(rules, list):
        return jsonify({"error": "rules musi być tablicą"}), 400
    if len(rules) > 500:
        return jsonify({"error": "Za dużo reguł — limit wynosi 500"}), 400
    # Waliduj typy
    valid_types = {"PRICE_ROUNDING","CURRENCY_TOLERANCE","FIELD_IGNORE",
                   "PAYMENT_ALIAS","REF_TRANSFORM","QTY_MULTIPLIER"}
    _rule_str_cap = 500
    sanitized_rules = []
    for r in rules:
        if not isinstance(r, dict) or r.get("type") not in valid_types:
            return jsonify({"error": f"Nieznany typ reguły: {r.get('type') if isinstance(r, dict) else r}"}), 400
        sanitized_rules.append({k: str(v)[:_rule_str_cap] if isinstance(v, str) else v
                                 for k, v in r.items()})
    rules = sanitized_rules
    db = get_db()
    try:
        db.execute("UPDATE suppliers SET custom_rules_json=? WHERE id=?",
                   (json.dumps(rules), sid))
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True, "rules_count": len(rules)})


# ═══════════════════════════════════════════════════════════════
# ZGŁOSZENIA (TICKETS)
# ═══════════════════════════════════════════════════════════════

@app.route("/api-costs")
@require_role("manager")
def api_costs_page():
    """Strona kosztów Claude API — manager widzi swoje, admin widzi wszystkich."""
    role = session["role"]
    uid  = session["user_id"]
    is_admin_role = role in ("admin", "superuser")
    db = get_db()
    totals = {"calls": 0, "inp": 0, "out": 0, "cost": 0.0}
    current_month = {"calls": 0, "cost": 0.0}
    by_model = []; by_type = []; monthly = []; by_user = []
    try:
        wh   = "" if is_admin_role else "WHERE a.user_id=?"
        p    = [] if is_admin_role else [uid]
        # PG nie ma strftime — porównujemy z miesiącem policzonym w Pythonie (param).
        wh_m = (wh + (" AND" if wh else "WHERE") + " substr(a.created_at,1,7)=?")
        _cur_month = _ts_ago(seconds=0)[:7]   # 'YYYY-MM'

        totals = dict(db.execute(
            # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
            f"SELECT COUNT(*) as calls, COALESCE(SUM(input_tokens),0) as inp,"  # nosec B608
            f" COALESCE(SUM(output_tokens),0) as out, COALESCE(SUM(cost_usd),0.0) as cost"
            f" FROM api_usage a {wh}", p).fetchone() or {}) or totals

        current_month = dict(db.execute(
            # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
            f"SELECT COUNT(*) as calls, COALESCE(SUM(cost_usd),0.0) as cost"  # nosec B608
            f" FROM api_usage a {wh_m}", p + [_cur_month]).fetchone() or {}) or current_month

        by_model = [dict(r) for r in db.execute(
            # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
            f"SELECT model, COUNT(*) as calls,"  # nosec B608
            f" COALESCE(SUM(input_tokens),0) as inp,"
            f" COALESCE(SUM(output_tokens),0) as out,"
            f" COALESCE(SUM(cost_usd),0.0) as cost"
            f" FROM api_usage a {wh} GROUP BY model ORDER BY cost DESC", p).fetchall()]

        by_type = [dict(r) for r in db.execute(
            # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
            f"SELECT call_type, COUNT(*) as calls, COALESCE(SUM(cost_usd),0.0) as cost"  # nosec B608
            f" FROM api_usage a {wh} GROUP BY call_type ORDER BY cost DESC", p).fetchall()]

        monthly = [dict(r) for r in db.execute(
            # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
            f"SELECT substr(a.created_at,1,7) as month, COUNT(*) as calls,"  # nosec B608
            f" COALESCE(SUM(cost_usd),0.0) as cost"
            f" FROM api_usage a {wh} GROUP BY month ORDER BY month DESC LIMIT 12", p).fetchall()]

        if is_admin_role:
            by_user = [dict(r) for r in db.execute(
                "SELECT u.username, COUNT(*) as calls,"
                " COALESCE(SUM(a.cost_usd),0.0) as cost"
                " FROM api_usage a JOIN users u ON a.user_id=u.id"
                " GROUP BY a.user_id ORDER BY cost DESC").fetchall()]
            total_cost = totals.get("cost") or 0
            for row in by_user:
                row["pct"] = round(row["cost"] / total_cost * 100, 1) if total_cost else 0
    except Exception as _e:
        logger.warning(f"api_costs_page DB error: {_e}")
    finally:
        db.close()

    avg_cost = round(totals.get("cost", 0) / totals["calls"], 6) if totals.get("calls") else 0
    USD_TO_PLN = 4.0  # przybliżony kurs — aktualizuj ręcznie w razie potrzeby
    return render_template("api_costs.html",
        username=session["username"], role=role,
        totals=totals, current_month=current_month,
        by_model=by_model, by_type=by_type,
        monthly=list(reversed(monthly)),
        by_user=by_user, is_admin=is_admin_role,
        avg_cost=avg_cost, usd_to_pln=USD_TO_PLN)


# ═══════════════════════════════════════════════════════════════
# SZABLONY PORÓWNAŃ
# ═══════════════════════════════════════════════════════════════

def _ensure_json_str(value, default: str = "{}") -> str:
    """Return *value* as a JSON string. If it's already a string, validate it;
    if it's a dict/list, serialize it; otherwise return *default*."""
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value)
        except Exception:
            return default
    if isinstance(value, str):
        try:
            json.loads(value)
            return value
        except Exception:
            return default
    return default

@app.route("/templates")
@require_role("admin")
def templates_page():
    """Strona zarządzania szablonami porównań (tylko admin)."""
    db = get_db()
    try:
        rows = db.execute("SELECT * FROM comparison_templates ORDER BY page_type, name").fetchall()
        rows = [dict(r) for r in rows]
    except Exception as _e:
        logger.warning(f"templates_page DB error: {_e}")
        rows = []
    finally:
        db.close()
    return render_template("templates.html",
        username=session["username"], role=session["role"],
        templates=rows)


@app.route("/api/templates")
@login_required
def api_templates_list():
    """Zwraca listę szablonów publicznych (lub wszystkich dla admina)."""
    page_type = request.args.get("page_type", "")
    uid  = session["user_id"]
    role = session["role"]
    db   = get_db()
    try:
        if is_admin(role):
            rows = db.execute(
                "SELECT * FROM comparison_templates ORDER BY name").fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM comparison_templates WHERE is_public=1 ORDER BY name",
            ).fetchall()
    finally:
        db.close()
    result = [dict(r) for r in rows]
    if page_type:
        result = [r for r in result if r.get("page_type") == page_type]
    return jsonify(result)


@app.route("/api/templates", methods=["POST"])
@require_role("admin")
@csrf_protect
def api_templates_create():
    data = request.get_json(force=True) or {}
    name = str(data.get("name") or "").strip()[:200]
    if not name:
        return jsonify({"error": "Pole 'name' jest wymagane"}), 400
    page_type = data.get("page_type", "compare")
    if page_type not in ("compare", "typo", "table"):
        return jsonify({"error": "Nieprawidłowy page_type — dozwolone: compare, typo, table"}), 400
    try:
        price_tol = float(data.get("price_tolerance_pct") or 0.5)
        qty_tol   = float(data.get("qty_tolerance_pct") or 0.0)
    except (TypeError, ValueError):
        return jsonify({"error": "price_tolerance_pct i qty_tolerance_pct muszą być liczbami"}), 400
    if not (0.0 <= price_tol <= 100.0):
        price_tol = 0.5
    if not (0.0 <= qty_tol <= 100.0):
        qty_tol = 0.0
    db = get_db()
    try:
        cursor = db.execute(
            """INSERT INTO comparison_templates
               (name, description, page_type, is_public, supplier_code, doc_type_a, doc_type_b,
                price_tolerance_pct, qty_tolerance_pct, ai_model, use_ai,
                column_mapping_json, ignore_fields_json, created_by)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (name, str(data.get("description") or "")[:2000], page_type,
             int(bool(data.get("is_public"))),
             str(data.get("supplier_code") or "")[:50],
             str(data.get("doc_type_a") or "")[:50],
             str(data.get("doc_type_b") or "")[:50],
             price_tol, qty_tol,
             str(data.get("ai_model") or "claude-sonnet-4-6")[:100],
             int(bool(data.get("use_ai", True))),
             json.dumps(data.get("column_mapping") or {}),
             json.dumps(data.get("ignore_fields") or []),
             session["user_id"])
        )
        db.commit()
        return jsonify({"id": cursor.lastrowid}), 201
    except Exception as e:
        logger.exception("Unexpected error %s", request.path)
        return jsonify({"error": "Błąd serwera"}), 500
    finally:
        db.close()


@app.route("/api/templates/<int:tid>", methods=["PATCH"])
@require_role("admin")
@csrf_protect
def api_templates_update(tid):
    data = request.get_json(force=True) or {}
    db = get_db()
    try:
        if not db.execute("SELECT id FROM comparison_templates WHERE id=?", (tid,)).fetchone():
            return jsonify({"error": "Nie znaleziono szablonu"}), 404
        ALLOWED = {"name", "description", "page_type", "is_public", "supplier_code",
                   "doc_type_a", "doc_type_b", "price_tolerance_pct", "qty_tolerance_pct",
                   "ai_model", "use_ai", "column_mapping_json", "ignore_fields_json"}
        _tmpl_max_lens = {"name": 200, "description": 2000, "supplier_code": 50,
                          "doc_type_a": 50, "doc_type_b": 50, "ai_model": 100,
                          "column_mapping_json": 50000, "ignore_fields_json": 10000}
        _tmpl_enums = {"page_type": {"compare", "typo", "table"}}
        sets, vals = [], []
        for k, v in data.items():
            if k in ALLOWED:
                if k in _tmpl_enums:
                    if v not in _tmpl_enums[k]:
                        continue
                elif k in ("is_public", "use_ai"):
                    v = 1 if v else 0
                elif k in ("price_tolerance_pct", "qty_tolerance_pct"):
                    try:
                        v = float(v or 0)
                    except (TypeError, ValueError):
                        continue
                    if not (0.0 <= v <= 100.0):
                        continue
                elif k in _tmpl_max_lens:
                    v = str(v or "")[:_tmpl_max_lens[k]]
                sets.append(f"{k}=?")
                vals.append(v)
        if sets:
            sets.append("updated_at=datetime('now')")
            vals.append(tid)
            # Bandit B608: nazwy kolumn z twardej białej listy w kodzie; wartości jako parametry ?.
            db.execute(f"UPDATE comparison_templates SET {','.join(sets)} WHERE id=?", vals)  # nosec B608
            db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/templates/<int:tid>", methods=["DELETE"])
@require_role("admin")
@csrf_protect
def api_templates_delete(tid):
    db = get_db()
    try:
        db.execute("DELETE FROM comparison_templates WHERE id=?", (tid,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/templates/<int:tid>/use", methods=["POST"])
@login_required
@csrf_protect
def api_templates_use(tid):
    db = get_db()
    try:
        db.execute("UPDATE comparison_templates SET use_count=use_count+1 WHERE id=?", (tid,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/templates/export")
@require_role("admin")
def api_templates_export():
    db = get_db()
    try:
        rows = db.execute("SELECT * FROM comparison_templates ORDER BY name").fetchall()
    finally:
        db.close()
    payload = json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2)
    return (payload, 200, {
        "Content-Type": "application/json; charset=utf-8",
        "Content-Disposition": "attachment; filename=comparison_templates.json",
    })


@app.route("/api/templates/import", methods=["POST"])
@require_role("admin")
@csrf_protect
def api_templates_import():
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, list):
        return jsonify({"error": "Oczekiwano tablicy JSON szablonów"}), 400
    db = get_db()
    imported = 0
    try:
        for t in data:
            name      = (t.get("name") or "").strip()
            page_type = t.get("page_type", "compare")
            if not name or page_type not in ("compare", "typo", "table"):
                continue
            try:
                _ptol = float(t.get("price_tolerance_pct") or 0.5)
                _qtol = float(t.get("qty_tolerance_pct") or 0.0)
                if not (0.0 <= _ptol <= 100.0): _ptol = 0.5
                if not (0.0 <= _qtol <= 100.0): _qtol = 0.0
                _imp_name = name[:200]
                _imp_desc = str(t.get("description") or "")[:2000]
                _imp_sc   = str(t.get("supplier_code") or "")[:50]
                _imp_dta  = str(t.get("doc_type_a") or "")[:50]
                _imp_dtb  = str(t.get("doc_type_b") or "")[:50]
                _imp_am   = str(t.get("ai_model") or "claude-sonnet-4-6")[:100]
                _imp_cmj  = _ensure_json_str(t.get("column_mapping_json"), "{}")[:50000]
                _imp_ifj  = _ensure_json_str(t.get("ignore_fields_json"), "[]")[:10000]
                db.execute(
                    """INSERT INTO comparison_templates
                       (name, description, page_type, is_public, supplier_code,
                        doc_type_a, doc_type_b, price_tolerance_pct, qty_tolerance_pct,
                        ai_model, use_ai, column_mapping_json, ignore_fields_json, created_by)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (_imp_name, _imp_desc, page_type,
                     int(bool(t.get("is_public"))), _imp_sc,
                     _imp_dta, _imp_dtb,
                     _ptol, _qtol,
                     _imp_am,
                     int(bool(t.get("use_ai", True))),
                     _imp_cmj, _imp_ifj,
                     session["user_id"])
                )
                imported += 1
            except Exception:
                pass
        db.commit()
        return jsonify({"imported": imported})
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# POMOC / INSTRUKCJA
# ═══════════════════════════════════════════════════════════════

@app.route("/help")
@login_required
def help_page():
    return render_template("help.html",
                           role=session.get("role", "user"),
                           username=session.get("username", ""))


# ═══════════════════════════════════════════════════════════════
# BIBLIOTEKA ARTWORKÓW (dysk sieciowy)
# ═══════════════════════════════════════════════════════════════

def _library_token() -> str:
    """Zwraca (lub generuje) token synchronizacji biblioteki."""
    import secrets as _sec
    db = get_db()
    try:
        row = db.execute(
            "SELECT value FROM settings WHERE category='library' AND key='sync_token'"
        ).fetchone()
        if row:
            return row[0]
        token = _sec.token_hex(32)
        # ON CONFLICT — dwa równoległe pierwsze wywołania nie mogą wstawić dwóch tokenów
        # (UNIQUE(category,key)); po wstawieniu odczytujemy wartość obowiązującą.
        db.execute(
            "INSERT INTO settings(category,key,value) VALUES('library','sync_token',?) "
            "ON CONFLICT(category,key) DO NOTHING",
            (token,)
        )
        db.commit()
        row = db.execute(
            "SELECT value FROM settings WHERE category='library' AND key='sync_token'"
        ).fetchone()
        return row[0] if row else token
    finally:
        db.close()


def _library_auth(req) -> bool:
    """Sprawdza Bearer token w nagłówku Authorization."""
    import hmac as _hmac_lib
    auth = req.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    return _hmac_lib.compare_digest(auth[7:], _library_token())


def _path_levels(rel_path: str) -> list:
    """Rozbija ścieżkę na maks. 5 poziomów folderów."""
    parts = rel_path.replace("\\", "/").split("/")
    # ostatni element to plik, reszta to foldery
    parts = parts[:-1]
    return (parts + ["", "", "", "", ""])[:5]


@app.route("/library/download-agent")
@require_role("manager")
def library_download_agent():
    """Serwuje skrypt library_sync.py do pobrania."""
    from flask import send_file as _sf
    agent_path = os.path.join(os.path.dirname(__file__), "library_sync.py")
    if not os.path.exists(agent_path):
        return "Plik agenta nie znaleziony", 404
    return _sf(agent_path, as_attachment=True, download_name="library_sync.py",
               mimetype="text/x-python")


@app.route("/library")
@require_role("manager")
def library_page():
    """Strona zarządzania biblioteką artworków."""
    db = get_db()
    try:
        total = (db.execute("SELECT COUNT(*) FROM library_files").fetchone() or [0])[0]
        last_sync = (db.execute(
            "SELECT MAX(synced_at) FROM library_files"
        ).fetchone() or [None])[0]
        brands = [r[0] for r in db.execute(
            "SELECT DISTINCT lvl1 FROM library_files WHERE lvl1!='' ORDER BY lvl1"
        ).fetchall()]
    except Exception:
        total = 0; last_sync = None; brands = []
    finally:
        db.close()
    token = _library_token()
    return render_template("library.html",
        username=session["username"], role=session["role"],
        total=total, last_sync=last_sync, brands=brands,
        sync_token=token)


def _warn_naming_convention(filename: str) -> list:
    """Sprawdź nazwę wgrywanego mastera względem konwencji nazewniczej i zaloguj
    odstępstwa. Walidator jest DORADCZY — nie odrzuca pliku, bo większość bazy
    powstała przed konwencją; chodzi o wyłapanie błędu przy wgraniu zamiast
    pół roku później w bibliotece. Zwraca listę problemów (pustą, gdy nazwa ok)."""
    try:
        from artwork_naming import validate_master_filename
        problems = validate_master_filename(filename).get("problems") or []
    except Exception:
        return []
    if problems:
        logger.warning("master %s — nazwa niezgodna z konwencją: %s",
                       filename, "; ".join(p["msg"] for p in problems))
    return problems


def _ensure_master_profile(db, file_path: str, filename: str, rel_path: str,
                           thumb_bytes=None) -> bool:
    """Zarejestruj wgrany plik mastera jako profil mapowania (artwork_profiles), o ile
    jeszcze go nie ma — żeby pojawił się w zakładce „Mapowanie" jako pozycja DO
    ZMAPOWANIA (0 pól). Nie nadpisuje istniejącego profilu (nie kasuje mapowań).
    Zwraca True, gdy utworzono nowy profil."""
    try:
        _ensure_artwork_profile_tables(db)
    except Exception:
        pass
    ex = db.execute(
        "SELECT id FROM artwork_profiles WHERE master_pdf_path=? LIMIT 1", (file_path,)).fetchone()
    if ex:
        return False
    parsed = {}
    try:
        from artwork_naming import parse_master_filename as _pmf
        parsed = _pmf(filename) or {}
    except Exception:
        parsed = {}
    _warn_naming_convention(filename)
    ref_code = str(parsed.get("ref") or "").strip()
    pkg_type = str(parsed.get("packaging_type") or "").strip()
    rev_str  = str(parsed.get("revision") or "").strip()
    try:
        rev_rank = int(parsed.get("revision_rank") or 0)
    except (TypeError, ValueError):
        rev_rank = 0
    ean_val = str(parsed.get("ean") or "").strip()
    name = ref_code or os.path.splitext(filename)[0]
    folder = "/".join(rel_path.replace("\\", "/").split("/")[:-1])
    thumb_b64 = ""
    if thumb_bytes:
        try:
            import base64 as _b64
            thumb_b64 = _b64.b64encode(thumb_bytes).decode()   # surowy base64 (jak w profilach)
        except Exception:
            thumb_b64 = ""
    db.execute(
        "INSERT INTO artwork_profiles "
        "(name, ean, ref_code, master_pdf_path, thumb_b64, folder_path, "
        "packaging_type, revision, revision_rank, source_path, is_active, created_by) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,1,NULL)",
        (name, ean_val, ref_code, file_path, thumb_b64, folder,
         pkg_type, rev_str, rev_rank, rel_path))
    db.commit()
    logger.info("master profile created (do zmapowania) ref=%s plik=%s", ref_code or name, filename)
    return True


@app.route("/api/library/sync", methods=["POST"])
def api_library_sync():
    """
    Endpoint dla agenta synchronizacji (library_sync.py).
    Przyjmuje multipart/form-data z polem 'file' (PDF) i 'meta' (JSON).
    Gdy meta.is_master=true i przesłano pełny plik — rejestruje master jako profil
    mapowania (żeby był widoczny w zakładce „Mapowanie" jako do zmapowania).
    """
    if not _library_auth(request):
        return jsonify({"error": "Brak autoryzacji"}), 401

    meta_raw = request.form.get("meta") or request.get_json(silent=True) or {}
    if isinstance(meta_raw, str):
        try:
            import json as _j; meta_raw = _j.loads(meta_raw)
        except Exception:
            meta_raw = {}

    rel_path   = str(meta_raw.get("rel_path") or "").replace("\\", "/").strip("/")[:1000]
    filename   = str(meta_raw.get("filename") or rel_path.split("/")[-1] or "")[:500]
    checksum   = str(meta_raw.get("checksum", "") or "")[:128]
    modified   = str(meta_raw.get("modified_at", "") or "")[:50]
    z_path_raw = str(meta_raw.get("z_path", "") or "")[:1000]
    try:
        size_bytes = int(meta_raw.get("size_bytes") or 0)
    except (TypeError, ValueError):
        size_bytes = 0

    if not rel_path or not filename:
        return jsonify({"error": "rel_path i filename są wymagane"}), 400

    levels = _path_levels(rel_path)
    # REF + typ opakowania z nazwy — do szybkiego renderu z listy REF-ów.
    from artwork_naming import parse_master_filename
    _p = parse_master_filename(filename)
    ref_val, pkg_val = _p["ref"], _p["packaging_type"]

    # Get thumbnail (new mode: thumb only, no full file stored)
    thumb_data = None
    thumb_file = request.files.get("thumb")
    if thumb_file:
        thumb_data = thumb_file.read()
    elif meta_raw.get("thumb_b64"):
        import base64 as _b64
        try:
            thumb_data = _b64.b64decode(meta_raw["thumb_b64"])
        except Exception:
            thumb_data = None

    z_path = z_path_raw

    # Only save full file if explicitly sent (backward compat)
    lib_folder = os.path.realpath(app.config["LIBRARY_FOLDER"])
    file_path_on_disk = ""
    f = request.files.get("file")
    if f:
        dest_path = os.path.join(lib_folder, rel_path)
        dest_real = os.path.realpath(dest_path)
        if not dest_real.startswith(lib_folder + os.sep):
            return jsonify({"error": "Nieprawidłowa ścieżka pliku"}), 400
        os.makedirs(os.path.dirname(dest_real), exist_ok=True)
        f.save(dest_real)
        dest_path = dest_real
        size_bytes = os.path.getsize(dest_path)
        file_path_on_disk = dest_path

    db = get_db()
    try:
        existing = db.execute(
            "SELECT id, checksum FROM library_files WHERE rel_path=?", (rel_path,)
        ).fetchone()

        if existing:
            if existing["checksum"] == checksum and not file_path_on_disk and not thumb_data:
                return jsonify({"status": "skipped", "id": existing["id"]})
            update_fields = (
                "filename=?, size_bytes=?, modified_at=?, synced_at=datetime('now'), "
                "checksum=?, lvl1=?, lvl2=?, lvl3=?, lvl4=?, lvl5=?, z_path=?, "
                "ref=?, packaging_type=?"
            )
            params = [filename, size_bytes, modified, checksum, *levels, z_path,
                      ref_val, pkg_val]
            if file_path_on_disk:
                update_fields += ", file_path=?"
                params.append(file_path_on_disk)
            if thumb_data is not None:
                update_fields += ", thumb_data=?"
                params.append(thumb_data)
            params.append(rel_path)
            # Bandit B608: nazwy kolumn z twardej białej listy w kodzie; wartości jako parametry ?.
            db.execute(f"UPDATE library_files SET {update_fields} WHERE rel_path=?", params)  # nosec B608
            db.commit()
            if meta_raw.get("is_master") and file_path_on_disk:
                try:
                    _ensure_master_profile(db, file_path_on_disk, filename, rel_path, thumb_data)
                except Exception as _pe:
                    logger.warning("ensure master profile failed rel=%s: %s", rel_path, _pe)
            return jsonify({"status": "updated", "id": existing["id"]})
        else:
            cur = db.execute(
                """INSERT INTO library_files
                   (rel_path, filename, size_bytes, modified_at, checksum,
                    lvl1, lvl2, lvl3, lvl4, lvl5, file_path, thumb_data, z_path,
                    ref, packaging_type)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rel_path, filename, size_bytes, modified, checksum,
                 *levels, file_path_on_disk, thumb_data, z_path, ref_val, pkg_val)
            )
            db.commit()
            if meta_raw.get("is_master") and file_path_on_disk:
                try:
                    _ensure_master_profile(db, file_path_on_disk, filename, rel_path, thumb_data)
                except Exception as _pe:
                    logger.warning("ensure master profile failed rel=%s: %s", rel_path, _pe)
            return jsonify({"status": "created", "id": cur.lastrowid})
    except Exception as e:
        logger.error(f"library sync error: {e}")
        logger.exception("Unexpected error %s", request.path)
        return jsonify({"error": "Błąd serwera"}), 500
    finally:
        db.close()


@app.route("/api/library/needed-masters", methods=["GET"])
def api_library_needed_masters():
    """Lista POTWIERDZONYCH masterów, które NIE mają jeszcze pliku PDF na serwerze.

    Agent `library_sync.py --upload-masters` pobiera tę listę i wysyła TYLKO te
    pełne pliki — dzięki temu auto-podpinanie masterów do dostaw działa, bez
    przesyłania całej biblioteki (potwierdzone = potrzebne i aktualne). Auth
    tokenem synchronizacji (jak /api/library/sync)."""
    if not _library_auth(request):
        return jsonify({"error": "Brak autoryzacji"}), 401
    import artwork_index as _ai
    out = []
    db = get_db()
    try:
        try:
            _ai.ensure_confirmed_table(db)
        except Exception:
            pass
        rows = db.execute(
            "SELECT ref_code, rel_path, filename FROM artwork_confirmed "
            "WHERE rel_path<>'' ORDER BY ref_code").fetchall()
        # Jedno zapytanie zamiast SELECT-a per wiersz (N+1): przy kilku tysiącach
        # potwierdzonych masterów to była różnica między jednym round-tripem a tysiącami.
        wanted = [(r["rel_path"] or "").strip() for r in rows]
        wanted = [rel for rel in wanted if rel]
        lf_by_rel = {}
        for chunk_start in range(0, len(wanted), 500):     # limit parametrów zapytania
            chunk = wanted[chunk_start:chunk_start + 500]
            ph = ",".join("?" * len(chunk))
            for lf in db.execute(
                    # Bandit B608: interpolowane są tylko placeholdery ? (liczba = długość listy); wartości jako parametry.
                    f"SELECT rel_path, file_path, z_path FROM library_files "  # nosec B608
                    f"WHERE rel_path IN ({ph})", chunk).fetchall():
                lf_by_rel[lf["rel_path"]] = lf

        for r in rows:
            rel = (r["rel_path"] or "").strip()
            if not rel:
                continue
            lf = lf_by_rel.get(rel)
            # Już ma plik PDF na serwerze (i istnieje na dysku) → pomiń.
            if lf and (lf["file_path"] or "").strip() and os.path.exists(lf["file_path"]):
                continue
            out.append({
                "rel_path": rel,
                "filename": (r["filename"] or rel.split("/")[-1]),
                "z_path": ((lf["z_path"] if lf else "") or ""),
            })
    finally:
        db.close()
    return jsonify({"files": out, "count": len(out)})


@app.route("/api/library/browse")
@login_required
def api_library_browse():
    """Przeglądanie biblioteki po ścieżce."""
    path = request.args.get("path", "").strip("/")
    # Guard against path traversal and invalid characters
    if ".." in path or any(c in path for c in ("\x00", "\r", "\n")):
        return jsonify({"error": "Nieprawidłowa ścieżka"}), 400
    _MAX_LIBRARY_DEPTH = 5
    items: list = []
    total_files: int | None = None
    _page: int | None = None
    _per: int | None = None
    db = get_db()
    try:
        if not path:
            # Poziom 0 — lista marek (lvl1)
            rows = db.execute(
                "SELECT lvl1, COUNT(*) as cnt FROM library_files "
                "WHERE lvl1!='' GROUP BY lvl1 ORDER BY lvl1"
            ).fetchall()
            items = [{"name": r["lvl1"], "type": "folder",
                      "path": r["lvl1"], "count": r["cnt"]} for r in rows]
        else:
            parts = path.split("/")
            depth = min(len(parts), _MAX_LIBRARY_DEPTH - 1)
            parts = parts[:depth]
            col = f"lvl{depth + 1}"
            # Warunek WHERE dla wszystkich dotychczasowych poziomów
            wheres = [f"lvl{i+1}=?" for i in range(depth)]
            where_sql = " AND ".join(wheres) if wheres else "1=1"

            # Sprawdź czy są podfoldery
            try:
                sub = db.execute(
                    # Bandit B608: kolumna lvlN liczona z int (głębokość), WHERE ze stałych lvlN=?; wartości jako parametry.
                    f"SELECT {col}, COUNT(*) as cnt FROM library_files "  # nosec B608
                    f"WHERE {where_sql} AND {col}!='' "
                    f"GROUP BY {col} ORDER BY {col}",
                    parts
                ).fetchall()
            except Exception:
                sub = []

            if sub:
                items = [{"name": r[col], "type": "folder",
                          "path": path + "/" + r[col], "count": r["cnt"]}
                         for r in sub]
            else:
                # Liście — pliki z paginacją; where_sql uses bare column names (lvl1=? …)
                # so we prefix them with lf. for the JOIN query
                lf_where = re.sub(r'\blvl(\d)\b', r'lf.lvl\1', where_sql)
                try:
                    _page = max(1, min(10000, int(request.args.get("page", 1))))
                    _per  = max(1, min(int(request.args.get("per_page", 100)), 500))
                except (ValueError, TypeError):
                    _page, _per = 1, 100
                _offset = (_page - 1) * _per

                total_files = (db.execute(
                    # Bandit B608: kolumna lvlN liczona z int (głębokość), WHERE ze stałych lvlN=?; wartości jako parametry.
                    f"SELECT COUNT(*) FROM library_files lf WHERE {lf_where}",  # nosec B608
                    parts
                ).fetchone() or [0])[0]

                files = db.execute(
                    # Bandit B608: kolumna lvlN liczona z int (głębokość), WHERE ze stałych lvlN=?; wartości jako parametry.
                    f"""SELECT lf.id, lf.filename, lf.size_bytes, lf.modified_at,
                               COUNT(apf.id) AS field_count
                        FROM library_files lf
                        LEFT JOIN artwork_profiles ap
                               ON ap.master_pdf_path = lf.file_path
                              AND lf.file_path != '' AND lf.file_path IS NOT NULL
                        LEFT JOIN artwork_profile_fields apf ON apf.profile_id = ap.id
                        WHERE {lf_where}
                        GROUP BY lf.id
                        ORDER BY lf.filename
                        LIMIT ? OFFSET ?""",  # nosec B608
                    parts + [_per, _offset]
                ).fetchall()
                items = [{"id": r["id"], "name": r["filename"],
                          "type": "file", "size": r["size_bytes"],
                          "modified": r["modified_at"],
                          "path": path + "/" + r["filename"],
                          "field_count": r["field_count"] or 0}
                         for r in files]
    except Exception as e:
        logger.warning(f"library browse error: {e}")
        items = []
        total_files = 0
        _page = _per = 1
    finally:
        db.close()
    resp: dict = {"path": path, "items": items}
    # Include pagination metadata when browsing files (leaf level)
    if isinstance(total_files, int):
        resp["total"] = total_files
        resp["page"] = _page
        resp["per_page"] = _per
        resp["total_pages"] = max(1, (total_files + _per - 1) // _per)
    return jsonify(resp)


@app.route("/api/library/search")
def api_library_search():
    """Wyszukiwanie plików w bibliotece."""
    if "user_id" not in session and not _library_auth(request):
        return jsonify({"error": "Brak autoryzacji"}), 401
    q = request.args.get("q", "").strip()
    try:
        limit = max(1, min(int(request.args.get("limit", 30)), 100))
    except (ValueError, TypeError):
        limit = 30
    if not q or len(q) < 2:
        return jsonify({"results": []})
    db = get_db()
    try:
        q_esc = escape_like(q)
        like = f"%{q_esc}%"
        rows = db.execute(
            """SELECT lf.id, lf.filename, lf.rel_path, lf.size_bytes, lf.modified_at,
                      lf.lvl1, lf.lvl2, lf.lvl3, lf.lvl4, lf.lvl5,
                      COUNT(apf.id) AS field_count
               FROM library_files lf
               LEFT JOIN artwork_profiles ap
                      ON ap.master_pdf_path = lf.file_path
                     AND lf.file_path != '' AND lf.file_path IS NOT NULL
               LEFT JOIN artwork_profile_fields apf ON apf.profile_id = ap.id
               WHERE lf.filename LIKE ? ESCAPE '\\' OR lf.rel_path LIKE ? ESCAPE '\\'
               GROUP BY lf.id
               ORDER BY
                 CASE WHEN lf.filename LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END,
                 lf.lvl1, lf.lvl2, lf.lvl3, lf.lvl4, lf.filename
               LIMIT ?""",
            (like, like, like, limit)
        ).fetchall()
        results = []
        for r in rows:
            results.append({
                "id": r["id"],
                "filename": r["filename"],
                "rel_path": r["rel_path"],
                "size": r["size_bytes"],
                "modified": r["modified_at"],
                "field_count": r["field_count"] or 0,
                "breadcrumb": " / ".join(
                    p for p in [r["lvl1"], r["lvl2"], r["lvl3"], r["lvl4"], r["lvl5"]] if p
                ),
            })
    except Exception as e:
        logger.warning(f"library search error: {e}")
        results = []
    finally:
        db.close()
    return jsonify({"results": results, "query": q})


@app.route("/api/library/file/<int:fid>")
@login_required
def api_library_file(fid):
    """Serwuje plik PDF z biblioteki (z dysku)."""
    from flask import send_file as _send_file
    db = get_db()
    try:
        row = db.execute(
            "SELECT filename, file_path FROM library_files WHERE id=?", (fid,)
        ).fetchone()
    finally:
        db.close()
    if not row or not row["file_path"] or not os.path.isfile(row["file_path"]):
        return jsonify({"error": "Plik niedostępny — brak na dysku serwera"}), 404
    real = os.path.realpath(row["file_path"])
    lib_folder = os.path.realpath(app.config.get("LIBRARY_FOLDER", "library"))
    if not real.startswith(lib_folder + os.sep):
        from flask import abort as _abort
        _abort(403)
    return _send_file(
        real,
        mimetype="application/pdf",
        as_attachment=False,
        download_name=row["filename"],
    )


@app.route("/api/library/file/<int:fid>/thumb")
@login_required
def api_library_file_thumb(fid):
    """Serwuje miniaturę (JPEG) pliku z biblioteki."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT thumb_data, filename FROM library_files WHERE id=?", (fid,)
        ).fetchone()
        if not row or not row["thumb_data"]:
            svg = '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="220"><rect width="160" height="220" fill="#f3f4f6"/><text x="80" y="120" text-anchor="middle" font-size="40">&#128196;</text></svg>'
            return Response(svg, mimetype="image/svg+xml")
        return Response(
            row["thumb_data"],
            mimetype="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )
    finally:
        db.close()


def _glb_cache_dir() -> str:
    """Katalog na wyrenderowane GLB. DATA_DIR/glb_cache w prod, inaczej uploads/glb_cache."""
    base = os.environ.get("DATA_DIR", "")
    root = base if base and os.path.isdir(base) else app.config["UPLOAD_FOLDER"]
    d = os.path.join(root, "glb_cache")
    os.makedirs(d, exist_ok=True)
    return d


def _library_pdf_source(row) -> str:
    """Zwraca czytelną ścieżkę PDF pliku biblioteki: dysk serwera → dysk sieciowy Z:\\."""
    fp = row["file_path"] or ""
    if fp and os.path.isfile(fp):
        return fp
    zp = row["z_path"] or ""
    if zp and os.path.isfile(zp):
        return zp
    return ""


# Statusy renderu pliku biblioteki — jedno źródło prawdy, żeby helper i worker grupy
# nie rozjechały się na literówce w gołym stringu.
_RS_CACHED, _RS_RENDERED, _RS_MANUAL, _RS_MISSING, _RS_ERROR = (
    "cached", "rendered", "needs_manual_dims", "missing", "error")
_RS_ALL = (_RS_CACHED, _RS_RENDERED, _RS_MANUAL, _RS_MISSING, _RS_ERROR)


def _render_library_file_to_cache(fid: int, manual=None) -> dict:
    """Rdzeń leniwego renderu: hit cache (fid+checksum) → 'cached'; miss → czyta PDF
    (Z:\\ lub dysk), generuje GLB przez artwork_palviz, cache'uje. Współdzielone przez
    endpoint pojedynczego pliku i worker grupy. Zwraca dict ze 'status':
    cached | rendered | needs_manual_dims | missing | error (+ szczegóły)."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT id, filename, file_path, z_path, checksum FROM library_files WHERE id=?",
            (fid,)).fetchone()
        if not row:
            return {"status": _RS_MISSING, "error": "Plik nie istnieje w bibliotece"}
        checksum = row["checksum"] or ""
        filename = row["filename"] or ""
        z_path = row["z_path"] or ""
        from artwork_naming import format_master_label
        label = format_master_label(filename)
        cached = db.execute(
            "SELECT glb_path, checksum FROM artwork_glb_cache WHERE fid=?", (fid,)).fetchone()
        if cached and cached["checksum"] == checksum and os.path.isfile(cached["glb_path"]):
            # REG-04/Pitfall 1: plik serwowany z cache musi NADAL bumpować
            # last_confirmed_at w rejestrze, nie tylko świeże rendery.
            import artwork_render_registry as _arr
            _arr.register_render(
                db, fid=fid, variant=RenderVariant.DIELINE.value, source_hash=checksum,
                glb_path=cached["glb_path"],
                created_by=session.get("user_id") if has_request_context() else None)
            return {"status": _RS_CACHED, "label": label}
        src = _library_pdf_source(row)
        if not src:
            # Rozróżnij „nie ma pliku" od „jest na Z:\, ale dysk niezamapowany na serwerze".
            if z_path and (z_path[:2].upper() == "Z:" or z_path.startswith("\\\\")):
                return {"status": _RS_MISSING,
                        "error": "Brak dostępu do dysku Z:\\ na serwerze — biblioteka "
                                 "wymaga zamapowanego Z:\\ do renderu 3D."}
            return {"status": _RS_MISSING, "error": "PDF niedostępny (brak na dysku i na Z:\\)"}
    finally:
        db.close()

    # Ten sam generator co upload (/artwork/3d) — ma heurystyki orientacji
    # (pick_front_by_color: kolorowy panel → front; enforce_plain_top_bottom: pusta
    # ściana → góra/dół), więc model z biblioteki jest zorientowany tak dobrze jak
    # z uploadu, a nie płaski panel tekstowy na wierzchu (jak w gołym palviz).
    from artwork_3d import generate_artwork_3d  # lazy — ciężkie zależności 3D
    out_path = os.path.join(_glb_cache_dir(), f"{fid}.glb")
    try:
        res = generate_artwork_3d(src, out_path, dims_override=manual)
    except ValueError:
        # generate_artwork_3d rzuca ValueError, gdy nie ma wymiarów w PDF i nie podano
        # dims_override → poproś UI o W×H×D (chyba że user już je podał = realny błąd).
        if manual:
            return {"status": _RS_ERROR, "error": "Błąd generowania modelu 3D"}
        return {"status": _RS_MANUAL,
                "warnings": ["Brak wymiarów w PDF — podaj W×H×D ręcznie."]}
    except Exception as e:
        logger.warning("library 3d render fid=%s: %s", fid, e)
        return {"status": _RS_ERROR, "error": "Błąd generowania modelu 3D"}

    # Reużyj nauczonej orientacji (akceptacje z /artwork/3d): wypal ją w cache'owanym
    # GLB, żeby model z biblioteki stał tak, jak człowiek już kiedyś zatwierdził dla
    # tego REF-u/typu. Best-effort, zero-kosztowo gdy brak dopasowania.
    try:
        from artwork_naming import parse_master_filename
        from artwork_3d import bake_orientation
        p = parse_master_filename(filename)
        rpy = _a3d_learned({"ref": p["ref"], "packaging_type": p["packaging_type"]})
        if any(rpy):
            bake_orientation(out_path, *rpy)
    except Exception as e:
        logger.warning("library 3d orient reapply fid=%s: %s", fid, e)

    dm = res.get("dims_mm") or [0, 0, 0]          # generate_artwork_3d zwraca listę [w,h,d]
    dims = {"w": dm[0], "h": dm[1], "d": dm[2]}
    db = get_db()
    try:
        db.execute(
            "INSERT INTO artwork_glb_cache (fid, checksum, glb_path, w_mm, h_mm, d_mm) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(fid) DO UPDATE SET checksum=excluded.checksum, "
            "glb_path=excluded.glb_path, w_mm=excluded.w_mm, h_mm=excluded.h_mm, "
            "d_mm=excluded.d_mm, created_at=datetime('now')",
            (fid, checksum, out_path, dims.get("w", 0), dims.get("h", 0), dims.get("d", 0)))
        db.commit()
        # REG-04: drugi (i ostatni) branch choke-pointu — świeżo wyrenderowany
        # plik. library_files.checksum to już md5 bajtów PDF (D-01), zero
        # dodatkowego I/O.
        import artwork_render_registry as _arr
        _arr.register_render(
            db, fid=fid, variant=RenderVariant.DIELINE.value, source_hash=checksum,
            glb_path=out_path,
            created_by=session.get("user_id") if has_request_context() else None)
    finally:
        db.close()
    return {"status": _RS_RENDERED, "dims_mm": dims, "label": label}


def _current_revision_fid(db, fid: int) -> int:
    """Mapuje kliknięty plik na AKTUALNĄ rewizję jego (REF, opakowanie) w tym samym
    folderze biblioteki (spec: „renderujemy tylko aktualne rewizje"). Gdy nie ma jak
    rozstrzygnąć — zwraca oryginalny fid."""
    from artwork_naming import parse_master_filename, pick_current_revisions
    row = db.execute("SELECT rel_path, filename FROM library_files WHERE id=?", (fid,)).fetchone()
    if not row:
        return fid
    clicked = parse_master_filename(row["filename"])
    folder = (row["rel_path"] or "").rsplit("/", 1)[0]
    if not folder or not clicked["ref"]:
        return fid
    # Tylko bezpośredni folder (bez podfolderów): rel_path = folder/plik, nie folder/sub/plik.
    sib = db.execute(
        "SELECT id, filename, modified_at FROM library_files "
        "WHERE rel_path LIKE ? ESCAPE '\\' AND rel_path NOT LIKE ? ESCAPE '\\' "
        "AND lower(filename) LIKE '%.pdf'",
        (escape_like(folder) + "/%", escape_like(folder) + "/%/%")).fetchall()
    key = (clicked["ref"].lower(), clicked["packaging_type"].lower())
    for w in pick_current_revisions([dict(r) for r in sib]):
        if (w["ref"].lower(), w["packaging_type"].lower()) == key:
            return w["id"]
    return fid


@app.route("/api/library/file/<int:fid>/3d")
@login_required
def api_library_file_3d(fid):
    """Leniwy render 3D pliku biblioteki. Gdy brak wymiarów w PDF → {needs_manual_dims};
    ponów z ?w=&h=&d= (mm)."""
    manual = None
    try:
        w = float(request.args.get("w", "") or 0)
        h = float(request.args.get("h", "") or 0)
        d = float(request.args.get("d", "") or 0)
        if w and h and d:
            manual = (w, h, d)
    except (TypeError, ValueError):
        manual = None

    # Spec: renderujemy AKTUALNĄ rewizję — klik w starą podmienia na najnowszą w grupie.
    db = get_db()
    try:
        target_fid = _current_revision_fid(db, fid)
    finally:
        db.close()

    res = _render_library_file_to_cache(target_fid, manual=manual)
    st = res["status"]
    if st == _RS_MISSING:
        return jsonify({"error": res["error"]}), 404
    if st == _RS_ERROR:
        return jsonify({"error": res["error"]}), 500
    if st == _RS_MANUAL:
        return jsonify({"ok": False, "needs_manual_dims": True,
                        "warnings": res.get("warnings", [])})
    return jsonify({"ok": True, "cached": st == _RS_CACHED,
                    "glb_url": url_for("api_library_file_glb", fid=target_fid),
                    "label": res.get("label", ""),
                    "fid": target_fid,
                    "current_revision": target_fid != fid,
                    "dims_mm": res.get("dims_mm", {})})


@app.route("/api/library/file/<int:fid>/glb")
@login_required
def api_library_file_glb(fid):
    """Serwuje zcache'owany GLB pliku biblioteki (z dysku)."""
    from flask import send_file as _send_file
    db = get_db()
    try:
        row = db.execute(
            "SELECT c.glb_path, lf.filename FROM artwork_glb_cache c "
            "LEFT JOIN library_files lf ON lf.id=c.fid WHERE c.fid=?", (fid,)).fetchone()
        # Osierocony cache: plik zniknął z biblioteki (resync) → sprzątnij wiersz + GLB.
        if row and row["filename"] is None:
            try:
                if row["glb_path"] and os.path.isfile(row["glb_path"]):
                    os.remove(row["glb_path"])
            except OSError:
                pass
            db.execute("DELETE FROM artwork_glb_cache WHERE fid=?", (fid,))
            db.commit()
            row = None
    finally:
        db.close()
    if not row or not row["glb_path"] or not os.path.isfile(row["glb_path"]):
        return jsonify({"error": "Model 3D nie jest jeszcze wygenerowany"}), 404
    real = os.path.realpath(row["glb_path"])
    cache_root = os.path.realpath(_glb_cache_dir())
    if not real.startswith(cache_root + os.sep):
        from flask import abort as _abort
        _abort(403)
    from artwork_naming import format_master_label
    dl = secure_filename(format_master_label(row["filename"] or "").replace(" – ", "_")) \
        or f"library_{fid}"
    return _send_file(real, mimetype="model/gltf-binary", as_attachment=False,
                      download_name=f"{dl}.glb")


@app.route("/api/library/file/<int:fid>/3d/accept", methods=["POST"])
@login_required
@csrf_protect
def api_library_file_3d_accept(fid):
    """Zatwierdź układ modelu z biblioteki: wypal orientację w cache'owanym GLB i zapisz
    regułę uczenia keyed po (REF, opakowanie) z nazwy pliku — kolejne rendery tego
    REF-u/typu (biblioteka i upload) ją reużyją."""
    data = request.get_json(silent=True) or {}
    try:
        roll, pitch, yaw = (int(data.get(k, 0)) % 360 for k in ("roll", "pitch", "yaw"))
    except (TypeError, ValueError):
        return jsonify({"error": "Nieprawidłowy kąt orientacji."}), 400
    db = get_db()
    try:
        row = db.execute("SELECT filename FROM library_files WHERE id=?", (fid,)).fetchone()
        cache = db.execute("SELECT glb_path FROM artwork_glb_cache WHERE fid=?", (fid,)).fetchone()
    finally:
        db.close()
    if not row or not cache or not cache["glb_path"] or not os.path.isfile(cache["glb_path"]):
        return jsonify({"error": "Model nie istnieje — wygeneruj go ponownie."}), 404

    from artwork_naming import parse_master_filename
    p = parse_master_filename(row["filename"] or "")
    try:
        from artwork_3d import bake_orientation
        bake_orientation(cache["glb_path"], roll, pitch, yaw)
    except Exception as e:
        logger.exception("library 3d accept bake failed fid=%s", fid)
        return jsonify({"error": f"Nie udało się zapisać układu: {str(e)[:200]}"}), 500

    key = f"lib_{fid}.glb"          # stabilny klucz reguły dla pliku biblioteki
    db = get_db()
    try:
        db.execute("DELETE FROM artwork_3d_orientation WHERE glb_name=?", (key,))
        db.execute(
            "INSERT INTO artwork_3d_orientation "
            "(glb_name, ref, packaging_type, roll, pitch, yaw, accepted_by) "
            "VALUES (?,?,?,?,?,?,?)",
            (key, p["ref"], p["packaging_type"], roll, pitch, yaw, session.get("username")))
        db.commit()
    finally:
        db.close()
    _log_audit("library_3d_accept", str(fid),
               f"orientacja {roll}/{pitch}/{yaw}, ref={p['ref']}, opak={p['packaging_type']}")
    return jsonify({"accepted": True})


def _resolve_render_group_fids(db, members: dict) -> list:
    """Rozwija członków grupy (foldery-prefiksy + jawne file_ids) na listę id plików
    biblioteki, po jednym na (REF, opakowanie) — AKTUALNA rewizja (pick_current_revisions)."""
    from artwork_naming import pick_current_revisions
    folders = members.get("folders") or []
    file_ids = members.get("file_ids") or []
    seen, rows = set(), []
    for pref in folders:
        pref = str(pref).strip().strip("/")
        if not pref:
            continue
        like = escape_like(pref) + "/%"
        for r in db.execute(
                "SELECT id, filename, modified_at FROM library_files "
                "WHERE rel_path LIKE ? ESCAPE '\\' AND lower(filename) LIKE '%.pdf'",
                (like,)).fetchall():
            if r["id"] not in seen:
                seen.add(r["id"]); rows.append(dict(r))
    if file_ids:
        qs = ",".join("?" for _ in file_ids)
        for r in db.execute(
                # Bandit B608: interpolowane są tylko placeholdery ? (liczba = długość listy); wartości jako parametry.
                f"SELECT id, filename, modified_at FROM library_files WHERE id IN ({qs})",  # nosec B608
                [int(x) for x in file_ids]).fetchall():
            if r["id"] not in seen:
                seen.add(r["id"]); rows.append(dict(r))
    return [r["id"] for r in pick_current_revisions(rows)]


def _render_group_pdf_count(db, members: dict) -> int:
    """Szybki górny licznik PDF-ów grupy (bez parsowania nazw/dedupu rewizji) — do
    listy grup. Właściwy zestaw do renderu (aktualne rewizje) liczy _resolve_render_group_fids."""
    ids = set()
    for pref in (members.get("folders") or []):
        pref = str(pref).strip().strip("/")
        if not pref:
            continue
        like = escape_like(pref) + "/%"
        for r in db.execute(
                "SELECT id FROM library_files WHERE rel_path LIKE ? ESCAPE '\\' "
                "AND lower(filename) LIKE '%.pdf'", (like,)).fetchall():
            ids.add(r["id"])
    ids.update(int(x) for x in (members.get("file_ids") or []))
    return len(ids)


def _norm_ref(s: str) -> str:
    """Normalizacja REF do porównań: bez spacji, wielkie litery (toleruje różnice
    w zapisie między listą z MARA/Excela a nazwą pliku)."""
    return re.sub(r"\s+", "", str(s or "").strip().upper())


def _resolve_ref_list_fids(db, refs):
    """Mapuje listę REF-ów na id AKTUALNYCH rewizji masterów w library_files (wszystkie
    warianty opakowania danego REF). Zwraca (fids, unmatched_refs). Wymaga kolumny ref
    (sync/backfill)."""
    from artwork_naming import pick_current_revisions
    wanted = {}
    for r in refs:
        k = _norm_ref(r)
        if k:
            wanted.setdefault(k, str(r).strip())
    if not wanted:
        return [], []
    qs = ",".join("?" for _ in wanted)
    rows = db.execute(
        # Bandit B608: interpolowane są tylko placeholdery ? (liczba = długość listy); wartości jako parametry.
        f"SELECT id, filename, modified_at, ref FROM library_files "  # nosec B608
        f"WHERE UPPER(REPLACE(ref,' ','')) IN ({qs}) AND lower(filename) LIKE '%.pdf'",
        list(wanted.keys())).fetchall()
    fids = [r["id"] for r in pick_current_revisions([dict(x) for x in rows])]
    matched = {_norm_ref(r["ref"]) for r in rows}
    unmatched = [orig for k, orig in wanted.items() if k not in matched]
    return fids, unmatched


def _render_group_worker(jid, fids):
    """Wątek: renderuje każdy plik grupy do cache GLB, aktualizując pasek postępu."""
    total = len(fids)
    counts = {s: 0 for s in _RS_ALL}
    try:
        for i, fid in enumerate(fids, 1):
            res = _render_library_file_to_cache(fid)
            counts[res["status"]] = counts.get(res["status"], 0) + 1
            done = counts[_RS_RENDERED] + counts[_RS_CACHED]
            _async_job_set(jid, "running", step=i, pct=int(i * 100 / max(1, total)),
                           step_label=f"Renderuję {i}/{total} (gotowe: {done})")
        done = counts[_RS_RENDERED] + counts[_RS_CACHED]
        skipped_bits = []
        if counts[_RS_MANUAL]:
            skipped_bits.append(f"bez wymiarów: {counts[_RS_MANUAL]}")
        if counts[_RS_MISSING]:
            skipped_bits.append(f"brak pliku: {counts[_RS_MISSING]}")
        if counts[_RS_ERROR]:
            skipped_bits.append(f"błąd: {counts[_RS_ERROR]}")
        tail = f" · pominięto {total - done} ({', '.join(skipped_bits)})" if skipped_bits else ""
        _async_job_set(jid, "done", result={"total": total, **counts},
                       step_label=f"Gotowe: {done}/{total}{tail}")
    except Exception as e:
        logger.exception("render group worker failed jid=%s", jid)
        _async_job_set(jid, "error", error=str(e))


@app.route("/api/render-groups", methods=["GET"])
@login_required
def api_render_groups_list():
    """Lista grup renderowania z liczbą aktualnych rewizji do renderu."""
    db = get_db()
    try:
        groups = db.execute(
            "SELECT id, name, members_json, created_at FROM render_group ORDER BY name"
        ).fetchall()
        out = []
        for g in groups:
            try:
                members = json.loads(g["members_json"] or "{}")
            except (ValueError, TypeError):
                members = {}
            out.append({"id": g["id"], "name": g["name"],
                        "count": _render_group_pdf_count(db, members),
                        "folders": members.get("folders") or []})
    finally:
        db.close()
    return jsonify({"groups": out})


@app.route("/api/render-groups", methods=["POST"])
@login_required
@csrf_protect
def api_render_groups_create():
    """Tworzy nazwaną grupę: {name, folders:[rel_path-prefix], file_ids:[]}."""
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()[:120]
    folders = [str(x).strip().strip("/") for x in (data.get("folders") or []) if str(x).strip()]
    file_ids = [int(x) for x in (data.get("file_ids") or []) if str(x).isdigit()]
    if not name:
        return jsonify({"error": "Podaj nazwę grupy"}), 400
    if not folders and not file_ids:
        return jsonify({"error": "Grupa musi mieć co najmniej jeden folder lub plik"}), 400
    members = json.dumps({"folders": folders, "file_ids": file_ids}, ensure_ascii=False)
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO render_group (name, members_json, created_by) VALUES (?,?,?)",
            (name, members, session.get("user_id")))
        db.commit()
        gid = cur.lastrowid
    finally:
        db.close()
    _log_audit("render_group_create", name, f"gid={gid}, folderów={len(folders)}")
    return jsonify({"ok": True, "id": gid})


@app.route("/api/render-groups/<int:gid>", methods=["DELETE"])
@login_required
@csrf_protect
def api_render_groups_delete(gid):
    db = get_db()
    try:
        db.execute("DELETE FROM render_group WHERE id=?", (gid,))
        db.commit()
    finally:
        db.close()
    _log_audit("render_group_delete", str(gid), "")
    return jsonify({"ok": True})


@app.route("/api/render-groups/<int:gid>/render", methods=["POST"])
@login_required
@csrf_protect
def api_render_groups_render(gid):
    """Startuje render całej grupy w tle. Zwraca job_id do pollowania
    przez /api/artwork/3d/status/<jid>."""
    db = get_db()
    try:
        g = db.execute("SELECT members_json FROM render_group WHERE id=?", (gid,)).fetchone()
        if not g:
            return jsonify({"error": "Grupa nie istnieje"}), 404
        try:
            members = json.loads(g["members_json"] or "{}")
        except (ValueError, TypeError):
            members = {}
        fids = _resolve_render_group_fids(db, members)
    finally:
        db.close()
    if not fids:
        return jsonify({"error": "Brak plików do renderu w tej grupie"}), 400
    jid = _async_job_create(session["user_id"])
    threading.Thread(target=_render_group_worker, args=(jid, fids), daemon=True).start()
    return jsonify({"job_id": jid, "count": len(fids)})


@app.route("/api/render-groups/<int:gid>/push", methods=["POST"])
@login_required
@require_role("manager")
@csrf_protect
def api_render_groups_push(gid):
    """BATCH-01: push wszystkich renderów grupy do PalViz na raz (najnowszy per
    fid+wariant). Manager+. Daemon-thread; bez env (PALVIZ_PUSH_URL) → 'disabled'."""
    import palviz_push as _pp
    if not _pp.is_enabled():
        return jsonify({"status": "disabled",
                        "message": "Push do PalViz nie jest skonfigurowany (PALVIZ_PUSH_URL)."}), 200
    db = get_db()
    try:
        g = db.execute("SELECT members_json FROM render_group WHERE id=?", (gid,)).fetchone()
        if not g:
            return jsonify({"error": "Grupa nie istnieje"}), 404
        try:
            members = json.loads(g["members_json"] or "{}")
        except (ValueError, TypeError):
            members = {}
        fids = _resolve_render_group_fids(db, members)
    finally:
        db.close()
    if not fids:
        return jsonify({"error": "Brak plików w tej grupie"}), 400

    user = session.get("username")

    def _worker():
        wdb = None
        try:
            wdb = get_db()
            summary = _pp.push_group_fids(wdb, fids, user=user)
            _log_audit("palviz_push_batch", user, f"gid={gid} {summary}")
        except Exception:
            logger.exception("palviz batch push failed for group %s", gid)
        finally:
            if wdb is not None:
                try:
                    wdb.close()
                except Exception:
                    pass

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"status": "queued", "group_fids": len(fids)})


@app.route("/api/artwork/3d/render-refs", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_3d_render_refs():
    """Batch render 3D z wklejonej listy REF-ów (np. eksport z MARA). Mapuje każdy REF
    na aktualną rewizję mastera w bibliotece → render w tle. Zwraca job_id + nierozpoznane
    REF-y (bez mastera w bibliotece). Poll przez /api/artwork/3d/status/<jid>."""
    data = request.get_json(silent=True) or {}
    raw = data.get("refs")
    if isinstance(raw, str):
        refs = re.split(r"[\n,;]+", raw)
    else:
        refs = list(raw or [])
    refs = [r.strip() for r in refs if r and str(r).strip()]
    if not refs:
        return jsonify({"error": "Podaj listę REF-ów"}), 400
    db = get_db()
    try:
        fids, unmatched = _resolve_ref_list_fids(db, refs)
    finally:
        db.close()
    if not fids:
        return jsonify({"error": "Żaden z podanych REF-ów nie ma mastera w bibliotece "
                                 "(zsynchronizuj bibliotekę / sprawdź pisownię).",
                        "unmatched": unmatched}), 400
    jid = _async_job_create(session["user_id"])
    threading.Thread(target=_render_group_worker, args=(jid, fids), daemon=True).start()
    _log_audit("render_refs", f"{len(fids)} plików",
               f"REF-ów: {len(refs)}, nierozpoznanych: {len(unmatched)}")
    return jsonify({"job_id": jid, "count": len(fids),
                    "refs_in": len(refs), "unmatched": unmatched})


@app.route("/api/library/file/<int:fid>/use", methods=["POST"])
@login_required
@csrf_protect
def api_library_file_use(fid):
    """
    Kopiuje plik z biblioteki (z dysku) do folderu uploads/ i zwraca ścieżkę
    do użycia w porównaniu (zamiast uploadu).
    Jeśli plik nie jest na dysku serwera, zwraca z_path — ścieżkę sieciową.
    """
    import shutil
    db = get_db()
    try:
        row = db.execute(
            "SELECT filename, file_path, z_path FROM library_files WHERE id=?", (fid,)
        ).fetchone()
    finally:
        db.close()

    if not row:
        return jsonify({"error": "Plik nie istnieje w bibliotece"}), 404

    z_path = row["z_path"] or ""
    has_file = bool(row["file_path"] and os.path.isfile(row["file_path"]))

    if not has_file:
        # Thumbnail-only mode — return z_path for the user to open manually
        return jsonify({
            "ok": True,
            "has_file": False,
            "filename": row["filename"],
            "z_path": z_path,
        })

    uid = session["user_id"]
    safe = secure_filename(row["filename"]) or f"lib_{fid}.pdf"
    dest = os.path.join(app.config["UPLOAD_FOLDER"], f"{uid}_lib_{fid}_{safe}")
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    shutil.copy2(row["file_path"], dest)
    return jsonify({
        "ok": True,
        "has_file": True,
        "path": dest,
        "filename": row["filename"],
        "z_path": z_path,
    })


@app.route("/api/library/token-reset", methods=["POST"])
@require_role("admin")
@csrf_protect
def api_library_token_reset():
    """Regeneruje token synchronizacji biblioteki."""
    import secrets as _sec
    token = _sec.token_hex(32)
    db = get_db()
    try:
        db.execute(
            "INSERT INTO settings(category,key,value) "
            "VALUES('library','sync_token',?) "
            "ON CONFLICT(category,key) DO UPDATE SET value=excluded.value", (token,)
        )
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True, "token": token})


@app.route("/api/library/stats")
@require_role("manager")
def api_library_stats():
    """Statystyki biblioteki."""
    db = get_db()
    try:
        total = (db.execute("SELECT COUNT(*) FROM library_files").fetchone() or [0])[0]
        with_file = (db.execute(
            "SELECT COUNT(*) FROM library_files WHERE file_path!='' AND file_path IS NOT NULL"
        ).fetchone() or [0])[0]
        last_sync = (db.execute(
            "SELECT MAX(synced_at) FROM library_files"
        ).fetchone() or [None])[0]
        brands = [r[0] for r in db.execute(
            "SELECT DISTINCT lvl1 FROM library_files WHERE lvl1!='' ORDER BY lvl1"
        ).fetchall()]
        try:
            mapped_count = (db.execute(
                """SELECT COUNT(DISTINCT lf.id) FROM library_files lf
                   JOIN artwork_profiles ap ON ap.master_pdf_path = lf.file_path
                   JOIN artwork_profile_fields apf ON apf.profile_id = ap.id
                   WHERE lf.file_path != '' AND lf.file_path IS NOT NULL"""
            ).fetchone() or [0])[0]
        except Exception:
            mapped_count = 0
    except Exception:
        total = with_file = mapped_count = 0; last_sync = None; brands = []
    finally:
        db.close()
    return jsonify({
        "total_files": total, "files_on_disk": with_file,
        "mapped_count": mapped_count,
        "last_sync": last_sync, "brands": brands
    })


@app.route("/artwork/render-coverage")
@login_required
def artwork_render_coverage_page():
    """Widok „Pokrycie 3D" — co z biblioteki ma już wygenerowany model 3D
    (COV-01..03). Read-only, library-wide — @login_required bez @require_role,
    jak /artwork/library (T-01-01)."""
    db = get_db()
    try:
        lvl1_options = [r[0] for r in db.execute(
            "SELECT DISTINCT lvl1 FROM library_files WHERE lvl1!='' ORDER BY lvl1"
        ).fetchall()]
    finally:
        db.close()
    return render_template("artwork_render_coverage.html", lvl1_options=lvl1_options,
                           role=session.get("role"))


@app.route("/api/artwork/render-coverage")
@login_required
def api_artwork_render_coverage():
    """JSON dla widoku Pokrycie 3D — filtrowana, paginowana lista library_files
    z LEFT JOIN artwork_render_registry (batched, nie per-wiersz — COV-01..03)."""
    ref = request.args.get("ref", "")
    lvl1 = request.args.get("lvl1", "")
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = max(1, min(int(request.args.get("per_page", 50)), 50))
    except (TypeError, ValueError):
        per_page = 50
    import artwork_render_registry as _arr
    db = get_db()
    try:
        res = _arr.coverage(db, ref=ref, lvl1=lvl1, page=page, per_page=per_page)
    finally:
        db.close()
    return jsonify(res)


@app.route("/api/artwork/render/<int:registry_id>/push-retry", methods=["POST"])
@login_required
@require_role("manager")
@csrf_protect
def api_artwork_render_push_retry(registry_id):
    """PUSH-04/05: ręczne ponowienie nieudanego pusha wiersza rejestru do PalViz.
    Manager+ (require_role). Bounded backoff w palviz_push.retry_row. Bez env
    (PALVIZ_PUSH_URL) push jest wyłączony — zwraca 'disabled', nie błąd."""
    import palviz_push as _pp
    if not _pp.is_enabled():
        return jsonify({"status": "disabled",
                        "message": "Push do PalViz nie jest skonfigurowany (PALVIZ_PUSH_URL)."}), 200
    db = get_db()
    try:
        res = _pp.retry_row(db, registry_id, user=session.get("username"))
    finally:
        db.close()
    code = 200 if res.get("status") in ("sent", "backoff", "disabled") else 202
    return jsonify(res), code


@app.route("/admin/palviz-push")
@login_required
@require_role("admin")
def admin_palviz_push_page():
    """ADMIN-01: panel dostaw push do PalViz — ostatnie próby, detal błędu,
    one-click retry (reużywa /api/artwork/render/<id>/push-retry)."""
    import palviz_push as _pp
    return render_template("admin_palviz_push.html",
                           role=session.get("role"),
                           push_enabled=_pp.is_enabled())


@app.route("/api/admin/palviz-push")
@login_required
@require_role("admin")
def api_admin_palviz_push():
    """JSON dla panelu ADMIN-01: ostatnie N prób pusha (opcjonalny filtr statusu)."""
    import palviz_push as _pp
    status = request.args.get("status", "")
    limit = request.args.get("limit", 100)
    db = get_db()
    try:
        rows = _pp.recent_deliveries(db, status=status, limit=limit)
    finally:
        db.close()
    return jsonify({"items": rows, "push_enabled": _pp.is_enabled()})


# ═══════════════════════════════════════════════════════════════
# REF DATABASE — baza kodów REF i nazw produktów
# ═══════════════════════════════════════════════════════════════

@app.route("/api/ref-database/upload", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_ref_database_upload():
    """Importuje plik Excel (.xlsx/.xls) z kolumnami REF i NAZWA do bazy ref_database."""
    if "file" not in request.files:
        return jsonify({"error": "Brak pliku"}), 400
    f = request.files["file"]
    if not f or not f.filename:
        return jsonify({"error": "Brak pliku"}), 400
    fname = f.filename.lower()
    if not (fname.endswith(".xlsx") or fname.endswith(".xls")):
        return jsonify({"error": "Wymagany plik .xlsx lub .xls"}), 400

    try:
        import openpyxl
        import io
        data = f.read()
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
    except Exception as e:
        logger.exception("Excel read error: %s", e)
        return jsonify({"error": "Błąd odczytu pliku Excel — sprawdź format i zawartość pliku."}), 400

    if not rows:
        return jsonify({"error": "Plik jest pusty"}), 400

    # Detect header row vs data-only file
    ref_col = 0
    name_col = 1
    start_row = 0
    first = [str(v).strip().upper() if v is not None else "" for v in rows[0]]
    ref_headers = {"REF", "KOD", "KOD REF", "REFCODE"}
    name_headers = {"NAZWA", "NAME", "PRODUCT", "PRODUKT", "OPIS"}
    # Try to find header columns
    for ci, val in enumerate(first):
        if val in ref_headers:
            ref_col = ci
        if val in name_headers:
            name_col = ci
    # If first row looks like a header (contains known keyword), skip it
    if any(v in ref_headers or v in name_headers for v in first):
        start_row = 1

    db = get_db()
    imported = 0
    skipped = 0
    user_id = session.get("user_id")
    try:
        for row in rows[start_row:]:
            if len(row) <= max(ref_col, name_col):
                skipped += 1
                continue
            ref_val = row[ref_col]
            name_val = row[name_col]
            if ref_val is None or name_val is None:
                skipped += 1
                continue
            ref_str = str(ref_val).strip()
            name_str = str(name_val).strip()
            if not ref_str or not name_str:
                skipped += 1
                continue
            db.execute(
                "INSERT INTO ref_database(ref_code, product_name, uploaded_by, uploaded_at) "
                "VALUES(?, ?, ?, datetime('now')) "
                "ON CONFLICT(ref_code) DO UPDATE SET product_name=excluded.product_name, "
                "uploaded_by=excluded.uploaded_by, uploaded_at=excluded.uploaded_at",
                (ref_str, name_str, user_id)
            )
            imported += 1
        db.commit()
    except Exception as e:
        db.rollback()
        logger.exception("DB write error: %s", e)
        return jsonify({"error": "Błąd zapisu — spróbuj ponownie."}), 500
    finally:
        db.close()

    _log_audit("ref_db_upload", detail=f"imported={imported} skipped={skipped}")
    return jsonify({"ok": True, "imported": imported, "skipped": skipped})


@app.route("/api/ref-database/lookup")
def api_ref_database_lookup():
    """Wyszukuje nazwę produktu po kodzie REF (exact + prefix fuzzy match)."""
    if "user_id" not in session:
        return jsonify({"error": "Wymagane logowanie"}), 401
    ref = request.args.get("ref", "").strip()
    if not ref:
        return jsonify({"ref": ref, "name": None})
    db = get_db()
    try:
        # Try exact match first
        row = db.execute(
            "SELECT product_name FROM ref_database WHERE ref_code=?", (ref,)
        ).fetchone()
        if row:
            return jsonify({"ref": ref, "name": row[0]})
        # Prefix match fallback
        ref_esc = escape_like(ref)
        row = db.execute(
            "SELECT product_name FROM ref_database WHERE ref_code LIKE ? ESCAPE '\\' ORDER BY ref_code LIMIT 1",
            (ref_esc + "%",)
        ).fetchone()
        return jsonify({"ref": ref, "name": row[0] if row else None})
    finally:
        db.close()


@app.route("/api/ref-database/stats")
def api_ref_database_stats():
    """Zwraca statystyki bazy REF."""
    if "user_id" not in session:
        return jsonify({"error": "Wymagane logowanie"}), 401
    db = get_db()
    try:
        count_row = db.execute("SELECT COUNT(*) FROM ref_database").fetchone()
        count = count_row[0] if count_row else 0
        last_row = db.execute(
            "SELECT MAX(uploaded_at) FROM ref_database"
        ).fetchone()
        last_uploaded = last_row[0] if last_row else None
    finally:
        db.close()
    return jsonify({"count": count, "last_uploaded": last_uploaded})


@app.route("/api/ref-database", methods=["DELETE"])
@require_role("admin")
@csrf_protect
def api_ref_database_clear():
    """Usuwa wszystkie wpisy z bazy REF (tylko admin)."""
    db = get_db()
    try:
        db.execute("DELETE FROM ref_database")
        db.commit()
    finally:
        db.close()
    _log_audit("ref_db_clear", detail="Wyczyszczono całą bazę REF")
    return jsonify({"ok": True})


# ═══════════════════════════════════════════════════════════════
# ARTWORK PROFILES (szablony pól artworka)
# ═══════════════════════════════════════════════════════════════

@app.route("/artwork/profiles")
@require_role("manager")
def artwork_profiles_page():
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        profiles = db.execute(
            "SELECT p.id, p.name, p.description, p.ean, p.ref_code, p.ref_list_json, "
            "p.master_pdf_path, p.page_width_mm, p.page_height_mm, p.is_active, "
            "p.use_count, p.created_at, p.folder_path, u.username as created_by_name, "
            "(p.thumb_b64 IS NOT NULL AND p.thumb_b64 != '') as has_thumb, "
            "(SELECT COUNT(*) FROM artwork_profile_fields f WHERE f.profile_id=p.id AND (f.skip_analysis IS NULL OR f.skip_analysis=0)) as field_count "
            "FROM artwork_profiles p "
            "LEFT JOIN users u ON p.created_by=u.id "
            "ORDER BY p.folder_path, p.name"
        ).fetchall()
        profiles = [dict(r) for r in profiles]
        return render_template("artwork_profiles.html",
                               username=session["username"],
                               role=session["role"],
                               profiles=profiles)
    finally:
        db.close()


# Persist master PDFs on the Render.com /data disk when available.
# On local dev falls back to uploads/masters/ inside the repo.
_DATA_DIR = os.environ.get("DATA_DIR", "/data")
if os.path.isdir(_DATA_DIR):
    MASTERS_DIR = os.path.join(_DATA_DIR, "masters")
else:
    MASTERS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads", "masters")
os.makedirs(MASTERS_DIR, exist_ok=True)


def _resolve_master_path(stored_path: str, profile_id: int = None, db=None) -> str:
    """Return a usable path for a stored master PDF.

    Absolute paths break when the deployment root changes between environments.
    Tries multiple candidate directories in order:
      1. stored_path as-is
      2. Current MASTERS_DIR (e.g. /data/masters/ on Render, uploads/masters/ locally)
      3. <app_root>/uploads/masters/  (old default before /data migration)
      4. /app/uploads/masters/        (Render container root before DATA_DIR was set)
    When found at a new path and profile_id given, self-heals the DB record.

    If the caller already holds an open connection it should pass it as ``db`` so
    the self-heal UPDATE reuses it instead of opening a second connection (avoids
    SQLite write-lock contention / potential deadlock).
    """
    if not stored_path:
        return stored_path
    if os.path.exists(stored_path):
        return stored_path
    basename = os.path.basename(stored_path)
    if not basename:
        return stored_path
    _app_root = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(MASTERS_DIR, basename),
        os.path.join(_app_root, "uploads", "masters", basename),
        os.path.join("/app", "uploads", "masters", basename),
        os.path.join("/data", "masters", basename),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            if profile_id:
                if db is not None:
                    # Reuse the caller's open connection — no second connection,
                    # no commit (the caller owns the transaction lifecycle).
                    try:
                        db.execute("UPDATE artwork_profiles SET master_pdf_path=? WHERE id=?",
                                   (candidate, profile_id))
                    except Exception:
                        pass
                else:
                    _db = None
                    try:
                        _db = get_db()
                        _db.execute("UPDATE artwork_profiles SET master_pdf_path=? WHERE id=?",
                                    (candidate, profile_id))
                        _db.commit()
                    except Exception:
                        pass
                    finally:
                        try:
                            if _db:
                                _db.close()
                        except Exception:
                            pass
            return candidate
    return stored_path


_artwork_tables_initialized = False


def _ensure_artwork_profile_tables(db):
    """Tworzy tabele artwork_profiles i artwork_profile_fields jeśli nie istnieją."""
    global _artwork_tables_initialized
    if _artwork_tables_initialized:
        return
    db.execute("""
        CREATE TABLE IF NOT EXISTS artwork_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            ean TEXT DEFAULT '',
            ref_code TEXT DEFAULT '',
            ref_list_json TEXT DEFAULT '[]',
            master_pdf_path TEXT DEFAULT '',
            thumb_b64 TEXT DEFAULT '',
            page_width_mm REAL DEFAULT 0,
            page_height_mm REAL DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_by INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS artwork_profile_fields (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL,
            field_name TEXT NOT NULL,
            display_name TEXT NOT NULL,
            x1_pct REAL NOT NULL,
            y1_pct REAL NOT NULL,
            x2_pct REAL NOT NULL,
            y2_pct REAL NOT NULL,
            severity TEXT DEFAULT 'critical',
            notes TEXT DEFAULT '',
            sort_order INTEGER DEFAULT 0
        )
    """)
    _ALLOWED_COL_TYPES = {"TEXT", "INTEGER", "REAL", "BLOB", "NUMERIC"}
    def _safe_add_col(table, col, dtype, dflt):
        if not re.match(r'^[a-z_][a-z0-9_]*$', col) or dtype.upper() not in _ALLOWED_COL_TYPES:
            return
        try:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {dtype} DEFAULT {dflt}")
            # UTRWAL od razu. Bez commitu na PostgreSQL kolejny nieudany ALTER
            # (np. kolumna już istnieje) wywoła rollback CAŁEJ transakcji i cofnie
            # też te kolumny, które właśnie dodaliśmy → „column ... does not exist".
            db.commit()
        except Exception:
            # On PostgreSQL a failed DDL aborts the transaction; rollback to recover.
            try:
                db.rollback()
            except Exception:
                pass
    for col, dflt in [
        ("ref_list_json", "'[]'"),
        ("master_pdf_path", "''"),
        ("thumb_b64", "''"),
        ("use_count", "0"),
        ("folder_path", "''"),
        ("packaging_type", "''"),
        ("revision", "''"),
        ("revision_rank", "0"),
        ("source_path", "''"),
        ("is_current_revision", "1"),
    ]:
        dtype = "INTEGER" if dflt == "0" else "TEXT"
        _safe_add_col("artwork_profiles", col, dtype, dflt)
    # artwork_profile_fields extra columns
    for col, dflt in [("size_label", "''"), ("skip_analysis", "0"), ("display_layout", "'side_by_side'")]:
        dtype = "INTEGER" if dflt == "0" else "TEXT"
        _safe_add_col("artwork_profile_fields", col, dtype, dflt)
    # Indeksy (#5 Sprint 1): wyszukiwanie profili po EAN/REF oraz JOIN/filtr pól per profil.
    db.execute("CREATE INDEX IF NOT EXISTS idx_artwork_profiles_ean ON artwork_profiles(ean)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_artwork_profiles_ref ON artwork_profiles(ref_code)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_artwork_profile_fields_profile ON artwork_profile_fields(profile_id)")
    db.commit()
    db.execute("""
        CREATE TABLE IF NOT EXISTS artwork_field_dict (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            display_name TEXT NOT NULL UNIQUE,
            default_severity TEXT DEFAULT 'critical',
            display_layout TEXT DEFAULT 'side_by_side',
            default_rotation INTEGER DEFAULT 0,
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    # artwork_field_dict extra columns for existing DBs
    _safe_add_col("artwork_field_dict", "display_layout", "TEXT", "'side_by_side'")
    _safe_add_col("artwork_field_dict", "default_rotation", "INTEGER", "0")
    _safe_add_col("artwork_field_dict", "comparison_mode", "TEXT", "'text'")
    _safe_add_col("artwork_field_dict", "numeric_tolerance", "REAL", "0.0")
    # (Migracje kolumn kolejka_status_log / kolejka_zlecenia usunięte — tabele znikły
    #  razem z modułem Kolejki/Transportu w forku Artwork.)
    # ── Material master tables ────────────────────────────────────────────
    db.execute("""
        CREATE TABLE IF NOT EXISTS artwork_pkg_level_type (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            label TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            color TEXT DEFAULT '#6b7280',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS artwork_material (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ref_code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            product_family TEXT DEFAULT '',
            ean TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS artwork_material_level (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            material_id INTEGER NOT NULL REFERENCES artwork_material(id) ON DELETE CASCADE,
            level_type_id INTEGER NOT NULL REFERENCES artwork_pkg_level_type(id),
            profile_id INTEGER REFERENCES artwork_profiles(id),
            mapping_type TEXT DEFAULT 'none',
            source_material_id INTEGER REFERENCES artwork_material(id),
            notes TEXT DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(material_id, level_type_id)
        )
    """)
    # Seed canonical flow packaging levels (SZT < JU < OP < OPZ < KAR).
    # Pusta tabela → pełny seed. Stary domyślny seed (rozpoznawany po etykiecie
    # "Sztuka (JED)") → jednorazowa migracja: dodanie JU + poprawa etykiet/kolejności
    # wierszy wciąż na starych domyślnych. Poza tym NIE ruszamy tabeli — edycje
    # i USUNIĘCIA admina są trwałe (nie re-seedujemy skasowanych poziomów).
    _seed_rows = [
        ("sztuka", "Sztuka (SZT)", 1, "#10b981"),
        ("ju",     "Jednostka użytkowa (JU)", 2, "#14b8a6"),
        ("op",     "Opakowanie (OP)", 3, "#3b82f6"),
        ("opz",    "Opakowanie zbiorcze (OPZ)", 4, "#8b5cf6"),
        ("karton", "Karton (KAR)", 5, "#f59e0b"),
    ]
    _n = db.execute("SELECT COUNT(*) AS n FROM artwork_pkg_level_type").fetchone()["n"]
    _old_seed = _n and db.execute(
        "SELECT 1 FROM artwork_pkg_level_type WHERE code='sztuka' AND label='Sztuka (JED)'"
    ).fetchone()
    if _n == 0 or _old_seed:
        for code, label, sort_order, color in _seed_rows:
            db.execute(
                "INSERT OR IGNORE INTO artwork_pkg_level_type (code, label, sort_order, color) VALUES (?, ?, ?, ?)",
                (code, label, sort_order, color)
            )
        for code, old_label, old_sort, new_label, new_sort in [
            ("sztuka", "Sztuka (JED)", 1, "Sztuka (SZT)", 1),
            ("op",     "Opakowanie (OP)", 2, "Opakowanie (OP)", 3),
            ("opz",    "OPZ", 3, "Opakowanie zbiorcze (OPZ)", 4),
            ("karton", "Karton (KAR)", 4, "Karton (KAR)", 5),
        ]:
            db.execute(
                "UPDATE artwork_pkg_level_type SET label=?, sort_order=? "
                "WHERE code=? AND label=? AND sort_order=?",
                (new_label, new_sort, code, old_label, old_sort)
            )
    # ── Operator feedback / learning tables ──────────────────────────────
    db.execute("""
        CREATE TABLE IF NOT EXISTS artwork_field_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            field_name TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            profile_name TEXT DEFAULT '',
            queue_id TEXT DEFAULT '',
            comparison_mode TEXT DEFAULT 'text',
            val_a TEXT DEFAULT '',
            val_b TEXT DEFAULT '',
            was_changed INTEGER DEFAULT 0,
            operator_verdict TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    db.commit()
    _artwork_tables_initialized = True


def _master_thumb(img, max_w=220) -> str:
    """Tworzy miniaturę mastera jako base64 JPEG."""
    import io as _io, base64 as _b64
    w, h = img.size
    if w > max_w:
        img = img.resize((max_w, int(h * max_w / w)), img.LANCZOS if hasattr(img, 'LANCZOS') else 1)
    buf = _io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return _b64.b64encode(buf.getvalue()).decode()


def _ai_detect_fields_from_b64(img_b64: str, mime: str = "image/jpeg") -> list:
    """Wywołuje Claude Vision na obrazie b64 i zwraca listę pól."""
    import json as _json
    from artwork_comparator import _get_api_key_artwork
    import anthropic
    api_key = _get_api_key_artwork()
    if not api_key:
        return []
    client = anthropic.Anthropic(api_key=api_key, timeout=120.0)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": mime, "data": img_b64}},
            {"type": "text", "text": (
                "Analyze this medical packaging artwork. Return ONLY a JSON array (no markdown).\n"
                'Each object: {"field_name":"snake_case","display_name":"Polish label",'
                '"x1_pct":5.2,"y1_pct":3.1,"x2_pct":45.8,"y2_pct":12.4,'
                '"severity":"critical|important|info","notes":"desc"}\n\n'
                "Coordinates = % of image (0,0=top-left). Crop tightly.\n"
                "critical: EAN barcode number, REF/catalog number, product name, CE/MDR+notified body, "
                "EACH size table row separately (XS row, S row, M row, L row, XL row), quantity, sterility\n"
                "important: each icon/pictogram, translation blocks by language group, AQL, storage\n"
                "info: manufacturer address, importer, website\n"
                "Return 15-45 fields. Be precise."
            )}
        ]}]
    )
    raw = (response.content[0].text if response.content and hasattr(response.content[0], "text") else "").strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"): raw = raw[4:]
    raw = raw.strip()
    try:
        fields = _json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(fields, list):
        return []
    clean = []
    for f in fields:
        if not all(k in f for k in ("x1_pct","y1_pct","x2_pct","y2_pct")):
            continue
        try:
            _x1 = float(f["x1_pct"]); _y1 = float(f["y1_pct"])
            _x2 = float(f["x2_pct"]); _y2 = float(f["y2_pct"])
        except (TypeError, ValueError):
            continue   # pojedyncze złe pole z AI nie może wywalić całej partii
        sev = f.get("severity","important")
        clean.append({
            "field_name":   str(f.get("field_name","field")),
            "display_name": str(f.get("display_name", f.get("field_name","Pole"))),
            "x1_pct": max(0.0, min(99.0, _x1)),
            "y1_pct": max(0.0, min(99.0, _y1)),
            "x2_pct": max(1.0, min(100.0, _x2)),
            "y2_pct": max(1.0, min(100.0, _y2)),
            "severity": sev if sev in ("critical","important","info") else "important",
            "notes": str(f.get("notes","")),
        })
    return clean


_VALID_FIELD_SEVERITIES = {"critical", "important", "info", "warning"}

def _save_profile_fields(db, pid, fields):
    if not fields:
        return
    db.execute("DELETE FROM artwork_profile_fields WHERE profile_id=?", (pid,))
    for i, f in enumerate(fields):
        if not all(k in f for k in ("x1_pct", "y1_pct", "x2_pct", "y2_pct")):
            continue
        try:
            x1 = float(f["x1_pct"]); y1 = float(f["y1_pct"])
            x2 = float(f["x2_pct"]); y2 = float(f["y2_pct"])
        except (TypeError, ValueError):
            continue
        # Clamp to valid percentage range
        x1 = max(0.0, min(99.0, x1)); y1 = max(0.0, min(99.0, y1))
        x2 = max(1.0, min(100.0, x2)); y2 = max(1.0, min(100.0, y2))
        # Ensure x1 < x2 and y1 < y2 (non-zero-area region)
        if x1 >= x2: x2 = min(100.0, x1 + 1.0)
        if y1 >= y2: y2 = min(100.0, y1 + 1.0)
        sev = f.get("severity", "critical")
        if sev not in _VALID_FIELD_SEVERITIES:
            sev = "critical"
        db.execute(
            "INSERT INTO artwork_profile_fields "
            "(profile_id, field_name, display_name, x1_pct, y1_pct, x2_pct, y2_pct, "
            "severity, notes, sort_order, size_label, skip_analysis) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, str(f.get("field_name") or f.get("display_name") or "field")[:200],
             str(f.get("display_name", "") or "")[:200], x1, y1, x2, y2,
             sev, str(f.get("notes", "") or "")[:500], i,
             str(f.get("size_label", "") or "")[:50], 1 if f.get("skip_analysis") else 0)
        )


@app.route("/api/artwork/masters/upload", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_masters_bulk_upload():
    """
    Bulk upload masterów artworków.
    Dla każdego pliku PDF:
      1. Zapisuje do uploads/masters/
      2. Renderuje 300 DPI, tworzy miniaturę
      3. Ekstraktuje EAN/REF przez OCR
      4. Wywołuje Claude AI → wykrywa pola z bboxami
      5. Zapisuje profil z przypiętym masterem i polami
    Zwraca listę wyników per plik.
    """
    import base64 as _b64, io as _io, hashlib as _hs, json as _json, re as _re
    from artwork_comparator import _load_pages, _PROFILE_RENDER_DPI, _extract_text_ocr, _extract_ean
    from artwork_naming import parse_master_filename

    files = request.files.getlist("files[]")
    if not files:
        return jsonify({"error": "Brak plików"}), 400
    # Optional per-file original paths (np. Z:\...) aligned with files[] — used by
    # the local bulk uploader to preserve the source location for later reference.
    source_paths = request.form.getlist("source_paths[]")

    raw_folder = (request.form.get("folder_path") or "").strip().strip("/")
    # Filter out ".." and "." to prevent path-traversal strings being stored
    # in artwork_profiles.folder_path and later used in filesystem operations.
    upload_folder = "/".join(
        p.strip() for p in raw_folder.replace("\\", "/").split("/")
        if p.strip() and p.strip() not in ("..", ".")
    )

    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
    except Exception:
        pass
    results = []

    try:
        for _fi, file in enumerate(files):
            fname = file.filename or "unknown.pdf"
            result = {"filename": fname, "status": "error", "error": "", "pid": None, "field_count": 0}
            try:
                # Size guard before reading into RAM
                file.seek(0, 2)
                fsize_mb = file.tell() / 1024 / 1024
                file.seek(0)
                if fsize_mb > 150:
                    raise ValueError(f"Plik zbyt duży ({fsize_mb:.0f} MB). Maksimum 150 MB.")
                # Save master PDF to persistent directory
                raw = file.read()
                if not raw.startswith(b"%PDF-"):
                    raise ValueError("Plik nie jest prawidłowym PDF (rozszerzenie nie zgadza się z treścią).")
                h = _hs.md5(raw, usedforsecurity=False).hexdigest()[:12]
                safe = _re.sub(r'[^\w\-.]', '_', os.path.splitext(fname)[0])[:40]
                pdf_path = os.path.join(MASTERS_DIR, f"{safe}_{h}.pdf")
                with open(pdf_path, "wb") as fout:
                    fout.write(raw)

                # Render at 300 DPI
                pages = _load_pages(pdf_path, _PROFILE_RENDER_DPI, page_idx=None)
                if not pages:
                    raise ValueError("Nie można wyrenderować PDF")
                img = pages[0]

                # Thumbnail
                thumb = _master_thumb(img)

                # Extract EAN/REF via OCR
                ocr_text = _extract_text_ocr(img)
                ean_val = _extract_ean(ocr_text) or ""

                # Parse REF + packaging type + revision from the ACME filename
                parsed = parse_master_filename(fname)
                ref_from_name = parsed["ref"] or safe
                pkg_type = parsed["packaging_type"]
                rev_str = parsed["revision"]
                rev_rank = parsed["revision_rank"]
                if not ean_val and parsed["ean"]:
                    ean_val = parsed["ean"]
                src_path = (source_paths[_fi] if _fi < len(source_paths) else "")[:500]

                # No AI on upload — user maps fields manually in the editor
                fields = []

                # Check if profile with same master PDF already exists → update
                existing = db.execute(
                    "SELECT id FROM artwork_profiles WHERE master_pdf_path=?", (pdf_path,)
                ).fetchone()

                profile_name = os.path.splitext(fname)[0]
                ref_list = [ref_from_name] if ref_from_name else []

                if existing:
                    pid = existing["id"]
                    db.execute(
                        "UPDATE artwork_profiles SET name=?, ean=?, ref_code=?, ref_list_json=?, "
                        "thumb_b64=?, folder_path=?, packaging_type=?, revision=?, revision_rank=?, "
                        "source_path=?, updated_at=datetime('now') WHERE id=?",
                        (profile_name, ean_val, ref_from_name,
                         _json.dumps(ref_list, ensure_ascii=False), thumb, upload_folder,
                         pkg_type, rev_str, rev_rank, src_path, pid)
                    )
                    db.commit()
                    # Do NOT call _save_profile_fields here — fields=[] always on upload,
                    # and _save_profile_fields DELETEs all existing fields before inserting.
                    # Calling it would silently wipe every manually-mapped bbox field.
                else:
                    cur = db.execute(
                        "INSERT INTO artwork_profiles "
                        "(name, ean, ref_code, ref_list_json, master_pdf_path, thumb_b64, "
                        "folder_path, packaging_type, revision, revision_rank, source_path, "
                        "is_active, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?)",
                        (profile_name, ean_val, ref_from_name,
                         _json.dumps(ref_list, ensure_ascii=False),
                         pdf_path, thumb, upload_folder,
                         pkg_type, rev_str, rev_rank, src_path, session["user_id"])
                    )
                    pid = cur.lastrowid
                    # Single commit AFTER _save_profile_fields so both writes are atomic.
                    # (Previously db.commit() was called here, before _save_profile_fields,
                    # which orphaned the artwork_profiles row when _save_profile_fields raised.)
                    if fields:
                        _save_profile_fields(db, pid, fields)
                    db.commit()

                result.update({
                    "status": "ok", "pid": pid,
                    "field_count": len(fields),
                    "ean": ean_val, "ref": ref_from_name,
                    "packaging_type": pkg_type, "revision": rev_str,
                    "thumb_b64": thumb,
                })
            except Exception as e:
                # Rollback on EVERY per-file error so PostgreSQL doesn't leave
                # the shared db connection in an aborted-transaction state,
                # which would cause every subsequent file in the loop to fail
                # with "InFailedSqlTransaction" even if the file itself is valid.
                try:
                    db.rollback()
                except Exception:
                    pass
                result["error"] = str(e)[:200]
            results.append(result)
    finally:
        db.close()
    ok = sum(1 for r in results if r["status"] == "ok")
    return jsonify({"ok": True, "total": len(results), "success": ok, "results": results})


@app.route("/api/artwork/profiles", methods=["GET"])
@login_required
def api_artwork_profiles_list():
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        rows = db.execute(
            "SELECT p.id, p.name, p.description, p.ean, p.ref_code, p.ref_list_json, "
            "p.master_pdf_path, p.page_width_mm, p.page_height_mm, p.is_active, "
            "p.use_count, p.created_at, p.folder_path, "
            "(p.thumb_b64 IS NOT NULL AND p.thumb_b64 != '') as has_thumb, "
            "(SELECT COUNT(*) FROM artwork_profile_fields f WHERE f.profile_id=p.id AND (f.skip_analysis IS NULL OR f.skip_analysis=0)) as field_count "
            "FROM artwork_profiles p ORDER BY p.folder_path, p.name"
        ).fetchall()
        field_rows = db.execute(
            "SELECT profile_id, display_name FROM artwork_profile_fields "
            "WHERE (skip_analysis IS NULL OR skip_analysis=0) ORDER BY profile_id, sort_order"
        ).fetchall()
        fields_by_pid = {}
        for fr in field_rows:
            fields_by_pid.setdefault(fr["profile_id"], []).append(fr["display_name"])
        result = []
        for r in rows:
            d = dict(r)
            d["field_names"] = fields_by_pid.get(r["id"], [])
            result.append(d)
        return jsonify(result)
    finally:
        db.close()


@app.route("/api/artwork/profiles/<int:pid>/canvas")
@require_role("manager")
def api_artwork_profile_canvas(pid):
    """Renderuje zapisany PDF mastera w wysokiej jakości do edytora.
    Gdy plik PDF nie istnieje na dysku (np. po rebuildzie kontenera),
    zwraca zapisaną miniaturę z bazy danych jako fallback.
    Gdy miniatura jest pusta ale PDF istnieje, auto-zapisuje miniaturę.
    """
    import io as _io, base64 as _b64c
    from artwork_comparator import _load_pages, _PROFILE_RENDER_DPI
    from flask import Response

    db = get_db()
    try:
        row = db.execute(
            "SELECT master_pdf_path, thumb_b64 FROM artwork_profiles WHERE id=?", (pid,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono profilu"}), 404

        # Try rendering from PDF file
        pdf_path = row["master_pdf_path"] or ""
        if pdf_path:
            resolved = _resolve_master_path(pdf_path, profile_id=pid, db=db)
            if os.path.isfile(resolved):
                # Adaptive DPI: ensure minimum 2000px on the longer side for sharp display
                render_dpi = _PROFILE_RENDER_DPI
                try:
                    import fitz as _fitz_canvas
                    _cdoc = _fitz_canvas.open(resolved)
                    _cp = _cdoc[0]
                    _max_pts = max(_cp.rect.width, _cp.rect.height, 1)
                    _cdoc.close()
                    render_dpi = min(900, max(_PROFILE_RENDER_DPI, int(2000 * 72 / _max_pts)))
                except Exception:
                    pass
                pages = _load_pages(resolved, render_dpi, page_idx=None)
                if pages:
                    buf = _io.BytesIO()
                    pages[0].save(buf, "JPEG", quality=92)
                    img_bytes = buf.getvalue()
                    # Auto-save thumbnail when missing so future fallbacks work
                    if not (row["thumb_b64"] or ""):
                        try:
                            thumb = _master_thumb(pages[0])
                            db.execute(
                                "UPDATE artwork_profiles SET thumb_b64=? WHERE id=?",
                                (thumb, pid)
                            )
                            db.commit()
                        except Exception:
                            pass
                    return Response(img_bytes, mimetype="image/jpeg",
                                    headers={"Cache-Control": "private, max-age=300"})

        # PDF missing or unrenderable — fall back to stored thumbnail
        thumb_b64 = row["thumb_b64"] or ""
        if thumb_b64:
            try:
                img_bytes = _b64c.b64decode(thumb_b64)
            except Exception:
                return jsonify({"error": "Miniatura uszkodzona. Załaduj plik PDF ponownie."}), 404
            return Response(img_bytes, mimetype="image/jpeg",
                            headers={"Cache-Control": "public, max-age=3600",
                                     "X-Canvas-Fallback": "thumb"})

        return jsonify({"error": "Brak pliku PDF i miniatury. Załaduj plik PDF."}), 404
    finally:
        db.close()


@app.route("/api/artwork/masters/download/<int:pid>")
@require_role("manager")
def api_artwork_master_download(pid):
    """Pobierz plik PDF mastera."""
    from flask import send_file
    db = get_db()
    try:
        row = db.execute("SELECT master_pdf_path, name FROM artwork_profiles WHERE id=?", (pid,)).fetchone()
        if not row or not row["master_pdf_path"]:
            return jsonify({"error": "Brak pliku"}), 404
        path = _resolve_master_path(row["master_pdf_path"])
        if not os.path.isfile(path):
            return jsonify({"error": "Plik nie istnieje na dysku"}), 404
        real_path = os.path.realpath(path)
        real_masters = os.path.realpath(MASTERS_DIR)
        if not real_path.startswith(real_masters + os.sep):
            return jsonify({"error": "Niedozwolona ścieżka pliku"}), 403
        safe_name = (row["name"] or "master").replace("/", "_")[:60] + ".pdf"
        return send_file(path, mimetype="application/pdf",
                         as_attachment=True, download_name=safe_name)
    finally:
        db.close()


@app.route("/api/artwork/masters/storage-info")
@require_role("manager")
def api_artwork_masters_storage_info():
    """Zwraca informacje o zużyciu dysku przez mastery."""
    import shutil
    info = {"dir": MASTERS_DIR, "files": [], "total_bytes": 0, "total_mb": 0, "disk_free_gb": None}
    try:
        if os.path.isdir(MASTERS_DIR):
            for fname in os.listdir(MASTERS_DIR):
                fpath = os.path.join(MASTERS_DIR, fname)
                if os.path.isfile(fpath):
                    sz = os.path.getsize(fpath)
                    info["files"].append({"name": fname, "size_bytes": sz, "size_mb": round(sz/1024/1024, 2)})
                    info["total_bytes"] += sz
        info["total_mb"] = round(info["total_bytes"] / 1024 / 1024, 1)
        try:
            usage = shutil.disk_usage(MASTERS_DIR)
            info["disk_free_gb"] = round(usage.free / 1024**3, 2)
            info["disk_total_gb"] = round(usage.total / 1024**3, 2)
            info["disk_used_gb"] = round(usage.used / 1024**3, 2)
        except Exception:
            pass
    except Exception as e:
        info["error"] = str(e)[:200]
    return jsonify(info)


@app.route("/api/artwork/profiles/<int:pid>/thumb")
@login_required
def api_artwork_profile_thumb(pid):
    db = get_db()
    try:
        row = db.execute("SELECT thumb_b64 FROM artwork_profiles WHERE id=?", (pid,)).fetchone()
        if not row or not row["thumb_b64"]:
            return "", 404
        import base64 as _b64
        try:
            img_bytes = _b64.b64decode(row["thumb_b64"])
        except Exception:
            return "", 404
        from flask import Response
        return Response(img_bytes, mimetype="image/jpeg",
                        headers={"Cache-Control": "public, max-age=3600"})
    finally:
        db.close()


@app.route("/api/artwork/profiles/<int:pid>/move", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_profile_move(pid):
    """Przenosi master do innego folderu."""
    data = request.get_json(silent=True) or {}
    folder = str(data.get("folder_path") or "").strip().strip("/")[:500]
    folder = "/".join(p.strip() for p in folder.replace("\\", "/").split("/") if p.strip())
    db = get_db()
    try:
        db.execute("UPDATE artwork_profiles SET folder_path=?, updated_at=datetime('now') WHERE id=?",
                   (folder, pid))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/artwork/profiles/<int:pid>", methods=["GET"])
@login_required
def api_artwork_profile_get(pid):
    db = get_db()
    try:
        row = db.execute("SELECT * FROM artwork_profiles WHERE id=?", (pid,)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono"}), 404
        profile = dict(row)
        fields = db.execute(
            "SELECT * FROM artwork_profile_fields WHERE profile_id=? ORDER BY sort_order",
            (pid,)
        ).fetchall()
        profile["fields"] = [dict(f) for f in fields]
        return jsonify(profile)
    finally:
        db.close()


@app.route("/api/artwork/profiles", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_profile_create():
    data = request.get_json(force=True, silent=True)
    err = _validate_json_body(data, {"name": (str, True)})
    if err:
        return jsonify({"error": err}), 400
    data = data or {}
    name = str(data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Nazwa profilu jest wymagana"}), 400
    import json as _json
    ref_list = data.get("ref_list", [])
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        try:
            _pw = float(data.get("page_width_mm") or 0)
            _ph = float(data.get("page_height_mm") or 0)
        except (TypeError, ValueError):
            _pw = _ph = 0.0
        _pw = max(0.0, min(5000.0, _pw))
        _ph = max(0.0, min(5000.0, _ph))
        _cur = db.execute(
            "INSERT INTO artwork_profiles (name, description, ean, ref_code, ref_list_json, "
            "page_width_mm, page_height_mm, is_active, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (name[:200], str(data.get("description") or "")[:500], str(data.get("ean") or "")[:20],
             str(data.get("ref_code") or "")[:100], _json.dumps(ref_list, ensure_ascii=False),
             _pw, _ph, session["user_id"])
        )
        pid = _cur.lastrowid
        _save_profile_fields(db, pid, data.get("fields", []))
        db.commit()
        return jsonify({"ok": True, "id": pid})
    finally:
        db.close()


@app.route("/api/artwork/profiles/import-csv", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_profiles_import_csv():
    """Import profili artworków z pliku CSV.

    Wymagane kolumny CSV: name, ean, ref_code
    Opcjonalne: description, folder_path

    Zwraca liczbę zaimportowanych i pominiętych (duplikatów) profili.
    """
    import csv, io as _io
    file = request.files.get("file")
    if not file or not (file.filename or "").lower().endswith(".csv"):
        return jsonify({"error": "Wymagany plik CSV (.csv)"}), 400
    try:
        content = file.read().decode("utf-8-sig")  # handle BOM
    except UnicodeDecodeError:
        try:
            file.stream.seek(0)
            content = file.read().decode("cp1250")
        except Exception:
            return jsonify({"error": "Nie można odczytać pliku — sprawdź kodowanie (UTF-8 lub CP1250)"}), 400

    reader = csv.DictReader(_io.StringIO(content))
    if "name" not in (reader.fieldnames or []):
        return jsonify({"error": "Brak wymaganej kolumny 'name' w CSV"}), 400

    db = get_db()
    created, skipped = 0, 0
    errors = []
    try:
        _ensure_artwork_profile_tables(db)
        for i, row in enumerate(reader, 2):
            name = (row.get("name") or "").strip()
            if not name:
                skipped += 1
                continue
            ean = (row.get("ean") or "").strip()
            ref_code = (row.get("ref_code") or "").strip()
            description = (row.get("description") or "").strip()
            folder_path = (row.get("folder_path") or "").strip()
            existing = db.execute(
                "SELECT id FROM artwork_profiles WHERE name=?", (name,)
            ).fetchone()
            if existing:
                skipped += 1
                continue
            try:
                db.execute(
                    "INSERT INTO artwork_profiles (name, description, ean, ref_code, "
                    "is_active, created_by, folder_path) VALUES (?,?,?,?,1,?,?)",
                    (name[:200], description[:500], ean[:20], ref_code[:100],
                     session["user_id"], folder_path[:500])
                )
                created += 1
            except Exception as _e:
                errors.append(f"Wiersz {i}: {str(_e)[:200]}")
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True, "created": created, "skipped": skipped,
                    "errors": errors[:10]})


@app.route("/api/artwork/profiles/<int:pid>", methods=["PUT"])
@require_role("manager")
@csrf_protect
def api_artwork_profile_update(pid):
    data = request.get_json(force=True, silent=True) or {}
    import json as _json
    ref_list = data.get("ref_list", [])
    uid  = session["user_id"]
    uname = session["username"]
    db = get_db()
    try:
        row = db.execute("SELECT * FROM artwork_profiles WHERE id=?", (pid,)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono"}), 404
        name = str(data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "Nazwa profilu jest wymagana"}), 400
        folder = str(data.get("folder_path") or "").strip().strip("/")
        folder = "/".join(p.strip() for p in folder.replace("\\", "/").split("/") if p.strip())
        # Save history snapshot before overwriting
        try:
            fields_rows = db.execute(
                "SELECT * FROM artwork_profile_fields WHERE profile_id=? ORDER BY id", (pid,)
            ).fetchall()
            snapshot = {
                "profile": dict(row),
                "fields": [dict(f) for f in fields_rows],
            }
            db.execute(
                "INSERT INTO artwork_profile_history(profile_id, action, changed_by, changed_by_name, snapshot_json)"
                " VALUES(?,?,?,?,?)",
                (pid, "update", uid, uname, _json.dumps(snapshot, ensure_ascii=False, default=str))
            )
        except Exception:
            pass
        try:
            _pw2 = float(data.get("page_width_mm") or 0)
            _ph2 = float(data.get("page_height_mm") or 0)
        except (TypeError, ValueError):
            _pw2 = _ph2 = 0.0
        _pw2 = max(0.0, min(5000.0, _pw2))
        _ph2 = max(0.0, min(5000.0, _ph2))
        db.execute(
            "UPDATE artwork_profiles SET name=?, description=?, ean=?, ref_code=?, "
            "ref_list_json=?, page_width_mm=?, page_height_mm=?, is_active=?, "
            "folder_path=?, updated_at=datetime('now') WHERE id=?",
            (name[:200], str(data.get("description") or "")[:500], str(data.get("ean") or "")[:20],
             str(data.get("ref_code") or "")[:100], _json.dumps(ref_list, ensure_ascii=False),
             _pw2, _ph2,
             (1 if data.get("is_active", 1) else 0), folder[:500], pid)
        )
        if "fields" in data:
            _save_profile_fields(db, pid, data["fields"])
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/artwork/profiles/<int:pid>/history")
@require_role("manager")
def api_artwork_profile_history(pid):
    """Return audit trail for an artwork profile (last 50 revisions)."""
    db = get_db()
    try:
        rows = db.execute(
            "SELECT id, action, changed_by_name, created_at FROM artwork_profile_history"
            " WHERE profile_id=? ORDER BY created_at DESC LIMIT 50",
            (pid,)
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()


@app.route("/api/artwork/profiles/<int:pid>/history/<int:hid>")
@require_role("manager")
def api_artwork_profile_history_snapshot(pid, hid):
    """Return the full snapshot JSON for a specific history entry."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT snapshot_json, created_at, changed_by_name, action"
            " FROM artwork_profile_history WHERE id=? AND profile_id=?",
            (hid, pid)
        ).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono"}), 404
        try:
            snapshot = json.loads(row["snapshot_json"] or "{}")
        except Exception:
            snapshot = {}
        return jsonify({
            "snapshot": snapshot,
            "created_at": row["created_at"],
            "changed_by_name": row["changed_by_name"],
            "action": row["action"],
        })
    finally:
        db.close()


@app.route("/api/artwork/profiles/ai-detect", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_profile_ai_detect():
    """Wysyła artworka do Claude Vision i zwraca AI-sugerowane pola z bboxami."""
    import base64 as _b64, io as _io, json as _json
    from artwork_comparator import _load_pages, _PROFILE_RENDER_DPI, _get_api_key_artwork

    file = request.files.get("file")
    if not file:
        return jsonify({"error": "Brak pliku PDF"}), 400

    api_key = _get_api_key_artwork()
    if not api_key:
        return jsonify({"error": "Brak klucza API Claude"}), 400

    uid = session["user_id"]
    import uuid as _uuid
    tmp = os.path.join(app.config["UPLOAD_FOLDER"], f"_ai_detect_{uid}_{_uuid.uuid4().hex}.pdf")
    try:
        file.save(tmp)
        pages = _load_pages(tmp, _PROFILE_RENDER_DPI, page_idx=None)
        if not pages:
            return jsonify({"error": "Nie można wyrenderować PDF"}), 400

        buf = _io.BytesIO()
        pages[0].save(buf, "JPEG", quality=85)
        img_b64 = _b64.b64encode(buf.getvalue()).decode()

        import anthropic
        client = anthropic.Anthropic(api_key=api_key, timeout=120.0)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg",
                                   "data": img_b64},
                    },
                    {
                        "type": "text",
                        "text": (
                            "Analyze this medical packaging artwork and identify ALL fields requiring quality control verification.\n\n"
                            "Return ONLY a valid JSON array (no markdown, no code blocks). Each object:\n"
                            "{\n"
                            '  "field_name": "snake_case_id",\n'
                            '  "display_name": "Polish label",\n'
                            '  "x1_pct": 5.2,\n'
                            '  "y1_pct": 3.1,\n'
                            '  "x2_pct": 45.8,\n'
                            '  "y2_pct": 12.4,\n'
                            '  "severity": "critical|important|info",\n'
                            '  "notes": "brief description"\n'
                            "}\n\n"
                            "Coordinates are % of full image (0,0=top-left, 100,100=bottom-right). Crop tightly.\n\n"
                            "Severity: critical=EAN/REF/CE/rozmiary/sterylność/ilość, important=tłumaczenia/ikony/specs, info=adres/www\n\n"
                            "Identify individually:\n"
                            "- EAN-13 barcode number area\n"
                            "- REF/catalog number\n"
                            "- Product name\n"
                            "- CE/MDR marking with notified body number\n"
                            "- EACH row of size table (XS/S/M/L/XL with values) — separate field per row\n"
                            "- Quantity/count\n"
                            "- Sterility/single-use symbols\n"
                            "- Each icon/pictogram separately\n"
                            "- Translation blocks by language group\n"
                            "- Manufacturer block\n"
                            "- Importer block\n\n"
                            "Be precise. Return 15-40 fields typical for medical glove packaging."
                        )
                    }
                ]
            }]
        )

        raw = (response.content[0].text if response.content and hasattr(response.content[0], "text") else "").strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        fields = _json.loads(raw)
        if not isinstance(fields, list):
            raise ValueError("Not a list")

        # Validate and clamp all coordinates
        clean = []
        for f in fields:
            if not all(k in f for k in ("x1_pct", "y1_pct", "x2_pct", "y2_pct")):
                continue
            try:
                _x1 = max(0.0, min(99.0, float(f["x1_pct"])))
                _y1 = max(0.0, min(99.0, float(f["y1_pct"])))
                _x2 = max(1.0, min(100.0, float(f["x2_pct"])))
                _y2 = max(1.0, min(100.0, float(f["y2_pct"])))
            except (TypeError, ValueError):
                continue
            if _x1 >= _x2: _x2 = min(100.0, _x1 + 1.0)
            if _y1 >= _y2: _y2 = min(100.0, _y1 + 1.0)
            clean.append({
                "field_name":   str(f.get("field_name", "field")),
                "display_name": str(f.get("display_name", f.get("field_name", "Pole"))),
                "x1_pct": _x1, "y1_pct": _y1, "x2_pct": _x2, "y2_pct": _y2,
                "severity": f.get("severity", "important") if f.get("severity") in ("critical","important","info") else "important",
                "notes":    str(f.get("notes", "")),
            })
        return jsonify({"ok": True, "fields": clean, "count": len(clean)})

    except Exception as e:
        import traceback as _tb
        app.logger.error("AI detect (PDF) error: %s", _tb.format_exc())
        return jsonify({"error": "Błąd modułu AI — spróbuj ponownie."}), 500
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


@app.route("/api/artwork/profiles/ai-detect-image", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_profile_ai_detect_image():
    """Przyjmuje obraz JPEG (z canvas) i zwraca AI-sugerowane pola."""
    import base64 as _b64, json as _json
    from artwork_comparator import _get_api_key_artwork

    file = request.files.get("file")
    if not file:
        return jsonify({"error": "Brak pliku"}), 400

    api_key = _get_api_key_artwork()
    if not api_key:
        return jsonify({"error": "Brak klucza API Claude"}), 400

    try:
        img_bytes = file.read()
        img_b64 = _b64.b64encode(img_bytes).decode()
        # Detect mime type
        mime = "image/jpeg" if img_bytes[:2] == b'\xff\xd8' else "image/png"

        import anthropic
        client = anthropic.Anthropic(api_key=api_key, timeout=120.0)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime, "data": img_b64}},
                    {"type": "text", "text": (
                        "Analyze this medical packaging artwork and identify ALL fields requiring quality control verification.\n\n"
                        "Return ONLY a valid JSON array (no markdown). Each object:\n"
                        '{"field_name":"snake_case","display_name":"Polish label",'
                        '"x1_pct":5.2,"y1_pct":3.1,"x2_pct":45.8,"y2_pct":12.4,'
                        '"severity":"critical|important|info","notes":"brief desc"}\n\n'
                        "Coordinates: % of image (0,0=top-left, 100,100=bottom-right). Crop tightly.\n"
                        "critical=EAN/REF/CE mark/rozmiary/sterylność/ilość\n"
                        "important=tłumaczenia/ikony/specs\ninfo=adres/www\n\n"
                        "Identify individually:\n"
                        "- EAN-13 barcode digits area\n- REF/catalog number\n- Product name\n"
                        "- CE/MDR marking + notified body number\n"
                        "- EACH size table row separately (XS row, S row, M row, L row, XL row)\n"
                        "- Quantity/count (e.g. 100 szt)\n- Sterility symbol\n- Single-use symbol\n"
                        "- Latex-free / AQL icon areas\n- Storage conditions icon\n"
                        "- Translation blocks (group by language family: EN/DE/FR, PL/CZ/SK, etc.)\n"
                        "- Manufacturer block\n- Importer block\n\n"
                        "Return 15-45 fields. Be precise with coordinates."
                    )}
                ]
            }]
        )

        raw = (response.content[0].text if response.content and hasattr(response.content[0], "text") else "").strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"): raw = raw[4:]
        raw = raw.strip().strip("` \n")

        fields = _json.loads(raw)
        if not isinstance(fields, list):
            raise ValueError("Response is not a list")

        clean = []
        for f in fields:
            if not all(k in f for k in ("x1_pct","y1_pct","x2_pct","y2_pct")):
                continue
            try:
                _x1 = max(0.0, min(99.0, float(f["x1_pct"])))
                _y1 = max(0.0, min(99.0, float(f["y1_pct"])))
                _x2 = max(1.0, min(100.0, float(f["x2_pct"])))
                _y2 = max(1.0, min(100.0, float(f["y2_pct"])))
            except (TypeError, ValueError):
                continue   # pojedyncze złe pole z AI nie może wywalić całej partii
            sev = f.get("severity","important")
            clean.append({
                "field_name":   str(f.get("field_name","field")),
                "display_name": str(f.get("display_name", f.get("field_name","Pole"))),
                "x1_pct": _x1,
                "y1_pct": _y1,
                "x2_pct": _x2,
                "y2_pct": _y2,
                "severity": sev if sev in ("critical","important","info") else "important",
                "notes": str(f.get("notes","")),
            })
        return jsonify({"ok": True, "fields": clean, "count": len(clean)})

    except Exception as e:
        import traceback as _tb
        app.logger.error("AI detect (image) error: %s", _tb.format_exc())
        return jsonify({"error": "Błąd modułu AI — spróbuj ponownie."}), 500


@app.route("/api/artwork/profiles/<int:pid>", methods=["DELETE"])
@require_role("manager")
@csrf_protect
def api_artwork_profile_delete(pid):
    db = get_db()
    try:
        db.execute("DELETE FROM artwork_profile_fields WHERE profile_id=?", (pid,))
        db.execute("DELETE FROM artwork_profiles WHERE id=?", (pid,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/artwork/profiles/<int:pid>/duplicate", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_profile_duplicate(pid):
    """Duplikuje profil artworku wraz z polami — nowy profil otrzymuje suffix '(kopia)'."""
    import json as _json
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        src = db.execute("SELECT * FROM artwork_profiles WHERE id=?", (pid,)).fetchone()
        if not src:
            return jsonify({"error": "Profil nie istnieje"}), 404
        fields = db.execute(
            "SELECT * FROM artwork_profile_fields WHERE profile_id=? ORDER BY sort_order", (pid,)
        ).fetchall()
        new_name = (src["name"] or "") + " (kopia)"
        _dup_cur = db.execute(
            "INSERT INTO artwork_profiles (name, description, ean, ref_code, ref_list_json, "
            "master_pdf_path, thumb_b64, page_width_mm, page_height_mm, is_active, "
            "created_by, folder_path) VALUES (?,?,?,?,?,?,?,?,?,1,?,?)",
            (new_name, src["description"], src["ean"], src["ref_code"],
             src["ref_list_json"], src["master_pdf_path"], src["thumb_b64"],
             src["page_width_mm"], src["page_height_mm"],
             session["user_id"], src["folder_path"] or "")
        )
        new_pid = _dup_cur.lastrowid
        for f in fields:
            db.execute(
                "INSERT INTO artwork_profile_fields "
                "(profile_id, field_name, display_name, x1_pct, y1_pct, x2_pct, y2_pct, "
                "severity, notes, sort_order, display_layout, skip_analysis) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_pid, f["field_name"], f["display_name"],
                 f["x1_pct"], f["y1_pct"], f["x2_pct"], f["y2_pct"],
                 f["severity"], f["notes"] or "", f["sort_order"],
                 f["display_layout"] if "display_layout" in f.keys() else "side_by_side",
                 f["skip_analysis"] if "skip_analysis" in f.keys() else 0)
            )
        db.commit()
        return jsonify({"ok": True, "new_id": new_pid, "name": new_name})
    finally:
        db.close()


@app.route("/api/artwork/profiles/<int:pid>/render", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_profile_render(pid):
    """Renderuje artworka z nałożonymi polami szablonu — podgląd dla edytora."""
    import base64 as _b64, io as _io
    from artwork_comparator import _load_pages, _PROFILE_RENDER_DPI

    file = request.files.get("file")
    if not file:
        return jsonify({"error": "Brak pliku PDF"}), 400

    db = get_db()
    try:
        profile_row = db.execute("SELECT * FROM artwork_profiles WHERE id=?", (pid,)).fetchone()
        fields = db.execute(
            "SELECT * FROM artwork_profile_fields WHERE profile_id=? ORDER BY sort_order", (pid,)
        ).fetchall()
        if not profile_row:
            return jsonify({"error": "Profil nie istnieje"}), 404
    finally:
        db.close()

    uid = session["user_id"]
    import uuid as _uuid
    upload_dir = app.config["UPLOAD_FOLDER"]
    tmp_path = os.path.join(upload_dir, f"_profile_render_{uid}_{pid}_{_uuid.uuid4().hex}.pdf")
    try:
        file.save(tmp_path)
        pages = _load_pages(tmp_path, _PROFILE_RENDER_DPI, page_idx=None)
        if not pages:
            return jsonify({"error": "Nie można wyrenderować PDF"}), 400

        from PIL import ImageDraw
        img = pages[0].copy()
        draw = ImageDraw.Draw(img)
        w, h = img.size
        colors = {"critical": (220, 38, 38, 100), "important": (234, 179, 8, 100),
                  "info": (59, 130, 246, 100)}

        for f in fields:
            x1 = int(f["x1_pct"] * w / 100)
            y1 = int(f["y1_pct"] * h / 100)
            x2 = int(f["x2_pct"] * w / 100)
            y2 = int(f["y2_pct"] * h / 100)
            color = colors.get(f["severity"], colors["info"])
            draw.rectangle([x1, y1, x2, y2], outline=color[:3], width=3)
            draw.rectangle([x1, y1, x2, min(y1+22, y2)], fill=color[:3])
            draw.text((x1+4, y1+3), f["display_name"][:25], fill=(255, 255, 255))

        buf = _io.BytesIO()
        img.save(buf, "JPEG", quality=75)
        b64 = _b64.b64encode(buf.getvalue()).decode()
        return jsonify({"image_b64": b64, "width": w, "height": h})
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


@app.route("/api/artwork/profiles/render-blank", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_profile_render_blank():
    """Renderuje artworka bez nałożonych pól — do definiowania nowych regionów."""
    import base64 as _b64, io as _io
    from artwork_comparator import _load_pages, _PROFILE_RENDER_DPI

    file = request.files.get("file")
    if not file:
        return jsonify({"error": "Brak pliku PDF"}), 400

    uid = session["user_id"]
    import uuid as _uuid
    upload_dir = app.config["UPLOAD_FOLDER"]
    tmp_path = os.path.join(upload_dir, f"_profile_blank_{uid}_{_uuid.uuid4().hex}.pdf")
    try:
        file.save(tmp_path)
        pages = _load_pages(tmp_path, _PROFILE_RENDER_DPI, page_idx=None)
        if not pages:
            return jsonify({"error": "Nie można wyrenderować PDF"}), 400

        import io as _io2
        buf = _io2.BytesIO()
        pages[0].save(buf, "JPEG", quality=90)
        b64 = _b64.b64encode(buf.getvalue()).decode()
        w, h = pages[0].size
        return jsonify({"image_b64": b64, "width": w, "height": h})
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


@app.route("/api/artwork/profiles/<int:pid>/master", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_profile_attach_master(pid):
    """Save an uploaded PDF as the master for profile <pid>.

    Writes the file to MASTERS_DIR (persistent), updates master_pdf_path in the DB,
    and returns a rendered canvas image so the editor can display it immediately.
    Mapped fields are preserved — only the file path changes.
    """
    import hashlib as _hs, base64 as _b64, io as _io, re as _re
    from artwork_comparator import _load_pages, _PROFILE_RENDER_DPI

    file = request.files.get("file")
    if not file:
        return jsonify({"error": "Brak pliku PDF"}), 400

    db = get_db()
    try:
        row = db.execute("SELECT id, name FROM artwork_profiles WHERE id=?", (pid,)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono profilu"}), 404

        raw = file.read()
        h = _hs.md5(raw, usedforsecurity=False).hexdigest()[:12]
        safe = _re.sub(r"[^\w\-.]", "_", os.path.splitext(file.filename or row["name"])[0])[:40]
        pdf_path = os.path.join(MASTERS_DIR, f"{safe}_{h}.pdf")
        with open(pdf_path, "wb") as fout:
            fout.write(raw)

        # Render first page — used both for canvas response and thumbnail
        pages = _load_pages(pdf_path, _PROFILE_RENDER_DPI, page_idx=None)
        if not pages:
            try:
                os.remove(pdf_path)
            except OSError:
                pass
            return jsonify({"error": "Nie można wyrenderować PDF. Sprawdź czy plik nie jest uszkodzony."}), 400

        # Save thumbnail to DB so canvas can fall back when file is gone (e.g. container rebuild)
        thumb = _master_thumb(pages[0])
        db.execute(
            "UPDATE artwork_profiles SET master_pdf_path=?, thumb_b64=?, updated_at=datetime('now') WHERE id=?",
            (pdf_path, thumb, pid)
        )
        db.commit()

        buf = _io.BytesIO()
        pages[0].save(buf, "JPEG", quality=90)
        b64 = _b64.b64encode(buf.getvalue()).decode()
        w, h_px = pages[0].size
        return jsonify({"ok": True, "image_b64": b64, "width": w, "height": h_px, "path": pdf_path})
    finally:
        db.close()


# ─── ARTWORK FIELD DICT ──────────────────────────────────────────────────────

@app.route("/api/artwork/field-dict", methods=["GET"])
@require_role("manager")
def api_artwork_field_dict_list():
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        rows = db.execute(
            "SELECT id, display_name, default_severity, display_layout, default_rotation,"
            " comparison_mode, numeric_tolerance, notes FROM artwork_field_dict ORDER BY display_name"
        ).fetchall()
        # Also collect names from existing profile fields not yet in dict
        used = db.execute(
            "SELECT DISTINCT display_name FROM artwork_profile_fields ORDER BY display_name"
        ).fetchall()
        dict_names = {r["display_name"] for r in rows}
        extra = [{"id": None, "display_name": u["display_name"], "default_severity": "critical",
                  "display_layout": "side_by_side", "default_rotation": 0,
                  "comparison_mode": "text", "numeric_tolerance": 0.0,
                  "notes": "", "in_dict": False}
                 for u in used if u["display_name"] not in dict_names]
        result = [{"id": r["id"], "display_name": r["display_name"],
                   "default_severity": r["default_severity"],
                   "display_layout": r["display_layout"] or "side_by_side",
                   "default_rotation": int(r["default_rotation"] or 0),
                   "comparison_mode": r["comparison_mode"] or "text",
                   "numeric_tolerance": float(r["numeric_tolerance"] or 0.0),
                   "notes": r["notes"] or "", "in_dict": True} for r in rows]
        return jsonify({"items": result + extra})
    finally:
        db.close()


_VALID_DISPLAY_LAYOUTS = {"side_by_side", "stacked"}
_VALID_COMPARISON_MODES = {"text", "numeric", "graphic", "table", "translation"}


@app.route("/api/artwork/field-dict", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_field_dict_create():
    data = request.get_json(silent=True) or {}
    name = str(data.get("display_name") or "").strip()[:200]
    sev = data.get("default_severity", "critical")
    if sev not in _VALID_FIELD_SEVERITIES:
        sev = "critical"
    if not name:
        return jsonify({"error": "Brak nazwy"}), 400
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        layout = data.get("display_layout", "side_by_side")
        if layout not in _VALID_DISPLAY_LAYOUTS:
            layout = "side_by_side"
        try:
            rotation = int(data.get("default_rotation", 0) or 0) % 360
        except (TypeError, ValueError):
            rotation = 0
        cmode = data.get("comparison_mode", "text")
        if cmode not in _VALID_COMPARISON_MODES:
            cmode = "text"
        try:
            ntol = float(data.get("numeric_tolerance", 0.0) or 0.0)
        except (TypeError, ValueError):
            ntol = 0.0
        if not (0.0 <= ntol <= 1000.0):
            ntol = 0.0
        db.execute(
            "INSERT INTO artwork_field_dict (display_name, default_severity, display_layout,"
            " default_rotation, comparison_mode, numeric_tolerance) VALUES (?, ?, ?, ?, ?, ?)",
            (name, sev, layout, rotation, cmode, ntol)
        )
        db.commit()
        row = db.execute("SELECT * FROM artwork_field_dict WHERE display_name=?", (name,)).fetchone()
        return jsonify({"id": row["id"], "display_name": row["display_name"],
                        "default_severity": row["default_severity"],
                        "display_layout": row["display_layout"] or "side_by_side",
                        "default_rotation": int(row["default_rotation"] or 0),
                        "comparison_mode": row["comparison_mode"] or "text",
                        "numeric_tolerance": float(row["numeric_tolerance"] or 0.0)})
    except Exception as e:
        logger.exception("artwork_field_dict create error: %s", e)
        return jsonify({"error": "Błąd zapisu — sprawdź czy nazwa jest unikalna."}), 400
    finally:
        db.close()


@app.route("/api/artwork/field-dict/<int:fid>", methods=["PUT"])
@require_role("manager")
@csrf_protect
def api_artwork_field_dict_update(fid):
    data = request.get_json(silent=True) or {}
    name = str(data.get("display_name") or "").strip()[:200]
    sev = data.get("default_severity", "critical")
    if sev not in _VALID_FIELD_SEVERITIES:
        sev = "critical"
    if not name:
        return jsonify({"error": "Brak nazwy"}), 400
    db = get_db()
    try:
        old = db.execute("SELECT display_name FROM artwork_field_dict WHERE id=?", (fid,)).fetchone()
        if not old:
            return jsonify({"error": "Nie znaleziono"}), 404
        layout = data.get("display_layout", "side_by_side")
        if layout not in _VALID_DISPLAY_LAYOUTS:
            layout = "side_by_side"
        try:
            rotation = int(data.get("default_rotation", 0) or 0) % 360
        except (ValueError, TypeError):
            rotation = 0
        cmode = data.get("comparison_mode", "text")
        if cmode not in _VALID_COMPARISON_MODES:
            cmode = "text"
        try:
            ntol = float(data.get("numeric_tolerance", 0.0) or 0.0)
        except (ValueError, TypeError):
            ntol = 0.0
        if not (0.0 <= ntol <= 1000.0):
            ntol = 0.0
        db.execute(
            "UPDATE artwork_field_dict SET display_name=?, default_severity=?, display_layout=?,"
            " default_rotation=?, comparison_mode=?, numeric_tolerance=? WHERE id=?",
            (name, sev, layout, rotation, cmode, ntol, fid)
        )
        if old["display_name"] != name:
            db.execute(
                "UPDATE artwork_profile_fields SET display_name=? WHERE display_name=?",
                (name, old["display_name"])
            )
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/artwork/field-dict/<int:fid>", methods=["DELETE"])
@require_role("manager")
@csrf_protect
def api_artwork_field_dict_delete(fid):
    db = get_db()
    try:
        db.execute("DELETE FROM artwork_field_dict WHERE id=?", (fid,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/artwork/field-dict/set-rotation", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_field_dict_set_rotation():
    """Upsert default_rotation for a field's display_name. Lets users 'teach'
    the system the correct orientation once during mapping — next time the
    same field type is loaded, the rotation is applied automatically."""
    data = request.get_json(silent=True) or {}
    name = str(data.get("display_name") or "").strip()
    if not name:
        return jsonify({"error": "display_name wymagane"}), 400
    try:
        rot = int(data.get("rotation", 0)) % 360
    except (TypeError, ValueError):
        return jsonify({"error": "Nieprawidłowa rotacja"}), 400
    if rot not in (0, 90, 180, 270):
        return jsonify({"error": "Rotacja musi być 0/90/180/270"}), 400
    db = get_db()
    try:
        existing = db.execute(
            "SELECT id FROM artwork_field_dict WHERE display_name=?", (name,)
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE artwork_field_dict SET default_rotation=? WHERE id=?",
                (rot, existing["id"]),
            )
        else:
            db.execute(
                "INSERT INTO artwork_field_dict (display_name, default_severity, display_layout, default_rotation, comparison_mode, numeric_tolerance) VALUES (?, ?, ?, ?, ?, ?)",
                (name, "critical", "side_by_side", rot, "text", 0.0),
            )
        db.commit()
        return jsonify({"ok": True, "display_name": name, "rotation": rot})
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════════════
# ARTWORK FIELD FEEDBACK / LEARNING
# ═══════════════════════════════════════════════════════════════════════════

def _load_artwork_field_stats(db) -> dict:
    """Return {field_name: {fpr, reviews, fp_count}} from feedback table.
    Only includes fields with ≥5 operator verdicts (minimum for reliable stats).
    fpr = false_positive_rate = verdicts where was_changed=1 but operator said OK.
    """
    rows = db.execute("""
        SELECT field_name,
               COUNT(*) as total,
               SUM(CASE WHEN was_changed=1 THEN 1 ELSE 0 END) as flagged,
               SUM(CASE WHEN was_changed=1 AND operator_verdict='false_positive' THEN 1 ELSE 0 END) as fp
        FROM artwork_field_feedback
        WHERE operator_verdict IS NOT NULL
        GROUP BY field_name
        HAVING COUNT(*) >= 5
    """).fetchall()
    stats = {}
    for r in rows:
        flagged = int(r["flagged"] or 0)
        fp = int(r["fp"] or 0)
        fpr = (fp / flagged) if flagged > 0 else 0.0
        stats[r["field_name"]] = {
            "fpr": round(fpr, 3),
            "reviews": int(r["total"]),
            "fp_count": fp,
            "flagged": flagged,
        }
    return stats


@app.route("/api/artwork/field-feedback", methods=["POST"])
@require_role("user")
@csrf_protect
def api_artwork_field_feedback_create():
    data = request.get_json(silent=True) or {}
    field_name = str(data.get("field_name") or "").strip()
    verdict = data.get("operator_verdict")
    if not field_name or verdict not in ("false_positive", "confirmed_error"):
        return jsonify({"error": "Wymagane: field_name, operator_verdict in (false_positive, confirmed_error)"}), 400
    _cmode = str(data.get("comparison_mode") or "text")
    if _cmode not in ("text", "numeric", "graphic", "table", "translation"):
        _cmode = "text"
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        db.execute(
            "INSERT INTO artwork_field_feedback "
            "(field_name, display_name, profile_name, queue_id, comparison_mode, val_a, val_b, was_changed, operator_verdict) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (field_name[:200],
             str(data.get("display_name", field_name))[:200],
             str(data.get("profile_name", ""))[:200],
             str(data.get("queue_id", ""))[:100],
             _cmode,
             str(data.get("val_a", ""))[:200],
             str(data.get("val_b", ""))[:200],
             1 if data.get("was_changed") is True else 0,
             verdict)
        )
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/artwork/field-stats", methods=["GET"])
@require_role("manager")
def api_artwork_field_stats():
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        stats = _load_artwork_field_stats(db)
        # Also include all-feedback counts (before min-5 filter)
        all_rows = db.execute("""
            SELECT field_name, display_name,
                   COUNT(*) as total,
                   SUM(CASE WHEN operator_verdict='false_positive' THEN 1 ELSE 0 END) as fp,
                   SUM(CASE WHEN operator_verdict='confirmed_error' THEN 1 ELSE 0 END) as tp,
                   MAX(created_at) as last_at
            FROM artwork_field_feedback
            WHERE operator_verdict IS NOT NULL
            GROUP BY field_name
            ORDER BY total DESC
        """).fetchall()
        result = []
        for r in all_rows:
            fname = r["field_name"]
            s = stats.get(fname, {})
            result.append({
                "field_name": fname,
                "display_name": r["display_name"],
                "total": r["total"],
                "fp_count": r["fp"],
                "tp_count": r["tp"],
                "fpr": s.get("fpr"),
                "last_at": r["last_at"],
                "adjustment": (
                    "pix_threshold_96 + sev-2" if s.get("fpr", 0) >= 0.90 else
                    "pix_threshold_93 + sev-1" if s.get("fpr", 0) >= 0.80 else
                    "ostrzeżenie" if s.get("fpr", 0) >= 0.70 else
                    "brak"
                ),
            })
        return jsonify({"stats": result})
    finally:
        db.close()


@app.route("/api/artwork/field-stats/reset/<field_name>", methods=["DELETE"])
@require_role("manager")
@csrf_protect
def api_artwork_field_stats_reset(field_name):
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        db.execute("DELETE FROM artwork_field_feedback WHERE field_name=?", (field_name,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════════════
# ARTWORK MATERIAL MASTER DATA
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/artwork/materials")
@require_role("manager")
def page_artwork_materials():
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
    finally:
        db.close()
    return render_template("artwork_materials.html")


@app.route("/api/artwork/pkg-level-types", methods=["GET"])
@require_role("manager")
def api_pkg_level_types_list():
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        rows = db.execute(
            "SELECT id, code, label, sort_order, color FROM artwork_pkg_level_type ORDER BY sort_order"
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()


@app.route("/api/artwork/pkg-level-types", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_pkg_level_types_create():
    data = request.get_json(silent=True) or {}
    code = str(data.get("code") or "").strip().lower().replace(" ", "_")[:50]
    label = str(data.get("label") or "").strip()[:100]
    if not code or not label:
        return jsonify({"error": "Wymagane: code i label"}), 400
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        max_order = db.execute("SELECT MAX(sort_order) as m FROM artwork_pkg_level_type").fetchone()["m"] or 0
        db.execute(
            "INSERT INTO artwork_pkg_level_type (code, label, sort_order, color) VALUES (?, ?, ?, ?)",
            (code, label, max_order + 1, str(data.get("color") or "#6b7280")[:50])
        )
        db.commit()
        row = db.execute("SELECT * FROM artwork_pkg_level_type WHERE code=?", (code,)).fetchone()
        return jsonify(dict(row)), 201
    except Exception as e:
        logger.exception("pkg_level_type create error: %s", e)
        return jsonify({"error": "Błąd zapisu — kod musi być unikalny."}), 400
    finally:
        db.close()


@app.route("/api/artwork/pkg-level-types/<int:ltid>", methods=["PUT"])
@require_role("manager")
@csrf_protect
def api_pkg_level_types_update(ltid):
    data = request.get_json(silent=True) or {}
    try:
        sort_order = int(data.get("sort_order", 0))
    except (ValueError, TypeError):
        sort_order = 0
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        db.execute(
            "UPDATE artwork_pkg_level_type SET label=?, color=?, sort_order=? WHERE id=?",
            (str(data.get("label") or "")[:100], str(data.get("color") or "#6b7280")[:50], sort_order, ltid)
        )
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/artwork/pkg-level-types/<int:ltid>", methods=["DELETE"])
@require_role("manager")
@csrf_protect
def api_pkg_level_types_delete(ltid):
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        db.execute("DELETE FROM artwork_material_level WHERE level_type_id=?", (ltid,))
        db.execute("DELETE FROM artwork_pkg_level_type WHERE id=?", (ltid,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/materials", methods=["GET"])
@login_required
def api_materials_list():
    """Tabela materiałów master z filtrami (q, rodzina, producent) + paginacja."""
    import material_master as _mm
    q = request.args.get("q", "").strip()
    rodzina = request.args.get("rodzina", "").strip()
    producent = request.args.get("producent", "").strip()
    before = request.args.get("before", "").strip()   # migracja starsza niż (YYYY-MM-DD)
    after = request.args.get("after", "").strip()      # migracja od (YYYY-MM-DD)
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    # limit<=0 → wszystko (z bezpiecznym sufitem, by filtry działały na całym zakresie)
    if limit <= 0 or limit > 20000:
        limit = 20000
    _esc = lambda s: s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    db = get_db()
    try:
        _mm.ensure_table(db)
        where = ["active=1"]
        params = []
        if q:
            like = f"%{_esc(q)}%"
            where.append("(ref_code LIKE ? ESCAPE '\\' OR opis_pl LIKE ? ESCAPE '\\' "
                         "OR opis_en LIKE ? ESCAPE '\\' OR ean LIKE ? ESCAPE '\\')")
            params += [like, like, like, like]
        if rodzina:
            where.append("rodzina = ?")
            params.append(rodzina)
        if producent:
            where.append("producer_code LIKE ? ESCAPE '\\'")
            params.append(f"%{_esc(producent)}%")
        if before:
            where.append("updated_at < ?")
            params.append(before)
        if after:
            where.append("updated_at >= ?")
            params.append(after)
        wsql = " AND ".join(where)
        rows = db.execute(
            # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
            "SELECT ref_code, opis_pl, opis_en, ean, rodzina, base_uom, producer_code, "  # nosec B608
            f"tariff_cn, customs_code, vat_rate, sent, supplier_codes, txt_short_pl, levels_json, updated_at "
            f"FROM material_master WHERE {wsql} "
            "ORDER BY ref_code LIMIT ? OFFSET ?", params + [limit, offset]
        ).fetchall()
        # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
        total = db.execute(f"SELECT COUNT(*) FROM material_master WHERE {wsql}", params).fetchone()[0]  # nosec B608
        families = [r[0] for r in db.execute(
            "SELECT DISTINCT rodzina FROM material_master WHERE active=1 AND rodzina<>'' "
            "ORDER BY rodzina LIMIT 400").fetchall()]
    finally:
        db.close()
    return jsonify({"items": [dict(r) for r in rows], "total": total,
                    "offset": offset, "families": families})


@app.route("/api/materials/<path:ref>", methods=["DELETE"])
@require_role("manager")
@csrf_protect
def api_materials_delete(ref):
    """Usuwa pojedynczy rekord materiału (po REF)."""
    import material_master as _mm
    db = get_db()
    try:
        _mm.ensure_table(db)
        cur = db.execute("DELETE FROM material_master WHERE ref_code=?", (str(ref),))
        db.commit()
        _log_audit("material_delete", session.get("username"), str(ref))
        return jsonify({"ok": True, "deleted": cur.rowcount})
    finally:
        db.close()


@app.route("/api/materials/<path:ref>", methods=["PATCH"])
@require_role("manager")
@csrf_protect
def api_materials_update(ref):
    """Edycja inline pól materiału master: stawka VAT i flaga SENT."""
    import material_master as _mm
    data = request.get_json(silent=True) or {}
    db = get_db()
    try:
        _mm.ensure_table(db)
        sets, params = [], []
        if "vat_rate" in data:
            sets.append("vat_rate=?"); params.append(_mm.norm_vat(data.get("vat_rate")))
        if "sent" in data:
            sets.append("sent=?"); params.append(_mm.sent_truthy(data.get("sent")))
        if not sets:
            return jsonify({"error": "Brak pól do zapisu (vat_rate / sent)"}), 400
        sets.append("updated_at=datetime('now')")
        set_sql = ", ".join(sets)
        # Bandit B608: nazwy kolumn z twardej białej listy w kodzie; wartości jako parametry ?.
        cur = db.execute(f"UPDATE material_master SET {set_sql} WHERE ref_code=?",  # nosec B608
                         params + [str(ref)])
        db.commit()
        if cur.rowcount == 0:
            # Bandit B608: nazwy kolumn z twardej białej listy w kodzie; wartości jako parametry ?.
            db.execute(f"UPDATE material_master SET {set_sql} WHERE ref_norm=?",  # nosec B608
                       params + [_mm.normalize_ref(ref)])
            db.commit()
        _log_audit("material_update", session.get("username"),
                   f"{ref}: " + ", ".join(f"{k}={data[k]}" for k in ("vat_rate", "sent") if k in data))
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/materials/delete-old", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_materials_delete_old():
    """Usuwa hurtowo rekordy starsze niż podana data migracji (updated_at < before).
    Służy do czyszczenia po wgraniu nowej bazy (stare, niezaktualizowane rekordy)."""
    import material_master as _mm
    data = request.get_json(silent=True) or {}
    before = str(data.get("before") or "").strip()
    if not before:
        return jsonify({"error": "Podaj datę graniczną (before)"}), 400
    db = get_db()
    try:
        _mm.ensure_table(db)
        cur = db.execute("DELETE FROM material_master WHERE updated_at < ?", (before,))
        db.commit()
        _log_audit("material_delete_old", session.get("username"), f"before={before} n={cur.rowcount}")
        return jsonify({"ok": True, "deleted": cur.rowcount})
    finally:
        db.close()


@app.route("/api/materials/<path:ref>", methods=["GET"])
@login_required
def api_material_detail(ref):
    """Szczegóły materiału: REF, opis, producent, baza JM, poziomy + przeliczniki UOM."""
    import material_master as _mm
    db = get_db()
    try:
        mat = _mm.get_material(db, ref)
        if not mat:
            return jsonify({"error": "Nie znaleziono materiału"}), 404
        try:
            levels = json.loads(mat.get("levels_json") or "{}")
        except (ValueError, TypeError):
            levels = {}
        conv = []
        try:
            import uom as _uom
            _uom.ensure_table(db)
            rows = db.execute(
                "SELECT unit_from, unit_to, factor FROM uom_conversion WHERE ref_norm=? "
                "ORDER BY factor", (mat.get("ref_norm") or "",)
            ).fetchall()
            conv = [dict(r) for r in rows]
        except Exception:
            conv = []
    finally:
        db.close()
    return jsonify({
        "ref_code": mat.get("ref_code"), "opis_pl": mat.get("opis_pl"),
        "opis_en": mat.get("opis_en"), "ean": mat.get("ean"),
        "rodzina": mat.get("rodzina"), "base_uom": mat.get("base_uom"),
        "producer_code": mat.get("producer_code"),
        "tariff_cn": mat.get("tariff_cn"), "customs_code": mat.get("customs_code"),
        "supplier_codes": mat.get("supplier_codes"),
        "txt_short_pl": mat.get("txt_short_pl"),
        "levels": levels, "conversions": conv,
    })


@app.route("/api/materials/migrate-from-products", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_materials_migrate_products():
    """Wchłania tabelę products do material_master (scalenie rejestrów)."""
    import material_master as _mm
    db = get_db()
    try:
        res = _mm.migrate_from_products(db)
    finally:
        db.close()
    _log_audit("materials_migrate_products", session.get("username"),
               f"migrated={res.get('migrated', 0)}")
    return jsonify({"ok": "error" not in res, **res})


@app.route("/materials")
@login_required
def page_materials():
    return render_template("materials.html",
                           username=session.get("username"), role=session.get("role"))


# ── Master data dostawców (rozszerza tabelę suppliers) ─────────────────────────
@app.route("/suppliers/master")
@login_required
def page_suppliers_master():
    return render_template("suppliers_master.html",
                           username=session.get("username"), role=session.get("role"))


@app.route("/api/sup-master", methods=["GET"])
@login_required
def api_sup_master_list():
    import supplier_master as _sm
    db = get_db()
    try:
        items = _sm.list_suppliers(db, request.args.get("q", "").strip())
    finally:
        db.close()
    return jsonify({"items": items, "total": len(items)})


@app.route("/api/sup-master/<path:code>", methods=["GET"])
@login_required
def api_sup_master_detail(code):
    import supplier_master as _sm
    db = get_db()
    try:
        s = _sm.get_supplier(db, code)
    finally:
        db.close()
    if not s:
        return jsonify({"error": "Nie znaleziono dostawcy"}), 404
    try:
        kw = json.loads(s.get("detect_keywords_json") or "[]")
    except (ValueError, TypeError):
        kw = []
    return jsonify({
        "code": s.get("code"), "name": s.get("name"),
        "producer_code": s.get("producer_code"), "country": s.get("country"),
        "currency": s.get("currency"), "incoterms": s.get("incoterms"),
        "payment_terms_default": s.get("payment_terms_default"),
        "lead_time_days": s.get("lead_time_days"),
        "contact_person": s.get("contact_person"), "email": s.get("email"),
        "phone": s.get("phone"), "notes": s.get("notes"), "keywords": kw,
    })


@app.route("/api/sup-master/<path:code>/contact", methods=["PATCH"])
@require_role("manager")
@csrf_protect
def api_sup_master_update_contact(code):
    """Ustaw/edytuj dane kontaktowe dostawcy (e-mail odbiorcy zamówień, osoba, tel.)."""
    import supplier_master as _sm
    data = request.get_json(silent=True) or {}
    email = data.get("email")
    if email is not None:
        email = str(email).strip()
        if email and ("@" not in email or "." not in email.split("@")[-1]):
            return jsonify({"error": "Niepoprawny adres e-mail"}), 400
    db = get_db()
    try:
        res = _sm.update_contact(
            db, code, email=email,
            contact_person=data.get("contact_person"), phone=data.get("phone"))
    finally:
        db.close()
    if "error" in res:
        return jsonify(res), 400
    _log_audit("supplier_contact_update", session.get("username"),
               f"{code} email={email}")
    return jsonify(res)


@app.route("/api/sup-master/import", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_sup_master_import():
    import supplier_master as _sm
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "Brak pliku"}), 400
    fname = f.filename.lower()
    raw_rows = []
    try:
        if fname.endswith(".xlsx"):
            from openpyxl import load_workbook
            wb = load_workbook(f, read_only=True, data_only=True)
            ws = wb.active
            for r in ws.iter_rows(values_only=True):
                raw_rows.append(list(r))
                if len(raw_rows) > 20000:
                    break
        elif fname.endswith(".csv"):
            import csv as _csv, io as _io
            raw = f.read().decode("utf-8-sig", errors="replace")
            # Wykryj separator (polski Excel zapisuje CSV ze średnikiem).
            _sample = raw[:8192]
            _delim = max((",", ";", "\t"), key=lambda d: _sample.count(d))
            raw_rows = [r for r in _csv.reader(_io.StringIO(raw), delimiter=_delim)]
        else:
            return jsonify({"error": "Obsługiwane formaty: .xlsx, .csv"}), 400
    except Exception as _e:
        logger.warning("supplier master import parse error: %s", _e)
        return jsonify({"error": "Nie udało się odczytać pliku"}), 400
    db = get_db()
    try:
        res = _sm.import_workbook(db, raw_rows)
    finally:
        db.close()
    _log_audit("sup_master_import", session.get("username"), f"imported={res['imported']}")
    return jsonify({"ok": True, **res})


@app.route("/api/materials/import", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_materials_import():
    """Import master daty materiałów z CSV/XLSX. Format tolerancyjny: nagłówek
    1- lub 2-wierszowy (banner poziomów + nazwy pól), przeliczniki PAZ/PPA i
    podstawowa JM trafiają też do uom_conversion."""
    import material_master as _mm
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "Brak pliku"}), 400
    fname = f.filename.lower()
    raw_rows = []   # lista wierszy jako listy komórek (surowo)
    try:
        if fname.endswith(".xlsx"):
            from openpyxl import load_workbook
            wb = load_workbook(f, read_only=True, data_only=True)
            ws = wb.active
            for r in ws.iter_rows(values_only=True):
                raw_rows.append(list(r))
                if len(raw_rows) > 60000:
                    break
        elif fname.endswith(".csv"):
            import csv as _csv, io as _io
            raw = f.read().decode("utf-8-sig", errors="replace")
            # Wykryj separator (polski Excel zapisuje CSV ze średnikiem).
            _sample = raw[:8192]
            _delim = max((",", ";", "\t"), key=lambda d: _sample.count(d))
            raw_rows = [r for r in _csv.reader(_io.StringIO(raw), delimiter=_delim)]
        else:
            return jsonify({"error": "Obsługiwane formaty: .xlsx, .csv"}), 400
    except Exception as _e:
        logger.warning("materials import parse error: %s", _e)
        return jsonify({"error": "Nie udało się odczytać pliku"}), 400
    if len(raw_rows) > 55000:
        return jsonify({"error": "Za dużo wierszy (max ~55000)"}), 400
    db = get_db()
    try:
        res = _mm.import_workbook(db, raw_rows)
    finally:
        db.close()
    _log_audit("materials_import", session.get("username"),
               f"imported={res['imported']} conversions={res.get('conversions', 0)}")
    return jsonify({"ok": True, **res})


@app.route("/api/materials/marm-import", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_materials_marm_import():
    """Import eksportu SAP MARM (wymiary opakowań per REF+jednostka) z XLSX/CSV.
    Zasila material_uom_dims → dane dla generatora 3D z MARM."""
    import material_uom as _mu
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "Brak pliku"}), 400
    fname = f.filename.lower()
    raw_rows = []
    try:
        if fname.endswith(".xlsx"):
            from openpyxl import load_workbook
            wb = load_workbook(f, read_only=True, data_only=True)
            ws = wb.active
            for r in ws.iter_rows(values_only=True):
                raw_rows.append(list(r))
                if len(raw_rows) > 60000:
                    break
        elif fname.endswith(".csv"):
            import csv as _csv, io as _io
            raw = f.read().decode("utf-8-sig", errors="replace")
            _sample = raw[:8192]
            _delim = max((",", ";", "\t"), key=lambda d: _sample.count(d))
            raw_rows = [r for r in _csv.reader(_io.StringIO(raw), delimiter=_delim)]
        else:
            return jsonify({"error": "Obsługiwane formaty: .xlsx, .csv"}), 400
    except Exception as _e:
        logger.warning("MARM import parse error: %s", _e)
        return jsonify({"error": "Nie udało się odczytać pliku"}), 400
    if len(raw_rows) > 60000:
        return jsonify({"error": "Za dużo wierszy (max ~60000)"}), 400
    db = get_db()
    try:
        res = _mu.import_marm_rows(db, raw_rows)
    except ValueError as _e:
        return jsonify({"error": str(_e)}), 400
    finally:
        db.close()
    _log_audit("marm_import", session.get("username"),
               f"imported={res['imported']} refs={res['refs']} skipped={res['skipped']}")
    return jsonify({"ok": True, **res})


@app.route("/api/materials/marm/search")
@login_required
def api_materials_marm_search():
    """Podpowiedzi REF z danych MARM (tylko REF-y z renderowalnymi wymiarami)."""
    import material_uom as _mu
    q = (request.args.get("q") or "").strip()
    db = get_db()
    try:
        return jsonify(_mu.search_refs(db, q))
    finally:
        db.close()


@app.route("/api/materials/marm/suggest")
@login_required
def api_materials_marm_suggest():
    """MARM-SUGG-01: rankingowane sugestie powiązania MARM dla REF artworku
    (exact→fuzzy, tylko REF-y z renderowalnymi jednostkami)."""
    import material_uom as _mu
    ref = (request.args.get("ref") or "").strip()
    db = get_db()
    try:
        return jsonify({"suggestions": _mu.suggest_refs(db, ref)})
    finally:
        db.close()


@app.route("/api/materials/marm/<path:ref>/units")
@login_required
def api_materials_marm_units(ref):
    """Jednostki danego REF z wymiarami w mm (KAR/OP/OPZ/SZT/…), gotowe do renderu."""
    import material_uom as _mu
    db = get_db()
    try:
        return jsonify({"ref": ref, "units": _mu.get_ref_units(db, ref)})
    finally:
        db.close()


@app.route("/api/artwork/index/sync", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_index_sync():
    """Odbiera paczkę wpisów indeksu masterów z agenta ACME (skan Z:\\). #5"""
    import artwork_index as _ai
    data = request.get_json(silent=True) or {}
    entries = data.get("entries") or []
    if not isinstance(entries, list):
        return jsonify({"error": "entries musi być listą"}), 400
    if len(entries) > 5000:
        return jsonify({"error": "Za duża paczka (max 5000)"}), 400
    db = get_db()
    try:
        res = _ai.sync_entries(db, entries)
    finally:
        db.close()
    return jsonify({"ok": True, **res})


@app.route("/api/artwork/index/status", methods=["GET"])
@login_required
def api_artwork_index_status():
    """Stan indeksu masterów: ile plików, ile z REF, ostatni skan."""
    import artwork_index as _ai
    db = get_db()
    try:
        return jsonify(_ai.index_stats(db))
    finally:
        db.close()


@app.route("/api/artwork/master-files/wanted", methods=["GET"])
@require_role("manager")
def api_artwork_master_files_wanted():
    """rel_path masterów potrzebnych do podglądu (potwierdzone/grupowe), których chmura
    jeszcze nie ma. Agent skanujący Z:\\ dosyła te pliki (POST .../upload)."""
    import artwork_index as _ai
    db = get_db()
    try:
        return jsonify({"wanted": _ai.wanted_master_paths(db)})
    finally:
        db.close()


@app.route("/api/artwork/master-files/upload", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_master_file_upload():
    """Agent wgrywa bajty mastera (multipart: rel_path + file)."""
    import artwork_index as _ai, hashlib as _hl
    rel_path = (request.form.get("rel_path") or "").strip()
    f = request.files.get("file")
    if not rel_path or not f or not f.filename:
        return jsonify({"error": "Wymagane: rel_path + file"}), 400
    folder = os.path.join(app.config["UPLOAD_FOLDER"], "artwork_masters")
    os.makedirs(folder, exist_ok=True)
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"):
        ext = ".pdf"
    stored = _hl.sha1(rel_path.encode("utf-8"), usedforsecurity=False).hexdigest() + ext
    dest = os.path.join(folder, stored)
    # Enforce a size cap before reading the whole file into memory / the DB.
    _clen = request.content_length
    if _clen and _clen > MAX_UPLOAD_BYTES:
        return jsonify({"error": f"Plik jest zbyt duży. Maksymalny rozmiar to {MAX_UPLOAD_MB} MB."}), 413
    blob = f.read(MAX_UPLOAD_BYTES + 1)   # bajty trafiają DO BAZY (trwałe)
    if not blob:
        return jsonify({"error": "Pusty plik"}), 400
    if len(blob) > MAX_UPLOAD_BYTES:
        return jsonify({"error": f"Plik jest zbyt duży. Maksymalny rozmiar to {MAX_UPLOAD_MB} MB."}), 413
    try:
        with open(dest, "wb") as _fp:     # + cache na dysku (przyspiesza serwowanie)
            _fp.write(blob)
    except OSError:
        pass
    size = len(blob)
    ctype = f.mimetype or ("application/pdf" if ext == ".pdf" else "image/" + ext.lstrip("."))
    db = get_db()
    try:
        _ai.record_master_file(db, rel_path, stored, ctype, size, data=blob)
        # Zaciągnij też do widoku mapowania (Profile artwork) jako „do zmapowania".
        try:
            _ensure_master_profile(db, dest, f.filename or os.path.basename(rel_path), rel_path)
        except Exception as _pe:
            logger.debug("ensure master profile (upload) rel=%s: %s", rel_path, _pe)
    finally:
        db.close()
    return jsonify({"ok": True, "size": size})


@app.route("/api/artwork/master-file/view", methods=["GET"])
@login_required
def api_artwork_master_file_view():
    """Podgląd mastera (inline) — serwuje bajty wgrane przez agenta."""
    import artwork_index as _ai
    from flask import abort as _abort
    rel_path = (request.args.get("rel_path") or "").strip()
    if not rel_path:
        _abort(404)
    db = get_db()
    try:
        rec = _ai.master_file_for(db, rel_path)
        if not rec:
            _abort(404)
        path = os.path.join(app.config["UPLOAD_FOLDER"], "artwork_masters", rec["stored_name"])
        if os.path.isfile(path):
            return send_file(path, mimetype=rec.get("content_type") or "application/pdf",
                             as_attachment=False, download_name=os.path.basename(rel_path))
        # Cache na dysku zniknął (np. po redeployu) → odtwórz z BAZY i zapisz cache.
        blob = _ai.master_file_blob(db, rel_path)
        if not blob:
            _abort(404)
        ctype, data = blob
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as _fp:
                _fp.write(data)
        except OSError:
            pass
    finally:
        db.close()
    import io as _io
    return send_file(_io.BytesIO(data), mimetype=ctype or "application/pdf",
                     as_attachment=False, download_name=os.path.basename(rel_path))


@app.route("/api/artwork/master-files/from-path", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_master_file_from_path():
    """Jednym kliknięciem pobiera plik mastera ze znanej ścieżki PO STRONIE SERWERA
    (biblioteka/indeks zsynchr. przez agenta) i zapisuje do składnicy podglądu — bez
    ręcznego wgrywania. Działa, gdy plik jest już dostępny na serwerze; inaczej 409."""
    import artwork_index as _ai, hashlib as _hl
    data = request.get_json(silent=True) or {}
    rel_path = (data.get("rel_path") or "").strip()
    filename = (data.get("filename") or "").strip()
    if not rel_path:
        return jsonify({"error": "Brak ścieżki"}), 400
    db = get_db()
    try:
        src = _resolve_artwork_master_path(db, rel_path, filename)
        if not src or not os.path.exists(src):
            return jsonify({"error": "Plik nie jest jeszcze dostępny na serwerze — użyj "
                            "„Wgraj ręcznie” albo poczekaj, aż agent skanujący Z:\\ go prześle.",
                            "available": False}), 409
        folder = os.path.join(app.config["UPLOAD_FOLDER"], "artwork_masters")
        os.makedirs(folder, exist_ok=True)
        ext = os.path.splitext(src)[1].lower()
        if ext not in (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"):
            ext = ".pdf"
        stored = _hl.sha1(rel_path.encode("utf-8"), usedforsecurity=False).hexdigest() + ext
        dest = os.path.join(folder, stored)
        with open(src, "rb") as _fp:      # bajty DO BAZY (trwałe)
            blob = _fp.read()
        if not blob:
            return jsonify({"error": "Plik na serwerze jest pusty", "available": False}), 409
        if os.path.abspath(src) != os.path.abspath(dest):
            try:
                with open(dest, "wb") as _out:
                    _out.write(blob)
            except OSError:
                pass
        ctype = "application/pdf" if ext == ".pdf" else "image/" + ext.lstrip(".")
        _ai.record_master_file(db, rel_path, stored, ctype, len(blob), data=blob)
        try:
            _ensure_master_profile(db, dest, filename or os.path.basename(rel_path), rel_path)
        except Exception as _pe:
            logger.debug("ensure master profile (from-path) rel=%s: %s", rel_path, _pe)
    finally:
        db.close()
    return jsonify({"ok": True})


@app.route("/api/artwork/profiles/to-map", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_profile_to_map():
    """Tworzy w Profilach artworków wpis „do zmapowania" (0 pól) dla niezmapowanego
    REF — żeby od razu pojawił się w /artwork/profiles do uzupełnienia mapowania."""
    data = request.get_json(silent=True) or {}
    ref = (data.get("ref") or "").strip()
    ean = (data.get("ean") or "").strip()
    if not ref:
        return jsonify({"error": "Brak REF"}), 400
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        ex = db.execute(
            "SELECT id FROM artwork_profiles WHERE ref_code=? LIMIT 1", (ref,)).fetchone()
        if ex:
            return jsonify({"ok": True, "existed": True})
        db.execute(
            "INSERT INTO artwork_profiles (name, ean, ref_code, master_pdf_path, thumb_b64, "
            "folder_path, packaging_type, revision, revision_rank, source_path, is_active, created_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,1,?)",
            (ref, ean, ref, "", "", "", "", "", 0, "", session.get("user_id")))
        db.commit()
        _log_audit("artwork_profile_to_map", session.get("username"), f"ref={ref}")
    finally:
        db.close()
    return jsonify({"ok": True})


@app.route("/api/artwork/index/import", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_index_import():
    """Zasila indeks z LISTY ŚCIEŻEK (np. `dir /s /b "Z:\\...\\*.pdf" > lista.txt`)
    albo z kolumny ścieżek w CSV/XLSX. Wysyłamy tylko metadane (nazwa+ścieżka)."""
    import artwork_index as _ai
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "Brak pliku"}), 400
    fname = f.filename.lower()
    rows = []   # wiersze tabeli (listy komórek) — kolumnowy CSV/XLSX albo 1 ścieżka/linię
    try:
        if fname.endswith(".xlsx"):
            from openpyxl import load_workbook
            wb = load_workbook(f, read_only=True, data_only=True)
            for r in wb.active.iter_rows(values_only=True):
                rows.append(list(r))
                if len(rows) > 200000:
                    break
        else:  # .txt / .csv / dowolny tekst
            raw_bytes = f.read()
            # Wykryj kodowanie — Windows `dir > plik.txt` daje UTF-16 (PowerShell)
            # albo polską stronę kodową (cmd: cp1250/cp852); naiwny UTF-8 gubił „.pdf".
            if raw_bytes[:2] in (b"\xff\xfe", b"\xfe\xff"):
                raw = raw_bytes.decode("utf-16", errors="replace")
            elif raw_bytes[:3] == b"\xef\xbb\xbf":
                raw = raw_bytes.decode("utf-8-sig", errors="replace")
            else:
                raw = None
                for _enc in ("utf-8", "cp1250", "cp852", "latin-1"):
                    try:
                        raw = raw_bytes.decode(_enc)
                        break
                    except UnicodeDecodeError:
                        continue
                if raw is None:
                    raw = raw_bytes.decode("utf-8", errors="replace")
            import csv as _csv, io as _io
            if fname.endswith(".csv"):
                # Wykryj separator (polski/PowerShell eksport używa ';').
                _sample = raw[:8192]
                _delim = max((";", ",", "\t"), key=lambda d: _sample.count(d))
                rows = [r for r in _csv.reader(_io.StringIO(raw), delimiter=_delim)]
            else:
                rows = [[ln] for ln in raw.splitlines()]
    except Exception as _e:
        logger.warning("artwork index import parse error: %s", _e)
        return jsonify({"error": "Nie udało się odczytać pliku"}), 400
    if len(rows) > 200000:
        return jsonify({"error": "Za dużo wierszy (max ~200000)"}), 400
    db = get_db()
    try:
        res = _ai.import_listing(db, rows)
    finally:
        db.close()
    _log_audit("artwork_index_import", session.get("username"),
               f"indexed={res.get('indexed')} candidates={res.get('candidates')}")
    return jsonify({"ok": True, **res})


@app.route("/artwork/index")
@login_required
def artwork_index_page():
    return render_template("artwork_index.html",
                           username=session["username"], role=session["role"])


@app.route("/artwork/card/<ref>")
@login_required
def artwork_card_page(ref):
    """Karta produktu artworkowego per REF — agregat read-only (artwork_card.build_card):
    dane wspólne z material_master + arkusz per poziom + historia rewizji z artwork_index."""
    import artwork_card as _ac
    db = get_db()
    try:
        card = _ac.build_card(db, ref)
    finally:
        db.close()
    if not card:
        flash("Nieznany REF — brak danych w master dacie i indeksie artworków.", "error")
        return redirect(url_for("artwork_index_page"))
    return render_template("artwork_card.html", card=card,
                           username=session.get("username"), role=session.get("role"))


@app.route("/artwork/manage")
@require_role("manager")
def artwork_manage_page():
    """Pulpit zarządzania artworkami — spina indeks, aliasy, rewizje, luki,
    duplikaty i status plików w jeden obraz stanu biblioteki."""
    return render_template("artwork_manage.html",
                           username=session.get("username"), role=session.get("role"))


@app.route("/api/artwork/manage/kpi", methods=["GET"])
@require_role("manager")
def api_artwork_manage_kpi():
    """Zbiorcze KPI biblioteki do pulpitu (compute_kpis)."""
    import artwork_manage as _am
    db = get_db()
    try:
        kpi = _am.compute_kpis(db)
    finally:
        db.close()
    return jsonify(kpi)


@app.route("/artwork/manage/unbound")
@require_role("manager")
def artwork_manage_unbound_page():
    return render_template("artwork_manage_unbound.html",
                           username=session.get("username"), role=session.get("role"))


@app.route("/api/artwork/manage/unbound", methods=["GET"])
@require_role("manager")
def api_artwork_manage_unbound():
    """Pliki w indeksie bez rozpoznanego REF (do ręcznego wiązania)."""
    import artwork_manage as _am
    db = get_db()
    try:
        res = _am.list_unbound(db, q=request.args.get("q", ""),
                               page=request.args.get("page", 1, type=int))
    finally:
        db.close()
    return jsonify(res)


@app.route("/api/artwork/manage/suggest", methods=["GET"])
@require_role("manager")
def api_artwork_manage_suggest():
    """Propozycje produktu (REF) dla pliku bez REF. Query: rel_path."""
    import artwork_manage as _am
    rel_path = request.args.get("rel_path", "").strip()
    if not rel_path:
        return jsonify({"error": "Podaj rel_path"}), 400
    db = get_db()
    try:
        suggestions = _am.suggest_ref(db, rel_path)
    finally:
        db.close()
    return jsonify({"rel_path": rel_path, "suggestions": suggestions})


@app.route("/api/artwork/manage/bind", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_manage_bind():
    """Akceptacja: wiąże plik z REF (alias). Body: {rel_path, ref_code}."""
    import artwork_manage as _am
    data = request.get_json(silent=True) or {}
    rel_path = str(data.get("rel_path") or "").strip()
    ref_code = str(data.get("ref_code") or "").strip()
    if not rel_path or not ref_code:
        return jsonify({"error": "Podaj rel_path i ref_code"}), 400
    db = get_db()
    try:
        res = _am.bind_ref(db, rel_path, ref_code, user=session.get("username"))
    finally:
        db.close()
    if "error" in res:
        return jsonify(res), 400
    _log_audit("artwork_manage_bind", session.get("username"),
               f"ref={ref_code} file={rel_path} matched={res.get('matched')}")
    return jsonify(res)


@app.route("/artwork/manage/gaps")
@require_role("manager")
def artwork_manage_gaps_page():
    return render_template("artwork_manage_gaps.html",
                           username=session.get("username"), role=session.get("role"))


@app.route("/api/artwork/manage/gaps", methods=["GET"])
@require_role("manager")
def api_artwork_manage_gaps():
    """Produkty (material_master) bez żadnego artworku w indeksie."""
    import artwork_manage as _am
    db = get_db()
    try:
        rows = _am.find_gaps(db, q=request.args.get("q", ""))
    finally:
        db.close()
    return jsonify({"rows": rows, "total": len(rows)})


@app.route("/api/artwork/manage/duplicates", methods=["GET"])
@require_role("manager")
def api_artwork_manage_duplicates():
    """Grupy plików-duplikatów (ten sam REF + opakowanie + rewizja)."""
    import artwork_manage as _am
    db = get_db()
    try:
        groups = _am.find_duplicates(db)
    finally:
        db.close()
    return jsonify({"groups": groups, "total": len(groups)})


@app.route("/api/artwork/manage/file-status", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_manage_file_status():
    """Ustaw trwały status pliku. Body: {rel_path, status, note?}."""
    import artwork_manage as _am
    data = request.get_json(silent=True) or {}
    db = get_db()
    try:
        res = _am.set_file_status(db, str(data.get("rel_path") or ""),
                                  str(data.get("status") or ""),
                                  user=session.get("username"),
                                  note=str(data.get("note") or ""))
    finally:
        db.close()
    if "error" in res:
        return jsonify(res), 400
    _log_audit("artwork_manage_file_status", session.get("username"),
               f"file={res.get('rel_path')} status={res.get('status')}")
    return jsonify(res)


@app.route("/api/artwork/index/lookup", methods=["GET"])
@login_required
def api_artwork_index_lookup():
    """Najnowsza rewizja mastera dla REF + wszystkie rewizje (do ręcznej korekty)."""
    import artwork_index as _ai
    ref = request.args.get("ref", "").strip()
    if not ref:
        return jsonify({"error": "Podaj ref"}), 400
    db = get_db()
    try:
        best = _ai.lookup_best(db, ref)
        revs = _ai.all_revisions(db, ref)
    finally:
        db.close()
    return jsonify({"ref": ref, "best": best, "revisions": revs})


@app.route("/api/artwork/index/suggest", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_index_suggest():
    """Auto-dobór masterów dla listy REF-ów (pozycje zamówienia). #6
    Body: {refs:[...]}. Zwraca mapę REF→{found, master}."""
    import artwork_index as _ai
    data = request.get_json(silent=True) or {}
    refs = data.get("refs") or []
    if not isinstance(refs, list) or len(refs) > 1000:
        return jsonify({"error": "refs musi być listą (max 1000)"}), 400
    db = get_db()
    try:
        suggestions = _ai.suggest_for_refs(db, [str(x) for x in refs])
    finally:
        db.close()
    _found = sum(1 for v in suggestions.values() if v.get("found"))
    return jsonify({"suggestions": suggestions, "found": _found, "total": len(suggestions)})


@app.route("/api/artwork/index/stats", methods=["GET"])
@login_required
def api_artwork_index_stats():
    """Statystyki indeksu masterów (ile plików / unikalnych REF / ostatni skan)."""
    import artwork_index as _ai
    db = get_db()
    try:
        _ai.ensure_table(db)
        total = db.execute("SELECT COUNT(*) FROM artwork_index").fetchone()[0]
        refs = db.execute("SELECT COUNT(DISTINCT ref_norm) FROM artwork_index WHERE ref_norm<>''").fetchone()[0]
        last = db.execute("SELECT MAX(scanned_at) FROM artwork_index").fetchone()[0]
    finally:
        db.close()
    return jsonify({"files": total, "unique_refs": refs, "last_scan": last})


@app.route("/api/artwork/index/browse", methods=["GET"])
@login_required
def api_artwork_index_browse():
    """Przeszukiwalna, stronicowana biblioteka masterów (31k plików).
    Query: q, packaging, current (0/1), page."""
    import artwork_index as _ai
    db = get_db()
    try:
        res = _ai.browse(
            db,
            q=request.args.get("q", ""),
            packaging=request.args.get("packaging", ""),
            current_only=request.args.get("current") == "1",
            hide_ignored=request.args.get("hide_ignored") == "1",
            variant=request.args.get("variant", ""),
            marm_linked=request.args.get("marm_linked") == "1",
            page=request.args.get("page", 1, type=int),
            per_page=50,
        )
    finally:
        db.close()
    return jsonify(res)


@app.route("/artwork/aliases")
@login_required
def page_artwork_aliases():
    return render_template("artwork_aliases.html",
                           username=session.get("username"), role=session.get("role"))


@app.route("/artwork/revisions")
@login_required
def page_artwork_revisions():
    return render_template("artwork_revisions.html",
                           username=session.get("username"), role=session.get("role"))


@app.route("/api/artwork/alias", methods=["GET", "POST"])
@login_required
@csrf_protect
def api_artwork_alias():
    """Aliasy nazwa-pliku→REF (gdy nazwa artworku nie zawiera REF). GET: lista;
    POST (manager): {ref_code, pattern} — dodaje i backfilluje indeks."""
    import artwork_index as _ai
    db = get_db()
    try:
        if request.method == "POST":
            if session.get("role") not in ("manager", "superuser", "admin"):
                return jsonify({"error": "Brak uprawnień"}), 403
            data = request.get_json(silent=True) or {}
            res = _ai.add_alias(db, str(data.get("ref_code") or ""), str(data.get("pattern") or ""),
                                replace=bool(data.get("replace")))
            if "error" in res:
                return jsonify(res), 400
            _log_audit("artwork_alias_add", session.get("username"),
                       f"{data.get('ref_code')} <- {data.get('pattern')} ({res.get('matched')})"
                       + (" [replace]" if data.get("replace") else ""))
            return jsonify(res)
        return jsonify({"items": _ai.list_aliases(db)})
    finally:
        db.close()


@app.route("/api/artwork/alias/<int:aid>", methods=["DELETE"])
@require_role("manager")
@csrf_protect
def api_artwork_alias_delete(aid):
    import artwork_index as _ai
    db = get_db()
    try:
        res = _ai.delete_alias(db, aid)
    finally:
        db.close()
    return jsonify(res)


@app.route("/api/artwork/alias/suggest", methods=["GET"])
@login_required
def api_artwork_alias_suggest():
    """Podpowiada pliki artworków pasujące do REF (po opisie z master daty) lub
    do podanego tekstu — fuzzy po tokenach nazwy pliku."""
    import artwork_index as _ai
    ref = request.args.get("ref", "").strip()
    text = request.args.get("text", "").strip()
    db = get_db()
    try:
        opis = ""
        if ref:
            try:
                import material_master as _mm
                m = _mm.get_material(db, ref)
                if m:
                    # TXT_SHORT_PL (część nazwy artworku) ma priorytet — najlepiej
                    # pasuje do nazw plików; w razie braku użyj opisów.
                    opis = (m.get("txt_short_pl") or "").strip() or \
                        ((m.get("opis_pl") or "") + " " + (m.get("opis_en") or "")).strip()
            except Exception:
                opis = ""
        combined = (text + " " + opis).strip() or ref
        cands = _ai.suggest_by_text(db, combined, limit=12)
    finally:
        db.close()
    return jsonify({"ref": ref, "opis": opis, "candidates": cands})


@app.route("/artwork/mapping-groups")
@login_required
def page_artwork_mapping_groups():
    return render_template("artwork_mapping_groups.html",
                           username=session.get("username"), role=session.get("role"))


@app.route("/api/artwork/mapping-group", methods=["GET", "POST"])
@login_required
@csrf_protect
def api_artwork_mapping_group():
    """Grupy mapowań artworków (grupa → lista REF + jeden master). GET: lista;
    POST (manager): {id?, group_name, master_rel_path, refs[]} — tworzy/aktualizuje.
    REF w grupie dziedziczy master grupy, gdy nie ma własnego wzorca."""
    import artwork_index as _ai
    db = get_db()
    try:
        if request.method == "POST":
            if session.get("role") not in ("manager", "superuser", "admin"):
                return jsonify({"error": "Brak uprawnień"}), 403
            data = request.get_json(silent=True) or {}
            try:
                gid = _ai.save_group(
                    db,
                    str(data.get("group_name") or ""),
                    str(data.get("master_rel_path") or ""),
                    data.get("refs") or [],
                    created_by=session.get("user_id"),
                    gid=data.get("id") or None,
                )
            except ValueError as ve:
                return jsonify({"error": str(ve)}), 400
            _log_audit("artwork_group_save", session.get("username"),
                       f"{data.get('group_name')} ({len(data.get('refs') or [])} REF)")
            return jsonify({"ok": True, "id": gid})
        return jsonify({"items": _ai.list_groups(db)})
    finally:
        db.close()


@app.route("/api/artwork/mapping-group/<int:gid>", methods=["DELETE"])
@require_role("manager")
@csrf_protect
def api_artwork_mapping_group_delete(gid):
    import artwork_index as _ai
    db = get_db()
    try:
        _ai.delete_group(db, gid)
        _log_audit("artwork_group_delete", session.get("username"), f"group #{gid}")
    finally:
        db.close()
    return jsonify({"ok": True})


@app.route("/api/artwork/confirm", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_confirm():
    """Zapamiętuje potwierdzony artwork (REF→plik) — przy pierwszym porównaniu."""
    import artwork_index as _ai
    data = request.get_json(silent=True) or {}
    db = get_db()
    try:
        res = _ai.confirm_artwork(db, str(data.get("ref") or ""),
                                  str(data.get("rel_path") or ""),
                                  session.get("username", ""),
                                  level=str(data.get("level") or ""))
    finally:
        db.close()
    if "error" in res:
        return jsonify(res), 400
    _log_audit("artwork_confirm", session.get("username"),
               f"{data.get('ref')} [{data.get('level') or '—'}] = {data.get('rel_path')}")
    return jsonify(res)


@app.route("/api/artwork/unconfirm", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_unconfirm():
    """Cofa potwierdzenie artworku dla (REF, poziom) — pozwala poprawić błędny wybór."""
    import artwork_index as _ai
    data = request.get_json(silent=True) or {}
    db = get_db()
    try:
        res = _ai.unconfirm_artwork(db, str(data.get("ref") or ""),
                                    level=str(data.get("level") or ""))
    finally:
        db.close()
    _log_audit("artwork_unconfirm", session.get("username"),
               f"{data.get('ref')} [{data.get('level') or '—'}]")
    return jsonify(res)


@app.route("/api/artwork/confirm-status", methods=["GET"])
@login_required
def api_artwork_confirm_status():
    """Status dla flow porównania: czy trzeba zapytać o potwierdzenie i czy
    pojawiła się nowsza rewizja niż potwierdzona."""
    import artwork_index as _ai
    db = get_db()
    try:
        res = _ai.lookup_for_comparison(db, request.args.get("ref", "").strip())
    finally:
        db.close()
    return jsonify(res)


@app.route("/api/artwork/confirmations/stale", methods=["GET"])
@login_required
def api_artwork_stale():
    """REF-y, dla których pojawiła się nowsza rewizja niż potwierdzona artwork."""
    import artwork_index as _ai
    db = get_db()
    try:
        items = _ai.stale_confirmations(db)
    finally:
        db.close()
    return jsonify({"items": items, "count": len(items)})


@app.route("/api/uom", methods=["GET", "POST", "DELETE"])
@login_required
@csrf_protect
def api_uom():
    """Przeliczniki jednostek (#4). GET: lista; POST (manager): dodaj/edytuj
    {ref_norm?, unit_from, unit_to, factor}; DELETE (manager): {id}."""
    import uom as _uom
    db = get_db()
    try:
        _uom.ensure_table(db)
        if request.method == "GET":
            rows = db.execute(
                "SELECT id, ref_norm, unit_from, unit_to, factor FROM uom_conversion "
                "ORDER BY ref_norm, unit_from LIMIT 1000").fetchall()
            return jsonify({"items": [dict(r) for r in rows]})
        if session.get("role") not in ("manager", "superuser", "admin"):
            return jsonify({"error": "Brak uprawnień"}), 403
        data = request.get_json(silent=True) or {}
        if request.method == "DELETE":
            try:
                _id = int(data.get("id"))
            except (TypeError, ValueError):
                return jsonify({"error": "Brak id"}), 400
            db.execute("DELETE FROM uom_conversion WHERE id=?", (_id,))
            db.commit()
            return jsonify({"ok": True})
        # POST
        uf = _uom.canonical_unit(data.get("unit_from"))
        ut = _uom.canonical_unit(data.get("unit_to"))
        ref_norm = _uom.normalize_ref(data.get("ref_norm")) or "*"
        try:
            factor = float(data.get("factor"))
        except (TypeError, ValueError):
            return jsonify({"error": "Nieprawidłowy przelicznik"}), 400
        if not uf or not ut or factor <= 0:
            return jsonify({"error": "Podaj jednostki i dodatni przelicznik"}), 400
        db.execute(
            "INSERT INTO uom_conversion(ref_norm, unit_from, unit_to, factor) "
            "VALUES(?,?,?,?) ON CONFLICT(ref_norm, unit_from, unit_to) "
            "DO UPDATE SET factor=excluded.factor", (ref_norm, uf, ut, factor))
        db.commit()
        _log_audit("uom_set", session.get("username"),
                   f"{ref_norm} {uf}->{ut} x{factor}")
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/artwork/materials", methods=["GET"])
@require_role("manager")
def api_artwork_materials_list():
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        family_filter = request.args.get("family", "").strip()
        status_filter = request.args.get("status", "").strip()  # complete|partial|none
        search = request.args.get("q", "").strip()

        q = "SELECT * FROM artwork_material"
        params = []
        conds = []
        if family_filter:
            conds.append("product_family=?"); params.append(family_filter)
        if search:
            safe_search = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conds.append("(ref_code LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\')"); params += [f"%{safe_search}%", f"%{safe_search}%"]
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY product_family, ref_code"
        mats = db.execute(q, params).fetchall()

        level_types = db.execute(
            "SELECT id, code, label, sort_order, color FROM artwork_pkg_level_type ORDER BY sort_order"
        ).fetchall()
        lt_ids = [r["id"] for r in level_types]

        # Batch-fetch all levels in one query to avoid N+1
        mat_ids = [m["id"] for m in mats]
        all_levels: dict = {}
        if mat_ids:
            placeholders = ",".join(["?"] * len(mat_ids))
            levels_all = db.execute(
                # Bandit B608: interpolowane są tylko placeholdery ? (liczba = długość listy); wartości jako parametry.
                "SELECT aml.*, ap.name as profile_name FROM artwork_material_level aml "  # nosec B608
                f"LEFT JOIN artwork_profiles ap ON ap.id = aml.profile_id "
                f"WHERE aml.material_id IN ({placeholders})", mat_ids
            ).fetchall()
            for r in levels_all:
                all_levels.setdefault(r["material_id"], {})[r["level_type_id"]] = dict(r)

        result = []
        for m in mats:
            mid = m["id"]
            levels_map = all_levels.get(mid, {})
            assigned = sum(1 for lt in lt_ids if levels_map.get(lt, {}).get("profile_id"))
            total = len(lt_ids)
            if status_filter == "complete" and assigned < total:
                continue
            if status_filter == "partial" and (assigned == 0 or assigned == total):
                continue
            if status_filter == "none" and assigned > 0:
                continue
            result.append({
                "id": mid,
                "ref_code": m["ref_code"],
                "name": m["name"],
                "product_family": m["product_family"] or "",
                "ean": m["ean"] or "",
                "notes": m["notes"] or "",
                "created_at": m["created_at"],
                "updated_at": m["updated_at"],
                "levels": levels_map,
                "assigned": assigned,
                "total": total,
            })
        return jsonify({
            "items": result,
            "level_types": [dict(r) for r in level_types],
            "families": [r["product_family"] for r in db.execute(
                "SELECT DISTINCT product_family FROM artwork_material WHERE product_family!='' ORDER BY product_family"
            ).fetchall()],
        })
    finally:
        db.close()


@app.route("/api/artwork/materials", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_materials_create():
    data = request.get_json(silent=True) or {}
    ref = str(data.get("ref_code") or "").strip()
    if not ref:
        return jsonify({"error": "Brak ref_code"}), 400
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        db.execute(
            "INSERT INTO artwork_material (ref_code, name, product_family, ean, notes) VALUES (?, ?, ?, ?, ?)",
            (ref[:100], str(data.get("name") or "")[:200], str(data.get("product_family") or "")[:100],
             str(data.get("ean") or "")[:20], str(data.get("notes") or "")[:500])
        )
        db.commit()
        row = db.execute("SELECT * FROM artwork_material WHERE ref_code=?", (ref,)).fetchone()
        return jsonify(dict(row)), 201
    except Exception as e:
        logger.exception("artwork_material create error: %s", e)
        return jsonify({"error": "Błąd zapisu — ref_code musi być unikalny."}), 400
    finally:
        db.close()


@app.route("/api/artwork/materials/<int:mid>", methods=["PUT"])
@require_role("manager")
@csrf_protect
def api_artwork_materials_update(mid):
    data = request.get_json(silent=True) or {}
    ref = str(data.get("ref_code") or "").strip()
    if not ref:
        return jsonify({"error": "Brak ref_code"}), 400
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        db.execute(
            "UPDATE artwork_material SET ref_code=?, name=?, product_family=?, ean=?, notes=?, updated_at=datetime('now') WHERE id=?",
            (ref[:100], str(data.get("name") or "")[:200], str(data.get("product_family") or "")[:100],
             str(data.get("ean") or "")[:20], str(data.get("notes") or "")[:500], mid)
        )
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/artwork/materials/<int:mid>", methods=["DELETE"])
@require_role("manager")
@csrf_protect
def api_artwork_materials_delete(mid):
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        db.execute("DELETE FROM artwork_material_level WHERE material_id=?", (mid,))
        db.execute("DELETE FROM artwork_material WHERE id=?", (mid,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/artwork/materials/<int:mid>/levels/<int:ltid>", methods=["PUT"])
@require_role("manager")
@csrf_protect
def api_artwork_material_level_set(mid, ltid):
    data = request.get_json(silent=True) or {}
    _pid_raw = data.get("profile_id")
    try:
        profile_id = int(_pid_raw) if _pid_raw not in (None, "", False) else None
    except (TypeError, ValueError):
        profile_id = None
    _mt = str(data.get("mapping_type") or "direct").strip()
    mapping_type = _mt if _mt in ("direct", "none", "inherited") else ("direct" if profile_id else "none")
    if not profile_id:
        mapping_type = "none"
    _smid_raw = data.get("source_material_id")
    try:
        source_mid = int(_smid_raw) if _smid_raw not in (None, "", False) else None
    except (TypeError, ValueError):
        source_mid = None
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        existing = db.execute(
            "SELECT id FROM artwork_material_level WHERE material_id=? AND level_type_id=?",
            (mid, ltid)
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE artwork_material_level SET profile_id=?, mapping_type=?, source_material_id=?, notes=?, updated_at=datetime('now') WHERE id=?",
                (profile_id, mapping_type, source_mid, str(data.get("notes") or "")[:500], existing["id"])
            )
        else:
            db.execute(
                "INSERT INTO artwork_material_level (material_id, level_type_id, profile_id, mapping_type, source_material_id, notes) VALUES (?, ?, ?, ?, ?, ?)",
                (mid, ltid, profile_id, mapping_type, source_mid, str(data.get("notes") or "")[:500])
            )
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/artwork/materials/import-csv", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_materials_import_csv():
    """Import materials from CSV. Expected columns: ref_code, name, product_family, ean, notes
    Optionally: sztuka_profile, op_profile, opz_profile, karton_profile (profile names to auto-assign)"""
    import csv, io
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "Brak pliku"}), 400
    try:
        content = f.read().decode("utf-8-sig")  # utf-8-sig strips BOM from Excel exports
        reader = csv.DictReader(io.StringIO(content))
        rows = list(reader)
    except Exception as e:
        logger.exception("CSV parse error: %s", e)
        return jsonify({"error": "Błąd parsowania CSV — sprawdź format i kodowanie pliku."}), 400

    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        level_types = {r["code"]: r["id"] for r in
                       db.execute("SELECT code, id FROM artwork_pkg_level_type").fetchall()}
        profiles = {r["name"]: r["id"] for r in
                    db.execute("SELECT name, id FROM artwork_profiles").fetchall()}

        imported = 0; updated = 0; skipped = 0; errors = []
        for row in rows:
            ref = (row.get("ref_code") or row.get("REF") or "").strip()
            if not ref:
                skipped += 1; continue
            name = (row.get("name") or row.get("nazwa") or "").strip()
            family = (row.get("product_family") or row.get("rodzina") or "").strip()
            ean = (row.get("ean") or "").strip()
            notes = (row.get("notes") or row.get("uwagi") or "").strip()
            try:
                existing = db.execute("SELECT id FROM artwork_material WHERE ref_code=?", (ref,)).fetchone()
                if existing:
                    mid = existing["id"]
                    db.execute(
                        "UPDATE artwork_material SET name=?, product_family=?, ean=?, notes=?, updated_at=datetime('now') WHERE id=?",
                        (name[:200], family[:100], ean[:20], notes[:500], mid)
                    )
                    updated += 1
                else:
                    db.execute(
                        "INSERT INTO artwork_material (ref_code, name, product_family, ean, notes) VALUES (?, ?, ?, ?, ?)",
                        (ref[:100], name[:200], family[:100], ean[:20], notes[:500])
                    )
                    # BUGFIX: szukaj po ref[:100] (tak zapisano), inaczej dla ref>100 znaków
                    # fetchone() = None → crash na ["id"]. Brak wiersza → pomiń przypisania.
                    _mrow = db.execute("SELECT id FROM artwork_material WHERE ref_code=?", (ref[:100],)).fetchone()
                    if not _mrow:
                        continue
                    mid = _mrow["id"]
                    imported += 1
                # Auto-assign profiles if column exists
                for col_suffix, lt_code in [("sztuka", "sztuka"), ("op", "op"), ("opz", "opz"), ("karton", "karton")]:
                    pname = (row.get(f"{col_suffix}_profile") or row.get(f"profil_{col_suffix}") or "").strip()
                    if pname and pname in profiles and col_suffix in level_types:
                        ltid = level_types[col_suffix]
                        pid = profiles[pname]
                        ex = db.execute(
                            "SELECT id FROM artwork_material_level WHERE material_id=? AND level_type_id=?",
                            (mid, ltid)
                        ).fetchone()
                        if ex:
                            db.execute("UPDATE artwork_material_level SET profile_id=?, mapping_type='direct', updated_at=datetime('now') WHERE id=?", (pid, ex["id"]))
                        else:
                            db.execute(
                                "INSERT INTO artwork_material_level (material_id, level_type_id, profile_id, mapping_type) VALUES (?, ?, ?, 'direct')",
                                (mid, ltid, pid)
                            )
                db.commit()   # per-wiersz: na PG błąd jednego wiersza psuje transakcję
            except Exception as e:
                db.rollback()  # odblokuj transakcję dla kolejnych wierszy
                errors.append(f"{ref}: {str(e)[:200]}")
        return jsonify({"imported": imported, "updated": updated, "skipped": skipped, "errors": errors})
    finally:
        db.close()


@app.route("/api/artwork/materials/export-csv", methods=["GET"])
@require_role("manager")
def api_artwork_materials_export_csv():
    """Export material master as CSV."""
    import csv, io
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        level_types = db.execute(
            "SELECT id, code FROM artwork_pkg_level_type ORDER BY sort_order"
        ).fetchall()
        mats = db.execute("SELECT * FROM artwork_material ORDER BY product_family, ref_code").fetchall()
        # Bulk-load all profile assignments in one query to avoid N+1
        _profile_map: dict = {}
        for _lv_row in db.execute(
            "SELECT aml.material_id, aml.level_type_id, ap.name "
            "FROM artwork_material_level aml JOIN artwork_profiles ap ON ap.id=aml.profile_id"
        ).fetchall():
            _profile_map[(_lv_row["material_id"], _lv_row["level_type_id"])] = _lv_row["name"]
        buf = io.StringIO()
        cols = ["ref_code", "name", "product_family", "ean", "notes"] + [f"{r['code']}_profile" for r in level_types]
        writer = csv.DictWriter(buf, fieldnames=cols)
        writer.writeheader()
        for m in mats:
            row_d = {"ref_code": m["ref_code"], "name": m["name"],
                     "product_family": m["product_family"] or "",
                     "ean": m["ean"] or "", "notes": m["notes"] or ""}
            for lt in level_types:
                row_d[f"{lt['code']}_profile"] = _profile_map.get((m["id"], lt["id"]), "")
            writer.writerow(row_d)
        csv_bytes = buf.getvalue().encode("utf-8-sig")
        return csv_bytes, 200, {
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": "attachment; filename=material_master.csv"
        }
    finally:
        db.close()


@app.route("/api/admin/system-status")
@require_role("admin")
def api_admin_system_status():
    """Return status of all system and Python dependencies."""
    import importlib, shutil, subprocess, sys, platform
    checks = []

    def _check(name, label, test_fn, detail_fn=None):
        try:
            ok = test_fn()
            detail = detail_fn() if (ok and detail_fn) else ""
        except Exception as e:
            ok = False
            detail = str(e)[:200]
        checks.append({"name": name, "label": label, "ok": ok, "detail": detail})

    # ── system binaries ──────────────────────────────────────────
    _check("tesseract", "Tesseract OCR",
           lambda: bool(shutil.which("tesseract")),
           lambda: subprocess.check_output(["tesseract", "--version"], stderr=subprocess.STDOUT).decode().splitlines()[0])

    def _tess_langs():
        out = subprocess.check_output(["tesseract", "--list-langs"], stderr=subprocess.STDOUT).decode()
        langs = [l.strip() for l in out.splitlines() if l.strip() and "List" not in l]
        return "języki: " + ", ".join(langs)
    _check("tesseract_langs", "Tesseract — języki (pol/eng)",
           lambda: bool(shutil.which("tesseract")) and "pol" in subprocess.check_output(
               ["tesseract", "--list-langs"], stderr=subprocess.STDOUT).decode(),
           _tess_langs)

    _check("ghostscript", "Ghostscript (camelot tabele)",
           lambda: bool(shutil.which("gs") or shutil.which("gswin64c")),
           lambda: subprocess.check_output(["gs", "--version"], stderr=subprocess.STDOUT).decode().strip())

    _check("poppler", "Poppler (pdfinfo/pdftoppm)",
           lambda: bool(shutil.which("pdftoppm") or shutil.which("pdfinfo")),
           lambda: subprocess.check_output(["pdftoppm", "-v"], stderr=subprocess.STDOUT).decode().splitlines()[0])

    # ── Python packages ──────────────────────────────────────────
    def _pyver(pkg):
        m = importlib.import_module(pkg.replace("-", "_"))
        return getattr(m, "__version__", "?")

    for pkg, lbl in [
        ("flask", "Flask"),
        ("pymupdf", "PyMuPDF (fitz)"),
        ("pdfplumber", "pdfplumber"),
        ("camelot", "camelot"),
        ("pytesseract", "pytesseract"),
        ("PIL", "Pillow"),
        ("cv2", "OpenCV"),
        ("numpy", "numpy"),
        ("scipy", "scipy"),
        ("sklearn", "scikit-learn"),
        ("rapidfuzz", "rapidfuzz"),
        ("anthropic", "anthropic SDK"),
        ("reportlab", "ReportLab"),
        ("openpyxl", "openpyxl"),
        ("docx", "python-docx"),
    ]:
        mod = pkg.replace("-", "_")
        _check(f"py_{pkg}", lbl,
               lambda m=mod: bool(importlib.import_module(m)),
               lambda m=mod: _pyver(m))

    # ── barcode libs (optional) ───────────────────────────────────
    _check("py_zxingcpp", "zxingcpp (barcodes, preferred)",
           lambda: bool(importlib.import_module("zxingcpp")),
           lambda: _pyver("zxingcpp"))
    _check("py_pyzbar", "pyzbar (barcodes, fallback)",
           lambda: bool(importlib.import_module("pyzbar")),
           lambda: _pyver("pyzbar"))

    # ── runtime info ─────────────────────────────────────────────
    from db import SQLITE_PATH as _SQLITE_PATH
    runtime = {
        "python": sys.version,
        "platform": platform.platform(),
        "masters_dir": MASTERS_DIR,
        "masters_dir_writable": os.access(MASTERS_DIR, os.W_OK),
        "masters_dir_exists": os.path.isdir(MASTERS_DIR),
        "data_dir_env": os.environ.get("DATA_DIR", "(not set)"),
        "data_dir_exists": os.path.isdir(os.environ.get("DATA_DIR", "/data")),
        "sqlite_path": _SQLITE_PATH,
        "sqlite_exists": os.path.isfile(_SQLITE_PATH),
        "database_url_set": bool(os.environ.get("DATABASE_URL")),
        "anthropic_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
    }

    ok_count = sum(1 for c in checks if c["ok"])
    return jsonify({"ok": True, "checks": checks, "runtime": runtime,
                    "summary": {"total": len(checks), "ok": ok_count, "fail": len(checks) - ok_count}})


@app.route("/api/admin/activity-log")
@require_role("manager")
def api_admin_activity_log():
    """
    Export audit_log as JSONL (one JSON object per line).
    Query params:
      limit  — max rows (default 2000, max 10000)
      event  — filter by event name prefix
      user   — filter by username
      since  — ISO date string, e.g. '2026-05-01'
    Requires manager role or above.
    """
    import json as _json
    if session.get("role", "user") not in ("manager", "superuser", "admin"):
        return jsonify({"error": "Brak dostępu"}), 403

    try:
        limit = max(1, min(int(request.args.get("limit", 2000)), 10000))
    except (TypeError, ValueError):
        limit = 2000
    event_filter = request.args.get("event", "").strip()
    user_filter  = request.args.get("user", "").strip()
    since_filter = request.args.get("since", "").strip()

    db2 = get_db()
    try:
        where_parts = []
        params = []
        if event_filter:
            _esc_ev = event_filter.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where_parts.append("event LIKE ? ESCAPE '\\'")
            params.append(_esc_ev + "%")
        if user_filter:
            where_parts.append("username = ?")
            params.append(user_filter)
        if since_filter:
            where_parts.append("created_at >= ?")
            params.append(since_filter)
        where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
        rows = db2.execute(
            # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
            f"SELECT id, event, username, detail, ip, duration_ms, extra, created_at "  # nosec B608
            f"FROM audit_log {where_sql} ORDER BY id DESC LIMIT ?",
            params + [limit]
        ).fetchall()
    finally:
        db2.close()

    lines = []
    for r in rows:
        extra_val = None
        if r["extra"]:
            try:
                extra_val = _json.loads(r["extra"])
            except Exception:
                extra_val = r["extra"]
        lines.append(_json.dumps({
            "id":          r["id"],
            "ts":          r["created_at"],
            "event":       r["event"],
            "user":        r["username"],
            "detail":      r["detail"],
            "ip":          r["ip"],
            "duration_ms": r["duration_ms"],
            "extra":       extra_val,
        }, ensure_ascii=False))

    from flask import Response
    return Response(
        "\n".join(lines) + "\n",
        mimetype="application/x-ndjson",
        headers={"Content-Disposition": "attachment; filename=activity_log.jsonl"}
    )


# ═══════════════════════════════════════════════════════════════
# ERROR HANDLERS
# ═══════════════════════════════════════════════════════════════

@app.errorhandler(404)
def page_not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Nie znaleziono"}), 404
    return render_template("404.html",
                           username=session.get("username"),
                           role=session.get("role", "user")), 404

@app.errorhandler(413)
def request_entity_too_large(e):
    msg = f"Plik jest zbyt duży. Maksymalny rozmiar to {MAX_UPLOAD_MB} MB."
    if request.path.startswith("/api/"):
        return jsonify({"error": msg}), 413
    return render_template("500.html",
                           error_code=413,
                           error_title="Plik zbyt duży",
                           error_message=msg,
                           username=session.get("username"),
                           role=session.get("role", "user")), 413

@app.errorhandler(429)
def too_many_requests(e):
    msg = "Zbyt wiele żądań. Poczekaj chwilę i spróbuj ponownie."
    if request.path.startswith("/api/"):
        return jsonify({"error": msg}), 429
    return render_template("500.html",
                           error_code=429,
                           error_title="Zbyt wiele żądań",
                           error_message=msg,
                           username=session.get("username"),
                           role=session.get("role", "user")), 429

@app.errorhandler(500)
def internal_error(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Błąd serwera"}), 500
    return render_template("500.html",
                           username=session.get("username"),
                           role=session.get("role", "user")), 500


@app.route("/robots.txt")
def robots_txt():
    return app.response_class(
        "User-agent: *\nDisallow: /\n",
        mimetype="text/plain"
    )


# ─────────────────────────────────────────────────────────────────────────────
# ROUTE — POWER AUTOMATE INTAKE
# Przyjmuje PDF z Power Automate (lub dowolnego HTTP POST z tokenem).
# Autoryzacja: nagłówek  Authorization: Bearer <INTAKE_TOKEN>
# Env var INTAKE_TOKEN musi być ustawiony w Coolify — bez niego endpoint
# jest wyłączony (zwraca 503).
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/intake/artwork", methods=["POST"])
def intake_artwork():
    token = os.environ.get("INTAKE_TOKEN", "")
    if not token:
        return jsonify({"error": "Intake wyłączony — brak INTAKE_TOKEN"}), 503
    import hmac as _hmac_intake
    auth = request.headers.get("Authorization", "")
    if not _hmac_intake.compare_digest(auth, f"Bearer {token}"):
        return jsonify({"error": "Brak autoryzacji"}), 401

    file = request.files.get("file")
    if not file:
        return jsonify({"error": "Brak pliku (pole: file)"}), 400
    fname = secure_filename(file.filename or "artwork.pdf")
    if not fname.lower().endswith(".pdf"):
        return jsonify({"error": "Tylko pliki PDF"}), 400

    # magic bytes check
    header = file.stream.read(4)
    file.stream.seek(0)
    if header != b"%PDF":
        return jsonify({"error": "Plik nie jest prawidłowym dokumentem PDF"}), 400

    sender  = (request.form.get("sender")  or request.headers.get("X-Sender",  "")).strip()[:200]
    subject = (request.form.get("subject") or request.headers.get("X-Subject", "")).strip()[:200]

    intake_dir = os.path.join(_DATA_DIR if os.path.isdir(_DATA_DIR) else
                              os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads"),
                              "intake")
    os.makedirs(intake_dir, exist_ok=True)

    import hashlib as _hs, datetime as _dt2
    raw = file.read()
    h = _hs.md5(raw, usedforsecurity=False).hexdigest()[:10]
    date_str = _dt2.date.today().isoformat()
    dest_name = f"{date_str}_{h}_{fname}"
    dest_path = os.path.join(intake_dir, dest_name)
    with open(dest_path, "wb") as fh:
        fh.write(raw)

    db = get_db()
    try:
        db.execute(
            "INSERT INTO intake_queue(filename, path, sender, subject, created_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (dest_name, dest_path, sender, subject)
        )
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

    logger.info("Intake artwork: %s from %s", dest_name, sender or "unknown")
    return jsonify({"ok": True, "filename": dest_name}), 201


# ═══════════════════════════════════════════════════════════════
# WEBHOOKI — integracja z systemami zewnętrznymi (ERP/WMS)
# ═══════════════════════════════════════════════════════════════

def _is_ssrf_blocked_url(url: str) -> bool:
    """Return True if the URL resolves to a private/loopback address (SSRF guard)."""
    import socket as _sock, ipaddress as _ip
    try:
        from urllib.parse import urlparse as _up
        if _up(url).scheme not in ("http", "https"):   # np. file:// — tylko HTTP(S)
            return True
        host = _up(url).hostname or ""
        if not host:
            return True
        resolved = _sock.getaddrinfo(host, None)
        for _fam, _type, _proto, _canon, _addr in resolved:
            addr = _ip.ip_address(_addr[0])
            if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
                return True
    except Exception:
        return True
    return False


def _fire_webhooks(event: str, payload: dict):
    """Wysyła payload JSON do wszystkich aktywnych webhooków pasujących do eventu.

    Uruchamiane w tle (daemon thread) — nie blokuje odpowiedzi HTTP.
    """
    import urllib.request as _ur, hashlib as _hl, hmac as _hmac, json as _json
    try:
        db = get_db()
        try:
            rows = db.execute(
                "SELECT url, secret, events FROM webhooks WHERE is_active=1"
            ).fetchall()
        finally:
            db.close()
    except Exception as _e:
        logger.warning("webhook DB read failed: %s", _e)
        return

    body = _json.dumps({"event": event, **payload}, ensure_ascii=False).encode()
    for row in rows:
        subscribed = [e.strip() for e in (row["events"] or "").split(",")]
        if event not in subscribed and "*" not in subscribed:
            continue
        if _is_ssrf_blocked_url(row["url"]):
            logger.warning("webhook SSRF blocked: %s", row["url"])
            continue
        try:
            headers = {"Content-Type": "application/json", "X-DocCompare-Event": event}
            if row["secret"]:
                sig = _hmac.new(row["secret"].encode(), body, _hl.sha256).hexdigest()
                headers["X-DocCompare-Signature"] = f"sha256={sig}"
            req = _ur.Request(row["url"], data=body, headers=headers, method="POST")
            # Bandit B310: schemat http(s) i adres publiczny sprawdza _is_ssrf_blocked_url() wyżej.
            with _ur.urlopen(req, timeout=10) as _resp:  # nosec B310
                pass
        except Exception as _e:
            logger.warning("webhook delivery failed to %s: %s", row["url"], _e)


def _fire_webhooks_bg(event: str, payload: dict):
    """Non-blocking wrapper — fires webhook in a daemon thread."""
    t = threading.Thread(target=_fire_webhooks, args=(event, payload), daemon=True)
    t.start()


@app.route("/api/webhooks", methods=["GET"])
@require_role("admin")
def api_webhooks_list():
    db = get_db()
    try:
        raw = db.execute("SELECT id, name, url, secret, events, is_active, created_at FROM webhooks ORDER BY id").fetchall()
        rows = []
        for r in raw:
            row = dict(r)
            row["secret"] = "***" if row.get("secret") else ""
            rows.append(row)
    finally:
        db.close()
    return jsonify(rows)


@app.route("/api/webhooks", methods=["POST"])
@require_role("admin")
@csrf_protect
def api_webhooks_create():
    data = request.get_json(silent=True)
    err = _validate_json_body(data, {"name": (str, True), "url": (str, True)})
    if err:
        return jsonify({"error": err}), 400
    data = data or {}
    url = str(data.get("url") or "").strip()[:2000]
    name = str(data.get("name") or "").strip()[:200]
    if not url or not name:
        return jsonify({"error": "Wymagane: name i url"}), 400
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "URL musi zaczynać się od http:// lub https://"}), 400
    db = get_db()
    try:
        _wh_cur = db.execute(
            "INSERT INTO webhooks (name, url, secret, events, is_active, created_by) VALUES (?,?,?,?,1,?)",
            (name, url, str(data.get("secret") or "")[:500],
             str(data.get("events") or "comparison.completed")[:500], session["user_id"])
        )
        db.commit()
        wid = _wh_cur.lastrowid
    finally:
        db.close()
    return jsonify({"ok": True, "id": wid})


@app.route("/api/webhooks/<int:wid>", methods=["DELETE"])
@require_role("admin")
@csrf_protect
def api_webhooks_delete(wid):
    db = get_db()
    try:
        db.execute("DELETE FROM webhooks WHERE id=?", (wid,))
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True})


@app.route("/api/webhooks/<int:wid>/test", methods=["POST"])
@require_role("admin")
@csrf_protect
def api_webhooks_test(wid):
    """Wysyła testowy event do webhooka."""
    db = get_db()
    try:
        row = db.execute("SELECT * FROM webhooks WHERE id=?", (wid,)).fetchone()
    finally:
        db.close()
    if not row:
        return jsonify({"error": "Nie znaleziono"}), 404
    _fire_webhooks_bg("test.ping", {"message": "DocCompare webhook test", "webhook_id": wid})
    return jsonify({"ok": True, "message": "Test event wysłany"})


# ─────────────────────────────────────────────────────────────────────────────
# SŁOWNIK TŁUMACZEŃ
# ─────────────────────────────────────────────────────────────────────────────

# ── Słownik Incoterms ─────────────────────────────────────────────────────────
# _INCOTERMS_SEED / _ensure_incoterms_table / trasy /incoterms → blueprints.incoterms


@app.route("/data")
@login_required
def data_hub_page():
    return render_template("data_hub.html",
                           username=session.get("username"),
                           role=session.get("role"))


# ── Baza produktów ────────────────────────────────────────────────────────────

@app.route("/products")
@login_required
def products_page():
    # Scalone z Master data materiałów — „Baza produktów" przekierowuje do /materials.
    return redirect("/materials")


@app.route("/api/products", methods=["GET"])
@login_required
def api_products_list():
    q = request.args.get("q", "").strip()[:200]
    try:
        page = max(1, min(10000, int(request.args.get("page", 1))))
    except (ValueError, TypeError):
        page = 1
    per_page = 50
    offset = (page - 1) * per_page
    db = get_db()
    try:
        if q:
            _esc_q = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{_esc_q}%"
            rows = db.execute(
                "SELECT id, ref_code, product_name, ean, unit, tariff_cn, supplier_codes, active "
                "FROM products WHERE (ref_code LIKE ? ESCAPE '\\' OR product_name LIKE ? ESCAPE '\\' OR ean LIKE ? ESCAPE '\\') "
                "AND active=1 ORDER BY ref_code LIMIT ? OFFSET ?",
                (pattern, pattern, pattern, per_page, offset)
            ).fetchall()
            total = db.execute(
                "SELECT COUNT(*) FROM products WHERE (ref_code LIKE ? ESCAPE '\\' OR product_name LIKE ? ESCAPE '\\' OR ean LIKE ? ESCAPE '\\') AND active=1",
                (pattern, pattern, pattern)
            ).fetchone()[0]
        else:
            rows = db.execute(
                "SELECT id, ref_code, product_name, ean, unit, tariff_cn, supplier_codes, active "
                "FROM products WHERE active=1 ORDER BY ref_code LIMIT ? OFFSET ?",
                (per_page, offset)
            ).fetchall()
            total = db.execute("SELECT COUNT(*) FROM products WHERE active=1").fetchone()[0]
        return jsonify({"items": [dict(r) for r in rows], "total": total, "page": page})
    finally:
        db.close()


@app.route("/api/products", methods=["POST"])
@login_required
@require_role("manager")
@csrf_protect
def api_products_create():
    data = request.get_json(silent=True) or {}
    ref = str(data.get("ref_code") or "").strip().upper()[:100]
    name = str(data.get("product_name") or "").strip()[:500]
    if not ref or not name:
        return jsonify({"error": "ref_code i product_name są wymagane"}), 400
    db = get_db()
    try:
        try:
            cur = db.execute(
                "INSERT INTO products(ref_code, product_name, ean, unit, tariff_cn, description, supplier_codes) "
                "VALUES(?,?,?,?,?,?,?)",
                (ref, name, str(data.get("ean") or "")[:30], str(data.get("unit") or "szt")[:20],
                 str(data.get("tariff_cn") or "")[:20], str(data.get("description") or "")[:1000],
                 str(data.get("supplier_codes") or "")[:500])
            )
            db.commit()
            return jsonify({"ok": True, "id": cur.lastrowid})
        except Exception as e:
            if "UNIQUE" in str(e).upper():
                return jsonify({"error": f"Kod REF {ref} już istnieje"}), 409
            raise
    finally:
        db.close()


@app.route("/api/products/<int:pid>", methods=["PUT"])
@require_role("manager")
@csrf_protect
def api_products_update(pid):
    data = request.get_json(silent=True) or {}
    _prod_max_lens = {"product_name": 500, "ean": 30, "unit": 20, "tariff_cn": 20,
                      "description": 1000, "supplier_codes": 500}
    fields, vals = [], []
    for k in ("product_name", "ean", "unit", "tariff_cn", "description", "supplier_codes", "active"):
        if k in data:
            v = data[k]
            if k == "active":
                v = 1 if v else 0
            elif k in _prod_max_lens:
                v = str(v or "")[:_prod_max_lens[k]]
            fields.append(f"{k}=?")
            vals.append(v)
    if not fields:
        return jsonify({"error": "Brak pól"}), 400
    fields.append("updated_at=datetime('now')")
    vals.append(pid)
    db = get_db()
    try:
        # Bandit B608: nazwy kolumn z twardej białej listy w kodzie; wartości jako parametry ?.
        db.execute(f"UPDATE products SET {', '.join(fields)} WHERE id=?", vals)  # nosec B608
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/products/<int:pid>", methods=["DELETE"])
@require_role("manager")
@csrf_protect
def api_products_delete(pid):
    db = get_db()
    try:
        db.execute("UPDATE products SET active=0 WHERE id=?", (pid,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/products/import", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_products_import():
    """Import products from CSV. Expected columns: ref_code, product_name, ean, unit, tariff_cn."""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "Brak pliku"}), 400
    if not f.filename.lower().endswith(".csv"):
        return jsonify({"error": "Dozwolone tylko pliki .csv"}), 400
    import csv, io
    try:
        content = f.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        db = get_db()
        ok, skipped = 0, 0
        try:
            for row in reader:
                ref = (row.get("ref_code") or row.get("REF") or "").strip().upper()
                name = (row.get("product_name") or row.get("Nazwa") or "").strip()
                if not ref or not name:
                    skipped += 1
                    continue
                try:
                    db.execute(
                        "INSERT INTO products(ref_code, product_name, ean, unit, tariff_cn, description, supplier_codes) "
                        "VALUES(?,?,?,?,?,?,?) "
                        "ON CONFLICT(ref_code) DO UPDATE SET product_name=excluded.product_name, "
                        "ean=excluded.ean, unit=excluded.unit, tariff_cn=excluded.tariff_cn, "
                        "description=excluded.description, supplier_codes=excluded.supplier_codes, "
                        "updated_at=datetime('now')",
                        (ref[:100], name[:300], row.get("ean", "")[:20], row.get("unit", "szt")[:20],
                         row.get("tariff_cn", "")[:20], row.get("description", "")[:1000],
                         row.get("supplier_codes", "")[:500])
                    )
                    db.commit()   # per-wiersz: na PG nieudany INSERT psuje całą transakcję
                    ok += 1
                except Exception:
                    db.rollback()  # odblokuj transakcję, by kolejne wiersze przeszły
                    skipped += 1
        finally:
            db.close()
        return jsonify({"ok": True, "imported": ok, "skipped": skipped})
    except Exception as e:
        logger.exception("Transport import error: %s", e)
        return jsonify({"error": "Błąd importu danych — sprawdź format pliku."}), 500


if __name__ == "__main__":
    print("=" * 60)
    print("  DocCompare — lokalny silnik (bez API)")
    print("  http://localhost:5000")
    print()
    print("  Nowe funkcje:")
    print("    /batch      — batch processing wielu par PDF")
    print("    /suppliers  — profile dostawców")
    print()
    print("  Eksport: PDF i Excel z każdego raportu")
    print("=" * 60)
    _debug = os.environ.get("FLASK_DEBUG", "0") == "1" or os.environ.get("FLASK_ENV") == "development"
    app.run(debug=_debug, host="127.0.0.1", port=5000)
