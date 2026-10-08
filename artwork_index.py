"""
artwork_index.py — indeks masterów artworków z dysku Z:\\ (31k+ PDF).

Agent po stronie ACME (tools/scan_artwork_index.py) cyklicznie skanuje Z:\\,
parsuje nazwy plików (artwork_naming.parse_master_filename) i wysyła paczki do
chmury (POST /api/artwork/index/sync). Pliki ZOSTAJĄ na Z:\\ — w chmurze trzymamy
tylko metadane (REF, ścieżka, rewizja), żeby:
  • dobierać artworki do pozycji zamówienia po REF (auto + ręczna korekta, #6),
  • zawsze wskazywać NAJNOWSZĄ rewizję mastera.

Moduł jest „czysty": operuje na połączeniu DB przekazanym z app.py.
"""
from __future__ import annotations

import re
import json
import os

try:
    from artwork_naming import parse_master_filename, revision_sort_key
except Exception:  # pragma: no cover
    parse_master_filename = None
    revision_sort_key = None


def normalize_ref(raw) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(raw or "").upper())


# ── Poziom opakowania w nazwie pliku ──────────────────────────────────────────
# Ten sam REF (np. BT-PC6060) ma różne artworki per poziom: pouch dla OP, carton
# dla KAR. Nazwa pliku NIE zawiera REF-a, ale zawiera słowo poziomu, więc po nim
# rozróżniamy. Dzięki temu dla kartonu proponujemy „...carton..." a nie „...pouch".
_LEVEL_KEYWORDS = {
    "sztuka": ("single", "unit", "piece", "pcs"),
    "op":     ("pouch", "saszetka", "sachet", "torebka", "blister"),
    "opz":    ("inner", "zbiorcze", "wrap"),
    "karton": ("karton", "carton", "case"),
}
# Etykiety z UI (SZT/OP/OPZ/KAR) i synonimy → klucz _LEVEL_KEYWORDS.
_LEVEL_ALIASES = {
    "szt": "sztuka", "sztuka": "sztuka", "single": "sztuka",
    "op": "op", "pouch": "op",
    "opz": "opz",
    "kar": "karton", "karton": "karton", "carton": "karton", "box": "karton",
}
_LEVEL_SEP = "\x1f"   # rozdziela REF od poziomu w kluczu potwierdzenia (niewidoczny)


def _norm_level(level) -> str:
    """Etykieta/klucz poziomu (KAR, karton, OP…) → klucz w _LEVEL_KEYWORDS ('' = brak)."""
    s = str(level or "").strip().lower()
    if not s or s in ("—", "-"):
        return ""
    return _LEVEL_ALIASES.get(s, s if s in _LEVEL_KEYWORDS else "")


def _level_keywords(level) -> tuple:
    return _LEVEL_KEYWORDS.get(_norm_level(level), ())


def _other_level_keywords(level) -> set:
    """Słowa-klucze WSZYSTKICH innych poziomów (do odrzucania złych dopasowań)."""
    cur = _norm_level(level)
    if not cur:
        return set()
    out = set()
    for lv, kws in _LEVEL_KEYWORDS.items():
        if lv != cur:
            out.update(kws)
    return out - set(_level_keywords(level))


def _conf_key(ref, level="") -> str:
    """Klucz potwierdzenia: per-(REF, poziom). Bez poziomu = sam REF (kompatybilność
    wstecz z istniejącymi potwierdzeniami zapisanymi po samym REF)."""
    base = normalize_ref(ref)
    lv = _norm_level(level)
    return f"{base}{_LEVEL_SEP}{lv}" if lv else base


def _pick_level(rows: list, level=None) -> dict | None:
    """Z listy plików (posortowanej najnowsze-first) wybierz pasujący do poziomu.
    Jeśli żaden nie pasuje do poziomu, ale jakiś pasuje do INNEGO poziomu — zwróć
    None (lepiej „brak", niż pokazać pouch jako carton)."""
    if not rows:
        return None
    kws = _level_keywords(level)
    if not kws:
        return rows[0]
    matching = [r for r in rows if any(k in str(r.get("filename", "")).lower() for k in kws)]
    if matching:
        return matching[0]
    other = _other_level_keywords(level)
    non_conflict = [r for r in rows
                    if not any(k in str(r.get("filename", "")).lower() for k in other)]
    return non_conflict[0] if non_conflict else None


def ensure_table(db) -> None:
    # Memoizacja per-połączenie (patrz material_master.ensure_table): na PG DDL
    # wykonuje się raz na żądanie, nie przy każdym lookup_best w pętli po REF-ach.
    if getattr(db, "_ai_idx_ensured", False):
        return
    db.execute("""CREATE TABLE IF NOT EXISTS artwork_index (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ref_norm TEXT DEFAULT '',
        ref_code TEXT DEFAULT '',
        filename TEXT DEFAULT '',
        rel_path TEXT UNIQUE NOT NULL,
        packaging_type TEXT DEFAULT '',
        revision TEXT DEFAULT '',
        revision_rank INTEGER DEFAULT 0,
        ean TEXT DEFAULT '',
        source_mtime TEXT DEFAULT '',
        scanned_at TEXT DEFAULT (datetime('now'))
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_artwork_index_ref ON artwork_index(ref_norm)")
    # Wyszukiwanie aliasowe/suggest robi dużo `filename LIKE '%..%'` — indeks przyspiesza
    # prefiltry i skany po backfillu aliasów (przy 31k masterach sekwencyjny skan bolał).
    db.execute("CREATE INDEX IF NOT EXISTS idx_artwork_index_filename ON artwork_index(filename)")
    # Lookup pojedynczego wpisu po ścieżce (WHERE rel_path=?) był pełnym skanem 31k wierszy.
    db.execute("CREATE INDEX IF NOT EXISTS idx_artwork_index_rel_path ON artwork_index(rel_path)")
    db.commit()
    # #2 FTS: na Postgresie (prod, 31k) B-tree NIE przyspiesza `LIKE '%..%'` — pg_trgm GIN
    # tak. Zakłada rozszerzenie pg_trgm i indeksy GIN; wyszukiwarka browse() (LIKE) staje
    # się szybka bez zmiany zapytań. Na SQLite (dev) pomijamy — LIKE wystarcza przy małej
    # bazie. Każdy krok osobno w try/except (brak uprawnień do EXTENSION nie wywraca reszty).
    if os.environ.get("DATABASE_URL"):
        for _stmt in (
            "CREATE EXTENSION IF NOT EXISTS pg_trgm",
            "CREATE INDEX IF NOT EXISTS idx_artwork_index_filename_trgm "
            "ON artwork_index USING gin (filename gin_trgm_ops)",
            "CREATE INDEX IF NOT EXISTS idx_artwork_index_refcode_trgm "
            "ON artwork_index USING gin (ref_code gin_trgm_ops)",
        ):
            try:
                db.execute(_stmt)
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
    try:
        db._ai_idx_ensured = True
    except (AttributeError, TypeError):
        pass


def _parse_entry(e: dict) -> dict | None:
    """Z wpisu agenta {filename, rel_path, source_mtime} buduje rekord indeksu.
    REF/rewizja wyciągane z nazwy pliku (parse_master_filename)."""
    rel_path = str(e.get("rel_path") or "").strip()
    filename = str(e.get("filename") or "").strip()
    if not rel_path:
        return None
    if not filename:
        filename = rel_path.replace("\\", "/").rsplit("/", 1)[-1]
    parsed = {}
    if parse_master_filename:
        try:
            parsed = parse_master_filename(filename) or {}
        except Exception:
            parsed = {}
    ref_code = str(parsed.get("ref") or "").strip()
    return {
        "ref_norm": normalize_ref(ref_code),
        "ref_code": ref_code[:100],
        "filename": filename[:300],
        "rel_path": rel_path[:600],
        "packaging_type": str(parsed.get("packaging_type") or "")[:40],
        "revision": str(parsed.get("revision") or "")[:40],
        "revision_rank": int(parsed.get("revision_rank") or 0),
        "ean": str(parsed.get("ean") or "")[:14],
        "source_mtime": str(e.get("source_mtime") or "")[:32],
    }


# ── Aliasy: „fragment nazwy pliku → REF” (gdy nazwa nie zawiera REF) ───────────
def ensure_alias_table(db) -> None:
    if getattr(db, "_ai_alias_ensured", False):
        return
    db.execute("""CREATE TABLE IF NOT EXISTS artwork_alias (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ref_norm TEXT NOT NULL,
        ref_code TEXT DEFAULT '',
        pattern TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(ref_norm, pattern)
    )""")
    db.commit()
    try:
        db._ai_alias_ensured = True
    except (AttributeError, TypeError):
        pass


def _load_aliases(db) -> list:
    ensure_alias_table(db)
    return [dict(r) for r in db.execute(
        "SELECT ref_norm, ref_code, pattern FROM artwork_alias").fetchall()]


def add_alias(db, ref_code, pattern, replace: bool = False) -> dict:
    """Dodaje alias i backfilluje istniejące wpisy indeksu pasujące do wzorca.

    replace=True → CZYSTA PODMIANA wyboru dla danego REF: usuwa wcześniejsze aliasy
    tego REF i cofa wcześniejszy backfill (ref_norm=R → ''), zanim doda nowy. Dzięki
    temu błędny wybór mastera można nadpisać innym plikiem bez „duchów" w indeksie."""
    ensure_table(db)
    ensure_alias_table(db)
    rn = normalize_ref(ref_code)
    pat = str(pattern or "").strip()
    if not rn or not pat:
        return {"error": "Podaj REF i fragment nazwy pliku"}
    if replace:
        db.execute("DELETE FROM artwork_alias WHERE ref_norm=?", (rn,))
        # Cofnij wcześniejszy backfill — wpisy indeksu wskazujące na ten REF wracają
        # do stanu „bez REF", by stara propozycja nie wygrywała z nową przy lookup.
        db.execute("UPDATE artwork_index SET ref_norm='', ref_code='' WHERE ref_norm=?", (rn,))
    db.execute(
        "INSERT INTO artwork_alias(ref_norm, ref_code, pattern) VALUES(?,?,?) "
        "ON CONFLICT(ref_norm, pattern) DO NOTHING", (rn, str(ref_code)[:100], pat[:300]))
    esc = pat.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    cur = db.execute(
        "UPDATE artwork_index SET ref_norm=?, ref_code=? WHERE filename LIKE ? ESCAPE '\\'",
        (rn, str(ref_code)[:100], f"%{esc}%"))
    db.commit()
    return {"ok": True, "matched": cur.rowcount}


def list_aliases(db) -> list:
    ensure_alias_table(db)
    return [dict(r) for r in db.execute(
        "SELECT id, ref_code, ref_norm, pattern FROM artwork_alias ORDER BY ref_code LIMIT 1000"
    ).fetchall()]


def delete_alias(db, alias_id) -> dict:
    ensure_alias_table(db)
    try:
        aid = int(alias_id)
    except (TypeError, ValueError):
        return {"error": "Złe id"}
    db.execute("DELETE FROM artwork_alias WHERE id=?", (aid,))
    db.commit()
    return {"ok": True}


def sync_entries(db, entries: list) -> dict:
    """Upsert paczki wpisów z agenta (po rel_path). Zwraca {indexed, skipped}."""
    ensure_table(db)
    aliases = _load_aliases(db)
    indexed = skipped = 0
    for e in entries or []:
        rec = _parse_entry(e)
        if not rec:
            skipped += 1
            continue
        # Zastosuj alias: jeśli fragment nazwy pasuje, nadpisz REF.
        if aliases:
            _fn = rec["filename"].lower()
            for a in aliases:
                if a["pattern"].lower() in _fn:
                    rec["ref_norm"] = a["ref_norm"]
                    rec["ref_code"] = a["ref_code"]
                    break
        db.execute(
            "INSERT INTO artwork_index(ref_norm, ref_code, filename, rel_path, "
            "packaging_type, revision, revision_rank, ean, source_mtime, scanned_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(rel_path) DO UPDATE SET ref_norm=excluded.ref_norm, "
            "ref_code=excluded.ref_code, filename=excluded.filename, "
            "packaging_type=excluded.packaging_type, revision=excluded.revision, "
            "revision_rank=excluded.revision_rank, ean=excluded.ean, "
            "source_mtime=excluded.source_mtime, scanned_at=datetime('now')",
            (rec["ref_norm"], rec["ref_code"], rec["filename"], rec["rel_path"],
             rec["packaging_type"], rec["revision"], rec["revision_rank"],
             rec["ean"], rec["source_mtime"]),
        )
        indexed += 1
    db.commit()
    return {"indexed": indexed, "skipped": skipped}


def _norm_mtime(s) -> str:
    """Data → sortowalne ISO (YYYY-MM-DD HH:MM:SS). Obsługuje polski eksport
    Windows 'DD.MM.YYYY HH:MM[:SS]' (LastWriteTime)."""
    s = str(s or "").strip()
    if not s:
        return ""
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?", s)
    if m:
        d, mo, y, hh, mm, ss = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d} {int(hh or 0):02d}:{int(mm or 0):02d}:{int(ss or 0):02d}"
    return s[:32]


def import_listing(db, rows: list) -> dict:
    """Zasila indeks z wierszy tabeli (CSV/XLSX) LUB listy ścieżek. Wykrywa kolumnę
    ze ścieżką (FullName/Path/Ścieżka…), opcjonalnie datę modyfikacji i flagę
    folderu — bierze tylko PDF-y, foldery pomija. Bez nagłówka: każda komórka to
    kandydat. Wysyłamy wyłącznie nazwę + ścieżkę (+ mtime), bez treści plików."""
    rows = [list(r) for r in (rows or []) if r is not None]
    if not rows:
        return {"indexed": 0, "skipped": 0, "total_lines": 0, "candidates": 0}
    header = [str(c or "").strip().lower() for c in rows[0]]

    def _find(*needles):
        return next((i for i, h in enumerate(header)
                     if any(n in h for n in needles)), None)
    path_i = _find("fullname", "rel_path", "ścież", "sciez", "path", "plik", "filename")
    has_header = path_i is not None
    mtime_i = _find("write", "modif", "mtime", "data", "zmod") if has_header else None
    folder_i = _find("isfolder", "folder", "katalog", "is_dir") if has_header else None
    data = rows[1:] if has_header else rows

    entries, total = [], 0
    for r in data:
        total += 1
        if has_header:
            if folder_i is not None and folder_i < len(r) and \
               str(r[folder_i]).strip().lower() in ("true", "1", "prawda", "tak", "yes"):
                continue
            cells = [(str(r[path_i]) if path_i < len(r) else "",
                      str(r[mtime_i]) if (mtime_i is not None and mtime_i < len(r)) else "")]
        else:
            cells = [(str(c or ""), "") for c in r]
        for cell, mtime in cells:
            p = cell.replace("﻿", "").strip().strip('"').strip()
            if not p:
                continue
            norm = p.replace("\\", "/")
            fn = norm.rsplit("/", 1)[-1].strip()
            if not fn.lower().endswith(".pdf"):
                continue
            entries.append({"filename": fn[:300], "rel_path": norm[:600],
                            "source_mtime": _norm_mtime(mtime)})
    res = sync_entries(db, entries)
    res["total_lines"] = total
    res["candidates"] = len(entries)
    return res


def import_paths(db, paths: list) -> dict:
    """Zasila indeks z listy ścieżek (jedna na linię, np. `dir /s /b`)."""
    return import_listing(db, [[p] for p in (paths or [])])


def index_stats(db) -> dict:
    """Statystyki indeksu: ile plików, ile z rozpoznanym REF, ile unikalnych REF,
    ostatni skan. Do panelu „Indeks artworków" (czy sync w ogóle się odbył)."""
    ensure_table(db)
    row = db.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN ref_norm<>'' THEN 1 ELSE 0 END) AS with_ref, "
        "COUNT(DISTINCT CASE WHEN ref_norm<>'' THEN ref_norm END) AS refs, "
        "MAX(scanned_at) AS last_scan FROM artwork_index"
    ).fetchone()
    return {
        "total": (row["total"] or 0) if row else 0,
        "with_ref": (row["with_ref"] or 0) if row else 0,
        "distinct_refs": (row["refs"] or 0) if row else 0,
        "last_scan": (row["last_scan"] or "") if row else "",
    }


def packaging_types(db) -> list:
    """Lista niepustych typów opakowań w indeksie (do filtra biblioteki)."""
    ensure_table(db)
    return [r[0] for r in db.execute(
        "SELECT DISTINCT packaging_type FROM artwork_index "
        "WHERE packaging_type<>'' ORDER BY packaging_type"
    ).fetchall()]


# ponytail: cap the q-present in-memory rerank at 500 WHERE-matched candidates;
# beyond it fall back to SQL LIMIT/OFFSET (pin CASE still applies) — raise only
# if a real query needs a bigger candidate window (T-02-02 DoS backstop).
_RERANK_CAP = 500


def browse(db, q="", packaging="", current_only=False, hide_ignored=False,
           variant="", marm_linked=False, page=1, per_page=50) -> dict:
    """Przeszukiwalna, stronicowana lista masterów (biblioteka, 31k plików).

    q          — fragment nazwy pliku / REF (LIKE substring; ref też po ref_norm)
    packaging  — filtr po packaging_type (dokładny)
    current_only — tylko najnowsza rewizja per REF (odrzuca starsze revision_rank)

    SRCH-01 (D-02): gdy q niepuste, dokładna równość ref_norm=normalize_ref(q)
    jest twardym pinem na wiersz 1 strony 1 (SQL ORDER BY CASE — ORDER BY działa
    na całym dopasowanym zbiorze PRZED LIMIT/OFFSET, więc pin nie wymaga osobnego
    zapytania). Reszta wyników sortowana rapidfuzz-em w Pythonie na PEŁNYM
    dopasowanym zbiorze (nie na już-postronicowanej stronie — RESEARCH Pitfall 1),
    ograniczonym do _RERANK_CAP kandydatów; powyżej cap — fallback na czyste
    SQL LIMIT/OFFSET (pin nadal działa, tylko bez fuzzy dogrywki reszty).

    SRCH-02 (D-03): każdy zwrócony wiersz dostaje has_3d (+ has_dieline/has_marm/
    has_photo) — JEDNO zbiorcze zapytanie do artwork_render_registry po ref_norm
    dla wierszy TEJ strony, nigdy per-wiersz.

    Zwraca {rows, total, page, pages, per_page, packaging_types}.
    """
    ensure_table(db)
    where, params = [], []
    q = (q or "").strip()
    qn = ""
    if q:
        from db import escape_like
        qn = normalize_ref(q)          # tylko [A-Z0-9] → bez wildcardów LIKE
        qe = escape_like(q)            # literalne %/_/\ (ESCAPE '\'); jedno źródło w db.py
        where.append("(filename LIKE ? ESCAPE '\\' OR ref_code LIKE ? ESCAPE '\\' "
                     "OR ref_norm LIKE ?)")
        params += [f"%{qe}%", f"%{qe}%", f"%{qn}%"]
    if packaging:
        where.append("packaging_type = ?")
        params.append(packaging)
    # FACET-01: filtr po wariancie renderu 3D (ma dieline/marm/photo w rejestrze).
    variant = (variant or "").strip()
    from constants import RenderVariant as _RV
    if variant in {v.value for v in _RV}:
        where.append(
            "EXISTS (SELECT 1 FROM artwork_render_registry r "
            "WHERE r.ref_norm=artwork_index.ref_norm AND artwork_index.ref_norm<>'' "
            "AND r.variant=?)")
        params.append(variant)
    # FACET-01: filtr „ma powiązanie MARM" (artwork_marm_link po REF).
    if marm_linked:
        where.append(
            "EXISTS (SELECT 1 FROM artwork_marm_link l "
            "WHERE l.art_ref_norm=artwork_index.ref_norm AND artwork_index.ref_norm<>'')")
    if current_only:
        # Odrzuć rekord, jeśli dla tego samego REF istnieje wyższy revision_rank.
        where.append(
            "NOT EXISTS (SELECT 1 FROM artwork_index b WHERE b.ref_norm=artwork_index.ref_norm "
            "AND b.ref_norm<>'' AND b.revision_rank>artwork_index.revision_rank)"
        )
    if hide_ignored:
        # Ukryj pliki ręcznie oznaczone do pominięcia (Faza 4 portalu zarządzania).
        # Lazy import — cykl tylko przy imporcie modułu; tu wykonuje się dopiero w runtime.
        import artwork_manage as _am
        _am.ensure_status_table(db)
        where.append(
            "NOT EXISTS (SELECT 1 FROM artwork_file_status s "
            "WHERE s.rel_path=artwork_index.rel_path AND s.status IN ('ignored','archived'))"
        )
    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
    total = db.execute(f"SELECT COUNT(*) AS c FROM artwork_index{wsql}", params).fetchone()["c"]  # nosec B608
    try:
        per_page = max(1, min(200, int(per_page or 50)))
        page = max(1, int(page or 1))
    except (TypeError, ValueError):
        per_page, page = 50, 1
    offset = (page - 1) * per_page

    select_cols = ("ref_norm, ref_code, filename, rel_path, packaging_type, "
                   "revision, revision_rank, ean")
    order_params = []
    order_sql = "ORDER BY "
    if qn:
        # Exact-ref_norm hard pin (D-02): an exact match always also satisfies
        # the WHERE's `ref_norm LIKE '%qn%'` term, so it's guaranteed present
        # in the ordered set and — CASE value 0, the lowest — guaranteed row 1.
        order_sql += "CASE WHEN ref_norm = ? THEN 0 ELSE 1 END, "
        order_params.append(qn)
    order_sql += "ref_code, revision_rank DESC, filename"

    if qn:
        candidates = [dict(r) for r in db.execute(
            # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
            f"SELECT {select_cols} FROM artwork_index{wsql} {order_sql} LIMIT ?",  # nosec B608
            params + order_params + [_RERANK_CAP + 1]
        ).fetchall()]
        if len(candidates) > _RERANK_CAP:
            # Candidate set too large to rerank in memory — SQL LIMIT/OFFSET
            # fallback; the exact-pin CASE still applies, only the rapidfuzz
            # secondary ordering of the rest is skipped for this query.
            rows = [dict(r) for r in db.execute(
                # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
                f"SELECT {select_cols} FROM artwork_index{wsql} {order_sql} "  # nosec B608
                "LIMIT ? OFFSET ?", params + order_params + [per_page, offset]
            ).fetchall()]
        else:
            from rapidfuzz import fuzz

            def _rank_key(row):
                pinned = 0 if row["ref_norm"] == qn else 1
                score = 100.0 if pinned == 0 else fuzz.ratio(qn, row["ref_norm"] or "")
                return (pinned, -score, row["ref_code"] or "",
                        -(row["revision_rank"] or 0), row["filename"] or "")

            candidates.sort(key=_rank_key)
            rows = candidates[offset:offset + per_page]
    else:
        rows = [dict(r) for r in db.execute(
            # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
            f"SELECT {select_cols} FROM artwork_index{wsql} {order_sql} "  # nosec B608
            "LIMIT ? OFFSET ?", params + order_params + [per_page, offset]
        ).fetchall()]

    _attach_has_3d(db, rows)
    _attach_status(db, rows)

    pages = (total + per_page - 1) // per_page if per_page else 1
    # Lista typów opakowań zmienia się tylko przy sync — licz DISTINCT tylko dla 1. strony
    # (UI wczytuje ją raz), by paginacja nie robiła skanu 31k wierszy na każde kliknięcie.
    return {"rows": rows, "total": total, "page": page, "pages": pages,
            "per_page": per_page,
            "packaging_types": packaging_types(db) if page == 1 else []}


def _attach_status(db, rows: list) -> None:
    """Ustawia row['status'] (active/ignored/archived) dla wierszy strony — JEDNO
    zbiorcze zapytanie po rel_path, nigdy per-wiersz. Domyślnie 'active' (brak wpisu).
    Faza 4 portalu zarządzania. Cichy fallback, gdy tabela statusów jeszcze nie istnieje."""
    for r in rows:
        r["status"] = "active"
    paths = [r["rel_path"] for r in rows if r.get("rel_path")]
    if not paths:
        return
    ph = ",".join(["?"] * len(paths))
    try:
        found = db.execute(
            # Bandit B608: interpolowane są tylko placeholdery ? (liczba = długość listy); wartości jako parametry.
            f"SELECT rel_path, status FROM artwork_file_status WHERE rel_path IN ({ph})",  # nosec B608
            paths
        ).fetchall()
    except Exception:
        return
    smap = {row[0]: row[1] for row in found}
    for r in rows:
        if r.get("rel_path") in smap:
            r["status"] = smap[r["rel_path"]] or "active"


def _attach_has_3d(db, rows: list) -> None:
    """SRCH-02/D-03: sets has_3d/has_dieline/has_marm/has_photo on each row in
    place via ONE batched `ref_norm IN (...)` join to artwork_render_registry
    for the page's distinct non-empty ref_norms — never a per-row query.
    Mirrors artwork_render_registry.coverage()'s batched-join shape, ref_norm-
    only (artwork_index has no fid, D-04/Phase-1)."""
    from constants import RenderVariant
    refnorms = sorted({r["ref_norm"] for r in rows if r.get("ref_norm")})
    by_refnorm: dict = {}
    if refnorms:
        reg_rows = db.execute(
            # Bandit B608: interpolowane są tylko placeholdery ? (liczba = długość listy); wartości jako parametry.
            "SELECT ref_norm, variant FROM artwork_render_registry "  # nosec B608
            f"WHERE ref_norm IN ({','.join('?' * len(refnorms))})", refnorms
        ).fetchall()
        for rr in reg_rows:
            if rr["ref_norm"]:
                by_refnorm.setdefault(rr["ref_norm"], set()).add(rr["variant"])
    for r in rows:
        variants = by_refnorm.get(r.get("ref_norm") or "", set())
        r["has_dieline"] = RenderVariant.DIELINE.value in variants
        r["has_marm"] = RenderVariant.MARM.value in variants
        r["has_photo"] = RenderVariant.PHOTO.value in variants
        r["has_3d"] = bool(variants)


def lookup_best(db, ref, level=None) -> dict | None:
    """Najnowsza rewizja mastera dla danego REF (po revision_rank, potem mtime).
    Gdy podano `level`, wybiera plik pasujący do poziomu opakowania (carton dla
    KAR, pouch dla OP); gdy tylko pliki innego poziomu istnieją → None."""
    ensure_table(db)
    rn = normalize_ref(ref)
    if not rn:
        return None
    rows = [dict(r) for r in db.execute(
        "SELECT * FROM artwork_index WHERE ref_norm=? "
        "ORDER BY revision_rank DESC, source_mtime DESC, id DESC LIMIT 50", (rn,)
    ).fetchall()]
    best = _pick_level(rows, level)
    if best:
        return best
    # Fallback przez aliasy: znajdź plik po fragmencie nazwy zmapowanym do tego REF.
    try:
        ensure_alias_table(db)
        pats = db.execute("SELECT pattern FROM artwork_alias WHERE ref_norm=?", (rn,)).fetchall()
        alias_rows = []
        for pr in pats:
            pat = pr[0]
            esc = str(pat).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            alias_rows += [dict(r) for r in db.execute(
                "SELECT * FROM artwork_index WHERE filename LIKE ? ESCAPE '\\' "
                "ORDER BY revision_rank DESC, source_mtime DESC, id DESC LIMIT 20", (f"%{esc}%",)
            ).fetchall()]
        return _pick_level(alias_rows, level)
    except Exception:
        pass
    return None


def all_revisions(db, ref) -> list:
    """Wszystkie rewizje/pliki dla REF (do ręcznej korekty wyboru #6)."""
    ensure_table(db)
    rn = normalize_ref(ref)
    if not rn:
        return []
    rows = db.execute(
        "SELECT * FROM artwork_index WHERE ref_norm=? "
        "ORDER BY revision_rank DESC, source_mtime DESC, id DESC LIMIT 50", (rn,)
    ).fetchall()
    return [dict(r) for r in rows]


_STOP_TOKENS = {"the", "and", "dla", "do", "na", "a", "pdf", "rev", "ed", "wer"}


def _word_tokens(s) -> list:
    """Realne słowa z tekstu (bez sztucznych bigramów) — do prefiltru SQL LIKE,
    bo tylko one występują dosłownie w nazwach plików."""
    raw = [t for t in re.split(r"[^a-z0-9]+", str(s or "").lower()) if t]
    return [t for t in raw if len(t) >= 2 and t not in _STOP_TOKENS]


def _tok(s) -> set:
    """Tokeny do dopasowania nazw. Ulepszenia:
    - sufiks ilości w opakowaniu: A8↔8, A10↔10,
    - bigramy sąsiednich słów: 'bed bath' ↔ 'bedbath' (łączenie/rozdzielenie)."""
    words = _word_tokens(s)
    toks = set(words)
    extra = set()
    for t in words:
        m = re.fullmatch(r"a(\d{1,4})", t)   # A8 / A10 / A25 → też 8/10/25
        if m:
            extra.add(m.group(1))
        elif t.isdigit():                     # 8 → też a8
            extra.add("a" + t)
    for i in range(len(words) - 1):           # sklejone sąsiednie słowa
        if words[i].isalpha() and words[i + 1].isalpha():
            extra.add(words[i] + words[i + 1])
    return toks | extra


def suggest_by_text(db, text, limit: int = 10, level=None) -> list:
    """Proponuje pliki artworków najlepiej pasujące do tekstu (np. opisu produktu),
    po podobieństwie tokenów nazwy pliku. Zwraca listę kandydatów ze score.
    Gdy podano `level`, podbija pliki pasujące do poziomu (carton dla KAR) i
    obniża pliki innego poziomu (pouch dla KAR), żeby właściwy wypłynął na górę."""
    ensure_table(db)
    toks = _tok(text)
    if not toks:
        return []
    # Prefiltr SQL po najdłuższych REALNYCH słowach (nie sztucznych bigramach/kodach,
    # które nie pojawiają się dosłownie w nazwie pliku z separatorami). 4 słowa, by
    # zwiększyć szansę trafienia gdy najdłuższe słowo akurat nie jest w nazwie.
    key = sorted(set(_word_tokens(text)), key=len, reverse=True)[:4]
    if not key:
        key = sorted(toks, key=len, reverse=True)[:3]
    seen = {}
    for k in key:
        esc = k.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        for r in db.execute(
            "SELECT id, filename, ref_code, rel_path, revision, source_mtime FROM artwork_index "
            "WHERE filename LIKE ? ESCAPE '\\' LIMIT 400", (f"%{esc}%",)
        ).fetchall():
            seen[r["id"]] = dict(r)
    scored = []
    for d in seen.values():
        ft = _tok(d["filename"])
        if not ft:
            continue
        inter = len(toks & ft)
        if inter < 2:
            continue
        score = inter / len(toks | ft)   # Jaccard
        scored.append((score, inter, d))
    # Korekta wg poziomu opakowania: +0.30 gdy nazwa pasuje do poziomu, −0.30 gdy
    # pasuje do INNEGO poziomu (np. pouch przy szukaniu kartonu).
    _kw = _level_keywords(level)
    _other = _other_level_keywords(level)
    if _kw or _other:
        adj = []
        for s, i, d in scored:
            fn = str(d.get("filename", "")).lower()
            if _kw and any(k in fn for k in _kw):
                s += 0.30
            elif _other and any(k in fn for k in _other):
                s -= 0.30
            adj.append((s, i, d))
        scored = adj
    # Sort: najpierw trafność (score), potem najnowsze pliki (data modyfikacji
    # malejąco), na końcu liczba wspólnych tokenów. Sortowanie najpierw po mtime
    # (string, bywa puste) zakopywało trafne pliki.
    scored.sort(key=lambda x: (x[0], (x[2].get("source_mtime") or ""), x[1]), reverse=True)
    return [{"filename": d["filename"], "rel_path": d["rel_path"],
             "ref_code": d["ref_code"], "revision": d.get("revision", ""),
             "source_mtime": d.get("source_mtime", "") or "",
             "score": round(s, 2), "shared": i} for s, i, d in scored[:limit]]


# ── Potwierdzone mapowania artworków + wykrywanie zmiany rewizji ───────────────
def ensure_confirmed_table(db) -> None:
    if getattr(db, "_ai_conf_ensured", False):
        return
    db.execute("""CREATE TABLE IF NOT EXISTS artwork_confirmed (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ref_norm TEXT UNIQUE NOT NULL,
        ref_code TEXT DEFAULT '',
        rel_path TEXT DEFAULT '',
        filename TEXT DEFAULT '',
        revision TEXT DEFAULT '',
        revision_rank INTEGER DEFAULT 0,
        confirmed_by TEXT DEFAULT '',
        confirmed_at TEXT DEFAULT (datetime('now'))
    )""")
    db.commit()
    try:
        db._ai_conf_ensured = True
    except (AttributeError, TypeError):
        pass


_MFILE_DATA_COL_ENSURED = False   # czy próbowano już ALTER ADD COLUMN data (raz na proces)


def ensure_master_file_table(db) -> None:
    """Składnica bajtów masterów. ŹRÓDŁO PRAWDY = baza (kolumna `data` BLOB/BYTEA),
    trwała i backupowana — przeżywa redeploye. Dysk (`uploads/artwork_masters`) to
    tylko cache, odtwarzany z bazy na żądanie."""
    if getattr(db, "_ai_mfile_ensured", False):
        return
    db.execute("""CREATE TABLE IF NOT EXISTS artwork_master_file (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rel_path TEXT UNIQUE NOT NULL,
        stored_name TEXT NOT NULL,
        content_type TEXT DEFAULT 'application/pdf',
        size INTEGER DEFAULT 0,
        data BLOB,
        uploaded_at TEXT DEFAULT (datetime('now'))
    )""")
    db.commit()
    # Dla istniejących instalacji — dołóż kolumnę `data` (idempotentnie, PG/SQLite).
    # Próba ALTER tylko RAZ na proces (na PG kolejne próby i tak by padały — szum).
    global _MFILE_DATA_COL_ENSURED
    if not _MFILE_DATA_COL_ENSURED:
        try:
            db.execute("ALTER TABLE artwork_master_file ADD COLUMN data BLOB")
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
        _MFILE_DATA_COL_ENSURED = True
    try:
        db._ai_mfile_ensured = True
    except (AttributeError, TypeError):
        pass


def available_master_paths(db) -> set:
    """rel_path masterów, których BAJTY mamy w bazie (gotowe do podglądu)."""
    ensure_master_file_table(db)
    return {r["rel_path"] for r in db.execute(
        "SELECT rel_path FROM artwork_master_file WHERE data IS NOT NULL").fetchall()}


def wanted_master_paths(db) -> list:
    """rel_path potrzebne do podglądu (potwierdzone + przypisane do grup),
    których jeszcze nie mamy w bazie — agent je dosyła."""
    ensure_confirmed_table(db)
    ensure_groups_table(db)
    have = available_master_paths(db)
    want = set()
    for r in db.execute(
            "SELECT DISTINCT rel_path FROM artwork_confirmed WHERE COALESCE(rel_path,'')<>''").fetchall():
        want.add(r["rel_path"])
    for r in db.execute(
            "SELECT master_rel_path FROM artwork_mapping_group "
            "WHERE COALESCE(master_rel_path,'')<>''").fetchall():
        want.add(r["master_rel_path"])
    return sorted(want - have)


def record_master_file(db, rel_path, stored_name, content_type="application/pdf",
                       size=0, data=None) -> None:
    """Zapisz master do bazy (bajty w kolumnie `data`) — trwale, niezależnie od dysku.
    Niezmiennik: wpis ZAWSZE ma bajty. Bez bajtów nie tworzymy/aktualizujemy rekordu
    (inaczej powstałby wpis 'niedostępny' i agent ponawiałby go w nieskończoność)."""
    ensure_master_file_table(db)
    blob = data
    if blob is not None and not isinstance(blob, (bytes, bytearray)):
        blob = bytes(blob)
    if not blob:
        raise ValueError("record_master_file: brak bajtów (data) — wpis bez treści jest niedozwolony")
    blob = bytes(blob)
    db.execute(
        "INSERT INTO artwork_master_file(rel_path, stored_name, content_type, size, data, uploaded_at) "
        "VALUES(?,?,?,?,?, datetime('now')) ON CONFLICT(rel_path) DO UPDATE SET "
        "stored_name=excluded.stored_name, content_type=excluded.content_type, "
        "size=excluded.size, data=excluded.data, uploaded_at=datetime('now')",
        (str(rel_path)[:600], stored_name, content_type or "application/pdf",
         int(size or 0), blob))
    db.commit()


def master_file_for(db, rel_path) -> dict | None:
    ensure_master_file_table(db)
    r = db.execute(
        "SELECT stored_name, content_type FROM artwork_master_file WHERE rel_path=?",
        (str(rel_path or ""),)).fetchone()
    return dict(r) if r else None


def master_file_blob(db, rel_path):
    """Zwraca (content_type, bytes) z bazy albo None — do serwowania/odtwarzania cache."""
    ensure_master_file_table(db)
    r = db.execute(
        "SELECT content_type, data FROM artwork_master_file WHERE rel_path=?",
        (str(rel_path or ""),)).fetchone()
    if not r or r["data"] is None:
        return None
    data = r["data"]
    if not isinstance(data, bytes):
        data = bytes(data)
    return (r["content_type"] or "application/pdf", data)



def confirm_artwork(db, ref, rel_path, user: str = "", level: str = "") -> dict:
    """Zapamiętuje, że dla danego REF (+ poziomu) dany plik (rel_path) to właściwy
    artwork. Pobiera rewizję/rank z indeksu (po rel_path)."""
    ensure_table(db)
    ensure_confirmed_table(db)
    rn = normalize_ref(ref)
    rp = str(rel_path or "").strip()
    if not rn or not rp:
        return {"error": "Podaj REF i plik"}
    key = _conf_key(ref, level)
    row = db.execute(
        "SELECT filename, revision, revision_rank FROM artwork_index WHERE rel_path=?", (rp,)
    ).fetchone()
    fn = row["filename"] if row else ""
    rev = row["revision"] if row else ""
    rank = int(row["revision_rank"] or 0) if row else 0
    db.execute(
        "INSERT INTO artwork_confirmed(ref_norm, ref_code, rel_path, filename, revision, "
        "revision_rank, confirmed_by, confirmed_at) VALUES(?,?,?,?,?,?,?,datetime('now')) "
        "ON CONFLICT(ref_norm) DO UPDATE SET ref_code=excluded.ref_code, rel_path=excluded.rel_path, "
        "filename=excluded.filename, revision=excluded.revision, revision_rank=excluded.revision_rank, "
        "confirmed_by=excluded.confirmed_by, confirmed_at=datetime('now')",
        (key, str(ref)[:100], rp[:600], fn, rev, rank, str(user)[:80]),
    )
    db.commit()
    return {"ok": True, "ref_code": str(ref), "filename": fn, "revision": rev,
            "level": _norm_level(level)}


def unconfirm_artwork(db, ref, level: str = "") -> dict:
    """Cofa potwierdzenie dla (REF, poziom) — żeby dało się poprawić błędny wybór."""
    ensure_confirmed_table(db)
    conf = get_confirmed(db, ref, level)
    if not conf:
        return {"ok": True, "removed": 0}
    db.execute("DELETE FROM artwork_confirmed WHERE ref_norm=?", (conf.get("ref_norm"),))
    db.commit()
    return {"ok": True, "removed": 1}


def get_confirmed(db, ref, level: str = "") -> dict | None:
    ensure_confirmed_table(db)
    base = normalize_ref(ref)
    if not base:
        return None
    lv = _norm_level(level)
    # 1. Dokładne potwierdzenie per-poziom.
    if lv:
        row = db.execute("SELECT * FROM artwork_confirmed WHERE ref_norm=?",
                         (_conf_key(ref, lv),)).fetchone()
        if row:
            return dict(row)
    # 2. Stare potwierdzenie (bez poziomu) — użyj tylko gdy NIE należy do innego
    #    poziomu (np. nie pokazuj zapisanego „pouch" jako master dla kartonu).
    row = db.execute("SELECT * FROM artwork_confirmed WHERE ref_norm=?", (base,)).fetchone()
    if not row:
        return None
    if lv:
        fn = str(row["filename"] or "").lower()
        if (any(k in fn for k in _other_level_keywords(level))
                and not any(k in fn for k in _level_keywords(level))):
            return None
    return dict(row)


def lookup_for_comparison(db, ref, level: str = "") -> dict:
    """Sterownik flow porównania:
      - newest: najnowsza rewizja z indeksu (dla danego poziomu),
      - confirmed: potwierdzony plik (lub None),
      - needs_confirmation: brak potwierdzenia → zapytaj usera,
      - revision_changed: potwierdzony jest STARSZY niż najnowszy w indeksie."""
    newest = lookup_best(db, ref, level)
    confirmed = get_confirmed(db, ref, level)
    # Dryf rewizji: najlepszy master w indeksie jest NOWSZY niż potwierdzony —
    # wyższy revision_rank, albo (przy równym rank) nowsza data modyfikacji.
    # Sam inny rel_path NIE wystarcza (mógł być nowszy-potwierdzony lub równy rank).
    def _drifted(nw, cf) -> bool:
        if not (nw and cf) or nw.get("rel_path") == cf.get("rel_path"):
            return False
        nr = int(nw.get("revision_rank") or 0)
        cr = int(cf.get("revision_rank") or 0)
        if nr != cr:
            return nr > cr
        return (nw.get("source_mtime") or "") > (cf.get("source_mtime") or "")
    revision_changed = _drifted(newest, confirmed)
    # Fallback przez GRUPĘ MAPOWAŃ: gdy REF nie ma własnego mastera (ani potwierdzonego,
    # ani w indeksie), dziedziczy master z grupy, do której należy. Bezpośredni master
    # REF-a ma pierwszeństwo nad masterem grupy.
    group_master = None
    if newest is None and confirmed is None:
        try:
            group_master = lookup_group_master(db, ref, level)
        except Exception:
            group_master = None
    return {
        "newest": newest,
        "confirmed": confirmed,
        "needs_confirmation": confirmed is None,
        "revision_changed": revision_changed,
        "group_master": group_master,
    }


# ── Grupy mapowań artworków (grupa → lista REF + jeden master) ──────────────────
def ensure_groups_table(db) -> None:
    """Tabela grup mapowań. Memoizacja per-połączenie (jak artwork_index)."""
    if getattr(db, "_ai_grp_ensured", False):
        return
    db.execute("""CREATE TABLE IF NOT EXISTS artwork_mapping_group (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_name TEXT NOT NULL,
        master_rel_path TEXT DEFAULT '',
        refs_json TEXT DEFAULT '[]',
        created_by INTEGER,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    )""")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_artwork_group_name "
               "ON artwork_mapping_group(group_name)")
    db.commit()
    try:
        db._ai_grp_ensured = True
    except (AttributeError, TypeError):
        pass


def _master_record_by_path(db, rel_path):
    """Rekord indeksu (filename/revision/...) dla danej ścieżki mastera albo None."""
    if not rel_path:
        return None
    r = db.execute("SELECT * FROM artwork_index WHERE rel_path=? LIMIT 1", (rel_path,)).fetchone()
    return dict(r) if r else None


def _group_master_view(db, rel_path, group_name="", group_id=None) -> dict | None:
    """Buduje rekord mastera grupy zgodny kształtem z indeksem (rel_path/filename/revision)."""
    mp = str(rel_path or "")
    if not mp:
        return None
    rec = _master_record_by_path(db, mp)
    out = dict(rec) if rec else {"rel_path": mp, "filename": os.path.basename(mp), "revision": ""}
    out["via_group"] = True
    out["group_name"] = group_name
    out["group_id"] = group_id
    return out


def list_groups(db) -> list:
    ensure_groups_table(db)
    out = []
    for r in db.execute(
            "SELECT * FROM artwork_mapping_group ORDER BY group_name").fetchall():
        d = dict(r)
        try:
            refs = json.loads(d.get("refs_json") or "[]")
        except Exception:
            refs = []
        rec = _master_record_by_path(db, d.get("master_rel_path"))
        out.append({
            "id": d["id"],
            "group_name": d["group_name"],
            "master_rel_path": d.get("master_rel_path") or "",
            "master_filename": (rec or {}).get("filename")
                or (os.path.basename(d["master_rel_path"]) if d.get("master_rel_path") else ""),
            "master_revision": (rec or {}).get("revision") or "",
            "master_in_index": rec is not None,
            "refs": refs,
            "refs_count": len(refs),
        })
    return out


def save_group(db, group_name, master_rel_path, refs, created_by=None, gid=None) -> int:
    """Tworzy/aktualizuje grupę. REF-y deduplikowane po formie znormalizowanej."""
    ensure_groups_table(db)
    name = str(group_name or "").strip()[:120]
    if not name:
        raise ValueError("Nazwa grupy jest wymagana")
    norm, seen = [], set()
    for r in (refs or []):
        rc = str(r or "").strip()
        if not rc:
            continue
        rn = normalize_ref(rc)
        if not rn or rn in seen:
            continue
        seen.add(rn)
        norm.append(rc)
    refs_json = json.dumps(norm, ensure_ascii=False)
    mp = str(master_rel_path or "").strip()
    if gid:
        db.execute(
            "UPDATE artwork_mapping_group SET group_name=?, master_rel_path=?, "
            "refs_json=?, updated_at=datetime('now') WHERE id=?",
            (name, mp, refs_json, gid))
        db.commit()
        return int(gid)
    cur = db.execute(
        "INSERT INTO artwork_mapping_group(group_name, master_rel_path, refs_json, created_by) "
        "VALUES(?,?,?,?)", (name, mp, refs_json, created_by))
    db.commit()
    return cur.lastrowid


def delete_group(db, gid) -> None:
    ensure_groups_table(db)
    db.execute("DELETE FROM artwork_mapping_group WHERE id=?", (gid,))
    db.commit()


def lookup_group_master(db, ref, level: str = "") -> dict | None:
    """Master z grupy, do której należy dany REF (gdy REF nie ma własnego mastera)."""
    ensure_groups_table(db)
    rn = normalize_ref(ref)
    if not rn:
        return None
    for r in db.execute(
            "SELECT id, group_name, master_rel_path, refs_json "
            "FROM artwork_mapping_group").fetchall():
        d = dict(r)
        if not d.get("master_rel_path"):
            continue
        try:
            refs = json.loads(d.get("refs_json") or "[]")
        except Exception:
            refs = []
        if any(normalize_ref(x) == rn for x in refs):
            return _group_master_view(db, d["master_rel_path"], d["group_name"], d["id"])
    return None



def stale_confirmations(db) -> list:
    """REF-y, dla których w indeksie pojawiła się NOWSZA rewizja niż potwierdzona.
    Per-poziom: klucz potwierdzenia może zawierać poziom (REF␟poziom), więc dobór
    najnowszego liczymy przez lookup_best(REF, poziom) — nie prostym JOIN-em."""
    ensure_table(db)
    ensure_confirmed_table(db)
    rows = db.execute(
        "SELECT ref_norm, ref_code, rel_path, filename, revision FROM artwork_confirmed "
        "ORDER BY ref_code LIMIT 2000").fetchall()
    out = []
    for c in rows:
        key = str(c["ref_norm"] or "")
        _, _, lv = key.partition(_LEVEL_SEP)   # poziom z klucza ('' gdy brak)
        ref = c["ref_code"] or key.split(_LEVEL_SEP, 1)[0]
        newest = lookup_best(db, ref, lv or None)
        if not newest or not newest.get("rel_path"):
            continue
        if newest["rel_path"] != c["rel_path"]:
            out.append({
                "ref_code": c["ref_code"], "level": lv,
                "confirmed_path": c["rel_path"], "confirmed_file": c["filename"],
                "confirmed_rev": c["revision"],
                "newest_path": newest.get("rel_path", ""),
                "newest_file": newest.get("filename", ""),
                "newest_rev": newest.get("revision", ""),
            })
        if len(out) >= 1000:
            break
    return out


def suggest_for_refs(db, refs: list) -> dict:
    """Mapa REF→najlepszy master (auto-dobór dla pozycji zamówienia, #6).
    Brak dopasowania → wpis z found=False."""
    out = {}
    for ref in refs or []:
        ref = str(ref or "").strip()
        if not ref or ref in out:
            continue
        best = lookup_best(db, ref)
        out[ref] = {"found": bool(best), "master": best}
    return out
