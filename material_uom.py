"""
material_uom.py — wymiary opakowań per (REF, jednostka) z eksportu SAP MARM.

Jeden wiersz MARM = jeden REF w jednej alternatywnej jednostce miary (JU/SZT/KAR/
OP/OPZ/PAZ…) z wymiarami W×H×L (najczęściej w CM), przelicznikiem (Licznik/Mianownik),
wagą, objętością i EAN. Trzymamy dane liczbowo, tak jak w pliku (z jednostką wymiaru),
a konwersję do mm robimy przy odczycie — dzięki temu potok 3D (`artwork_3d`, wewn. mm)
dostaje gotowe `w_mm/h_mm/d_mm`.

Moduł jest „czysty": operuje na połączeniu DB przekazanym z app.py. Tabela jest
samo-bootstrapująca (wzorzec `uom.py` / `material_master.py`), nie ma jej w migrate_db.
"""
from __future__ import annotations

import re

from uom import normalize_ref

# Nazwa jednostki wymiaru → mnożnik do mm.
_DIM_UNIT_MM = {"MM": 1.0, "CM": 10.0, "M": 1000.0, "": 10.0}

# Zakres sensownych wymiarów renderowalnych (jak w /api/artwork/3d/photo).
_MIN_MM, _MAX_MM = 5.0, 2000.0


def _num(v):
    """Liczba z komórki (toleruje formaty PL) → float lub None."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    from normalizer import normalize_number
    d = normalize_number(str(v))
    return float(d) if d is not None else None


def ensure_table(db) -> None:
    # Memoizacja per-połączenie (jak uom.ensure_table).
    if getattr(db, "_material_uom_ensured", False):
        return
    db.execute("""CREATE TABLE IF NOT EXISTS material_uom_dims (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ref_code TEXT NOT NULL,
        ref_norm TEXT NOT NULL,
        unit TEXT NOT NULL,
        numerator REAL,
        denominator REAL,
        width REAL,
        height REAL,
        length REAL,
        dim_unit TEXT,
        gross_weight REAL,
        weight_unit TEXT,
        volume REAL,
        volume_unit TEXT,
        ean TEXT,
        updated_at TEXT DEFAULT (datetime('now')),
        UNIQUE(ref_norm, unit)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_material_uom_ref ON material_uom_dims(ref_norm)")
    db.commit()
    try:
        db._material_uom_ensured = True
    except (AttributeError, TypeError):
        pass


# ── Import MARM ─────────────────────────────────────────────────────────────

# Fragment nagłówka (lower, bez ogonków) → nazwa pola. Dopasowanie po zawieraniu,
# więc tolerujemy warianty ("materiał", "nr materiału", "kod ean/upc" itd.).
_HEADER_MAP = [
    ("material", "ref"),
    ("alternatywna jednostka", "unit"),
    ("jednostka miary", "unit"),
    ("mianownik", "denominator"),
    ("licznik", "numerator"),
    ("szerokosc", "width"),
    ("wysokosc", "height"),
    ("dlugosc", "length"),
    ("jednostka wymiaru", "dim_unit"),
    ("waga brutto", "gross_weight"),
    ("jednostka wagi", "weight_unit"),
    ("objetosc", "volume"),
    ("jednostka objetosci", "volume_unit"),
    ("ean", "ean"),
]

_DIACRITICS = str.maketrans("ąćęłńóśźż", "acelnoszz")


def _norm_header(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower().translate(_DIACRITICS))


def _map_columns(header_row) -> dict:
    """{nazwa_pola: indeks_kolumny} z wiersza nagłówka MARM."""
    cols = {}
    for idx, cell in enumerate(header_row):
        h = _norm_header(cell)
        if not h:
            continue
        for frag, field in _HEADER_MAP:
            if field in cols:
                continue
            if frag in h:
                cols[field] = idx
                break
    return cols


def import_marm_rows(db, raw_rows) -> dict:
    """Wczytuje wiersze MARM (lista list) do material_uom_dims. Zwraca liczniki.
    Upsert po (ref_norm, unit) — ponowny import nadpisuje, nie duplikuje."""
    ensure_table(db)
    if not raw_rows:
        return {"imported": 0, "refs": 0, "skipped": 0}

    # Znajdź wiersz nagłówka (pierwszy z 'material' i 'jednostka').
    header_idx, cols = None, {}
    for i, row in enumerate(raw_rows[:5]):
        c = _map_columns(row)
        if "ref" in c and "unit" in c:
            header_idx, cols = i, c
            break
    if header_idx is None:
        raise ValueError("Nie rozpoznano nagłówka MARM (brak kolumn Materiał / Jednostka miary)")

    def g(row, field):
        idx = cols.get(field)
        return row[idx] if idx is not None and idx < len(row) else None

    batch, refs, skipped = [], set(), 0
    seen = set()   # (ref_norm, unit) w tej paczce — ostatni wygrywa
    for row in raw_rows[header_idx + 1:]:
        ref = str(g(row, "ref") or "").strip()
        unit = str(g(row, "unit") or "").strip().upper()
        ref_norm = normalize_ref(ref)
        if not ref_norm or not unit:
            skipped += 1
            continue
        key = (ref_norm, unit)
        if key in seen:      # duplikat w pliku — pomiń wcześniejszy
            skipped += 1
        seen.add(key)
        refs.add(ref_norm)
        batch.append((
            ref, ref_norm, unit,
            _num(g(row, "numerator")), _num(g(row, "denominator")),
            _num(g(row, "width")), _num(g(row, "height")), _num(g(row, "length")),
            str(g(row, "dim_unit") or "").strip().upper(),
            _num(g(row, "gross_weight")), str(g(row, "weight_unit") or "").strip().upper(),
            _num(g(row, "volume")), str(g(row, "volume_unit") or "").strip().upper(),
            str(g(row, "ean") or "").strip(),
        ))

    sql = """INSERT OR REPLACE INTO material_uom_dims
        (ref_code, ref_norm, unit, numerator, denominator,
         width, height, length, dim_unit,
         gross_weight, weight_unit, volume, volume_unit, ean)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
    imported = 0
    for i in range(0, len(batch), 1000):
        chunk = batch[i:i + 1000]
        db.executemany(sql, chunk)
        imported += len(chunk)
    db.commit()
    return {"imported": imported, "refs": len(refs), "skipped": skipped}


# ── Odczyt ──────────────────────────────────────────────────────────────────

def _dims_mm(row) -> tuple | None:
    """(w,h,d) w mm z wiersza tabeli, albo None gdy niekompletne/poza zakresem."""
    mult = _DIM_UNIT_MM.get((row["dim_unit"] or "").upper(), 10.0)
    try:
        vals = [float(row["width"] or 0) * mult,
                float(row["height"] or 0) * mult,
                float(row["length"] or 0) * mult]
    except (TypeError, ValueError):
        return None
    if all(_MIN_MM <= v <= _MAX_MM for v in vals):
        return tuple(round(v, 2) for v in vals)
    return None


def get_ref_units(db, ref) -> list:
    """Renderowalne jednostki danego REF: [{unit, w_mm, h_mm, d_mm, ean, factor}].
    Konwersja dim_unit→mm; jednostki z zerowymi/poza-zakresem wymiarami są pomijane
    (np. JU z samymi zerami)."""
    ensure_table(db)
    ref_norm = normalize_ref(ref)
    if not ref_norm:
        return []
    rows = db.execute(
        "SELECT * FROM material_uom_dims WHERE ref_norm = ? ORDER BY unit",
        (ref_norm,)).fetchall()
    out = []
    for r in rows:
        dims = _dims_mm(r)
        if not dims:
            continue
        num, den = r["numerator"], r["denominator"]
        factor = (num / den) if (num and den) else None
        out.append({
            "unit": r["unit"],
            "w_mm": dims[0], "h_mm": dims[1], "d_mm": dims[2],
            "ean": r["ean"] or "",
            "factor": factor,
        })
    return out


def suggest_refs(db, artwork_ref, limit=5) -> list:
    """MARM-SUGG-01: rankingowane sugestie powiązania MARM dla REF artworku.

    Kandydaci prefiltrowani w SQL (exact / prefiks / substring — bounded, nie skan
    całej tabeli), potem ranking: exact ref_norm = 100, reszta rapidfuzz.ratio.
    Zwraca tylko REF-y z renderowalnymi jednostkami (get_ref_units niepuste):
    [{ref_code, score, units:[{unit,w_mm,h_mm,d_mm,ean,factor}]}] malejąco po score.
    """
    ensure_table(db)
    qn = normalize_ref(artwork_ref)
    if not qn:
        return []
    limit = max(1, min(int(limit or 5), 20))
    prefix = qn[:4] if len(qn) >= 4 else qn
    cand = db.execute(
        "SELECT DISTINCT ref_code, ref_norm FROM material_uom_dims "
        "WHERE ref_norm = ? OR ref_norm LIKE ? OR ref_norm LIKE ? LIMIT 500",
        (qn, f"{prefix}%", f"%{qn}%")).fetchall()
    if not cand:
        return []
    from rapidfuzz import fuzz
    scored = []
    for r in cand:
        rn = r["ref_norm"] or ""
        score = 100.0 if rn == qn else float(fuzz.ratio(qn, rn))
        scored.append((score, r["ref_code"]))
    scored.sort(key=lambda t: (-t[0], t[1]))
    out = []
    for score, ref_code in scored:
        units = get_ref_units(db, ref_code)
        if not units:
            continue
        out.append({"ref_code": ref_code, "score": round(score, 1), "units": units})
        if len(out) >= limit:
            break
    return out


def search_refs(db, q, limit=20) -> list:
    """Podpowiedzi REF (kod + opcjonalnie ile jednostek renderowalnych). Dopasowanie
    po ref_norm LIKE; zwraca tylko REF-y mające ≥1 jednostkę z wymiarami."""
    ensure_table(db)
    qn = normalize_ref(q)
    if not qn:
        return []
    limit = max(1, min(int(limit or 20), 50))
    rows = db.execute(
        "SELECT DISTINCT ref_code, ref_norm FROM material_uom_dims "
        "WHERE ref_norm LIKE ? ORDER BY ref_code LIMIT ?",
        (f"%{qn}%", limit * 4)).fetchall()
    out = []
    for r in rows:
        if get_ref_units(db, r["ref_code"]):
            out.append({"ref_code": r["ref_code"]})
        if len(out) >= limit:
            break
    return out
