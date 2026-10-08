"""
artwork_manage.py — warstwa ZARZĄDCZA nad indeksem artworków (portal).

Nic tu nie skanuje Z:\\ ani nie trzyma plików — to czysta logika agregacji na
połączeniu DB (jak artwork_index.py / material_master.py). Spina rozproszone
mechanizmy (indeks, aliasy, rewizje, material_master) w jeden obraz stanu
biblioteki:

  • KPI do pulpitu „Zarządzanie artworkami" (compute_kpis),
  • pliki bez REF do ręcznego wiązania (Faza 2),
  • luki (produkty bez artworku) i duplikaty (Faza 3),
  • trwały status pliku active/ignored/archived (tabela artwork_file_status) —
    rozwiązuje „które można pominąć"; dziś aktualność liczona jest tylko w locie.

Moduł jest „czysty": operuje na db przekazanym z app.py; ensure_status_table
bootstrapuje własną tabelę wzorem artwork_index.ensure_table.
"""
from __future__ import annotations

_VALID_STATUS = ("active", "ignored", "archived")


def ensure_status_table(db) -> None:
    """Trwała flaga statusu pliku (per rel_path). Bootstrap idempotentny."""
    if getattr(db, "_awm_status_ensured", False):
        return
    db.execute("""CREATE TABLE IF NOT EXISTS artwork_file_status (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rel_path TEXT UNIQUE NOT NULL,
        status TEXT DEFAULT 'active',
        note TEXT DEFAULT '',
        updated_by TEXT DEFAULT '',
        updated_at TEXT DEFAULT (datetime('now'))
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_awm_status_status "
               "ON artwork_file_status(status)")
    db.commit()
    try:
        db._awm_status_ensured = True
    except (AttributeError, TypeError):
        pass


def _scalar(db, sql, params=()):
    """Pierwsza kolumna pierwszego wiersza jako int (0 gdy brak / NULL / błąd DB
    np. jeszcze niezasilona tabela material_master)."""
    try:
        row = db.execute(sql, params).fetchone()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return 0
    if not row:
        return 0
    val = row[0]
    return int(val) if val is not None else 0


def count_unbound(db) -> int:
    """Pliki w indeksie bez rozpoznanego REF (kandydaci do ręcznego wiązania)."""
    return _scalar(db, "SELECT COUNT(*) FROM artwork_index WHERE ref_norm=''")


def count_obsolete(db) -> int:
    """Pliki przykryte nowszą rewizją tego samego REF (przestarzałe)."""
    return _scalar(
        db,
        "SELECT COUNT(*) FROM artwork_index a WHERE a.ref_norm<>'' "
        "AND EXISTS (SELECT 1 FROM artwork_index b WHERE b.ref_norm=a.ref_norm "
        "AND b.revision_rank>a.revision_rank)",
    )


def count_duplicates(db) -> int:
    """Grupy (REF, opakowanie, rewizja) występujące w >1 pliku — kandydaci na duplikaty."""
    return _scalar(
        db,
        "SELECT COUNT(*) FROM (SELECT ref_norm, packaging_type, revision_rank "
        "FROM artwork_index WHERE ref_norm<>'' "
        "GROUP BY ref_norm, packaging_type, revision_rank HAVING COUNT(*)>1) t",
    )


def count_gaps(db) -> int:
    """Aktywne produkty z material_master, które nie mają ŻADNEGO pliku w indeksie
    (po ref_norm). Gdy material_master jeszcze nie istnieje/pusta → 0."""
    return _scalar(
        db,
        "SELECT COUNT(*) FROM material_master m WHERE COALESCE(m.active,1)=1 "
        "AND m.ref_norm<>'' AND NOT EXISTS "
        "(SELECT 1 FROM artwork_index a WHERE a.ref_norm=m.ref_norm)",
    )


def count_ignored(db) -> int:
    """Pliki ręcznie oznaczone jako do pominięcia (ignored/archived)."""
    ensure_status_table(db)
    return _scalar(
        db,
        "SELECT COUNT(*) FROM artwork_file_status WHERE status IN ('ignored','archived')",
    )


# ── Faza 2: pliki bez REF + podpowiedź produktu ───────────────────────────────
def list_unbound(db, q="", page=1, per_page=50) -> dict:
    """Stronicowana lista plików w indeksie BEZ rozpoznanego REF (ref_norm='').
    q — opcjonalny fragment nazwy pliku."""
    import artwork_index as _ai
    _ai.ensure_table(db)
    where = ["ref_norm=''"]
    params = []
    q = (q or "").strip()
    if q:
        from db import escape_like
        where.append("filename LIKE ? ESCAPE '\\'")
        params.append(f"%{escape_like(q)}%")
    wsql = " WHERE " + " AND ".join(where)
    try:
        per_page = max(1, min(200, int(per_page or 50)))
        page = max(1, int(page or 1))
    except (TypeError, ValueError):
        per_page, page = 50, 1
    # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
    total = _scalar(db, f"SELECT COUNT(*) FROM artwork_index{wsql}", params)  # nosec B608
    rows = [dict(r) for r in db.execute(
        # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
        "SELECT id, filename, rel_path, packaging_type, revision, ean, source_mtime "  # nosec B608
        f"FROM artwork_index{wsql} ORDER BY filename LIMIT ? OFFSET ?",
        params + [per_page, (page - 1) * per_page]
    ).fetchall()]
    pages = (total + per_page - 1) // per_page if per_page else 1
    return {"rows": rows, "total": total, "page": page, "pages": pages, "per_page": per_page}


def suggest_ref(db, rel_path, limit: int = 5) -> list:
    """Propozycje produktu (REF) dla pliku bez REF — warstwowo:
      1. EAN pliku → dokładny hit w material_master (score 1.0, source 'ean'),
      2. tokeny nazwy pliku → dopasowanie do opisu produktu (Jaccard, source 'text').
    Propozycja — nigdy nie zapisuje; użytkownik akceptuje przez bind_ref."""
    import artwork_index as _ai
    import material_master as _mm
    _ai.ensure_table(db)
    try:
        _mm.ensure_table(db)
    except Exception:
        return []
    row = db.execute(
        "SELECT filename, ean FROM artwork_index WHERE rel_path=?", (str(rel_path),)
    ).fetchone()
    if not row:
        return []
    out, seen = [], set()

    # 1) EAN — najpewniejszy sygnał.
    ean = (row["ean"] or "").strip()
    if ean:
        for m in db.execute(
            "SELECT ref_code, opis_pl, ean FROM material_master "
            "WHERE ean=? AND COALESCE(active,1)=1 LIMIT ?", (ean, limit)
        ).fetchall():
            if m["ref_code"] not in seen:
                seen.add(m["ref_code"])
                out.append({"ref_code": m["ref_code"], "opis": m["opis_pl"] or "",
                            "ean": m["ean"] or "", "score": 1.0, "source": "ean"})

    # 2) Tekst nazwy pliku → opis produktu (Jaccard po tokenach).
    toks = _ai._tok(row["filename"] or "")
    key = sorted(set(_ai._word_tokens(row["filename"] or "")), key=len, reverse=True)[:4]
    if toks and key:
        cand = {}
        for k in key:
            from db import escape_like
            esc = escape_like(k)
            for m in db.execute(
                "SELECT ref_code, opis_pl, ean FROM material_master "
                "WHERE COALESCE(active,1)=1 AND opis_pl LIKE ? ESCAPE '\\' LIMIT 200",
                (f"%{esc}%",)
            ).fetchall():
                cand[m["ref_code"]] = dict(m)
        scored = []
        for m in cand.values():
            mt = _ai._tok(m["opis_pl"] or "")
            if not mt:
                continue
            inter = len(toks & mt)
            if inter < 2:
                continue
            scored.append((inter / len(toks | mt), m))
        scored.sort(key=lambda x: x[0], reverse=True)
        for s, m in scored:
            if len(out) >= limit:
                break
            if m["ref_code"] in seen:
                continue
            seen.add(m["ref_code"])
            out.append({"ref_code": m["ref_code"], "opis": m["opis_pl"] or "",
                        "ean": m["ean"] or "", "score": round(s, 2), "source": "text"})
    return out[:limit]


def bind_ref(db, rel_path, ref_code, user="") -> dict:
    """Akceptacja propozycji: wiąże plik z REF przez alias (fragment = pełna nazwa
    pliku, więc dotyczy dokładnie tego pliku i jego kopii). Cienka nakładka na
    artwork_index.add_alias — jedno źródło logiki backfillu."""
    import artwork_index as _ai
    _ai.ensure_table(db)
    row = db.execute(
        "SELECT filename FROM artwork_index WHERE rel_path=?", (str(rel_path),)
    ).fetchone()
    if not row:
        return {"error": "Nie znaleziono pliku w indeksie"}
    fn = (row["filename"] or "").strip()
    if not fn:
        return {"error": "Plik bez nazwy"}
    return _ai.add_alias(db, str(ref_code or ""), fn)


# ── Faza 3: luki i duplikaty + status pliku ───────────────────────────────────
def find_gaps(db, q="", limit: int = 500) -> list:
    """Aktywne produkty z material_master bez ŻADNEGO pliku w indeksie (po ref_norm)."""
    import artwork_index as _ai
    import material_master as _mm
    _ai.ensure_table(db)
    try:
        _mm.ensure_table(db)
    except Exception:
        return []
    where = ["COALESCE(m.active,1)=1", "m.ref_norm<>''",
             "NOT EXISTS (SELECT 1 FROM artwork_index a WHERE a.ref_norm=m.ref_norm)"]
    params = []
    q = (q or "").strip()
    if q:
        from db import escape_like
        esc = escape_like(q)
        where.append("(m.ref_code LIKE ? ESCAPE '\\' OR m.opis_pl LIKE ? ESCAPE '\\')")
        params += [f"%{esc}%", f"%{esc}%"]
    try:
        lim = max(1, min(2000, int(limit)))
    except (TypeError, ValueError):
        lim = 500
    try:
        rows = db.execute(
            # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
            "SELECT m.ref_code, m.opis_pl, m.rodzina, m.ean FROM material_master m "  # nosec B608
            "WHERE " + " AND ".join(where) + " ORDER BY m.ref_code LIMIT ?",
            params + [lim]
        ).fetchall()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return []
    return [{"ref_code": r["ref_code"], "opis": r["opis_pl"] or "",
             "rodzina": r["rodzina"] or "", "ean": r["ean"] or ""} for r in rows]


def find_duplicates(db, limit: int = 500) -> list:
    """Grupy (REF, opakowanie, rewizja) z >1 plikiem. Zwraca grupę + jej pliki
    (z aktualnym statusem), by w UI oznaczyć jeden aktywny, resztę pominąć."""
    import artwork_index as _ai
    _ai.ensure_table(db)
    ensure_status_table(db)
    try:
        lim = max(1, min(2000, int(limit)))
    except (TypeError, ValueError):
        lim = 500
    groups = db.execute(
        "SELECT ref_norm, ref_code, packaging_type, revision_rank, COUNT(*) AS n "
        "FROM artwork_index WHERE ref_norm<>'' "
        "GROUP BY ref_norm, packaging_type, revision_rank HAVING COUNT(*)>1 "
        "ORDER BY n DESC, ref_code LIMIT ?", (lim,)
    ).fetchall()
    out = []
    for g in groups:
        files = db.execute(
            "SELECT ai.rel_path, ai.filename, ai.revision, ai.source_mtime, "
            "COALESCE(s.status,'active') AS status "
            "FROM artwork_index ai "
            "LEFT JOIN artwork_file_status s ON s.rel_path=ai.rel_path "
            "WHERE ai.ref_norm=? AND ai.packaging_type=? AND ai.revision_rank=? "
            "ORDER BY ai.source_mtime DESC, ai.filename",
            (g["ref_norm"], g["packaging_type"], g["revision_rank"])
        ).fetchall()
        out.append({
            "ref_code": g["ref_code"], "ref_norm": g["ref_norm"],
            "packaging_type": g["packaging_type"] or "", "revision_rank": g["revision_rank"],
            "count": g["n"], "files": [dict(f) for f in files],
        })
    return out


def set_file_status(db, rel_path, status, user="", note="") -> dict:
    """Ustaw trwały status pliku (active/ignored/archived) — rozwiązuje „pomiń ten"."""
    ensure_status_table(db)
    st = str(status or "").strip().lower()
    if st not in _VALID_STATUS:
        return {"error": f"Status musi być jednym z: {', '.join(_VALID_STATUS)}"}
    rp = str(rel_path or "").strip()
    if not rp:
        return {"error": "Podaj rel_path"}
    db.execute(
        "INSERT INTO artwork_file_status(rel_path, status, note, updated_by, updated_at) "
        "VALUES(?,?,?,?,datetime('now')) "
        "ON CONFLICT(rel_path) DO UPDATE SET status=excluded.status, "
        "note=excluded.note, updated_by=excluded.updated_by, updated_at=datetime('now')",
        (rp, st, str(note or "")[:500], str(user or "")[:100])
    )
    db.commit()
    return {"ok": True, "rel_path": rp, "status": st}


def compute_kpis(db) -> dict:
    """Zbiorczy stan biblioteki do pulpitu. Każda metryka to osobne, tanie
    zapytanie agregujące (bez skanu 31k w Pythonie)."""
    import artwork_index as _ai
    _ai.ensure_table(db)
    ensure_status_table(db)
    stats = _ai.index_stats(db)  # total, with_ref, distinct_refs, last_scan
    return {
        "total": stats.get("total", 0),
        "with_ref": stats.get("with_ref", 0),
        "unbound": count_unbound(db),
        "distinct_refs": stats.get("distinct_refs", 0),
        "obsolete": count_obsolete(db),
        "duplicates": count_duplicates(db),
        "gaps": count_gaps(db),
        "ignored": count_ignored(db),
        "last_scan": stats.get("last_scan", ""),
    }
