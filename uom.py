"""
uom.py — jednostki miary i automatyczne przeliczanie ilości między poziomami
opakowań (np. PO w kartonach ↔ PI w sztukach).

Konwersje są danymi (tabela uom_conversion): {unit_from, unit_to, factor, ref_norm}
gdzie ref_norm = '*' oznacza regułę globalną (dla wszystkich produktów).
`factor` = ile jednostek `unit_to` mieści się w 1 jednostce `unit_from`
(np. CTN→PCS factor=1000 znaczy 1 karton = 1000 sztuk).

Moduł jest „czysty": operuje na połączeniu DB przekazanym z app.py.
"""
from __future__ import annotations

import re

# Mapowanie wariantów nazw na kanoniczne jednostki.
# Uwaga: liczba mnoga polska (końcówki -I/-A/-Y) nie jest zdejmowana przez
# generyczny strip (obsługuje tylko angielskie -Y/-S), więc częste formy mnogie
# PL podajemy wprost (OPAKOWANIA, SZTUKI, PALETY…).
_UNIT_ALIASES = {
    "PCS": "PCS", "PC": "PCS", "PIECE": "PCS", "PIECES": "PCS",
    "SZT": "PCS", "SZTUKA": "PCS", "SZTUKI": "PCS", "SZTUK": "PCS",
    "SZT.": "PCS", "EA": "PCS", "EACH": "PCS", "EACHES": "PCS",
    "UNIT": "PCS", "UNITS": "PCS",
    "CTN": "CTN", "CARTON": "CTN", "CARTONS": "CTN", "KARTON": "CTN",
    "KARTONY": "CTN", "KAR": "CTN", "BOX": "CTN", "BOXES": "CTN",
    "OP": "OP", "OPAKOWANIE": "OP", "OPAKOWANIA": "OP", "PACK": "OP",
    "PACKS": "OP", "PKG": "OP",
    "OPZ": "OPZ",
    "PAL": "PAL", "PALLET": "PAL", "PALLETS": "PAL", "PALETA": "PAL",
    "PALETY": "PAL",
    "SET": "SET", "SETS": "SET", "KPL": "SET", "ZESTAW": "SET",
    "ZESTAWY": "SET",
}


def canonical_unit(s) -> str:
    """Normalizuje nazwę jednostki do kanonicznej formy ('' gdy nieznana/pusta).
    Obsługuje liczbę mnogą (KARTONY→CTN, PIECES→PCS) przez ucięcie końcówki Y/S."""
    u = re.sub(r"[^A-Z]", "", str(s or "").upper())
    if u in _UNIT_ALIASES:
        return _UNIT_ALIASES[u]
    # Próg len>=3 (nie >3): aliasy literalne są sprawdzone wyżej, więc tu
    # trafiają tylko formy mnogie — pozwalamy więc kanonizować 3-znakowe
    # liczby mnogie (np. OPS→OP) bez psucia 3-znakowych aliasów (PCS, CTN…),
    # które zwróciły już wcześniej.
    for suf in ("Y", "S"):
        if len(u) >= 3 and u.endswith(suf) and u[:-1] in _UNIT_ALIASES:
            return _UNIT_ALIASES[u[:-1]]
    return u


def normalize_ref(raw) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(raw or "").upper())


def ensure_table(db) -> None:
    # Memoizacja per-połączenie (patrz material_master.ensure_table).
    if getattr(db, "_uom_ensured", False):
        return
    db.execute("""CREATE TABLE IF NOT EXISTS uom_conversion (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ref_norm TEXT DEFAULT '*',
        unit_from TEXT NOT NULL,
        unit_to TEXT NOT NULL,
        factor REAL NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(ref_norm, unit_from, unit_to)
    )""")
    db.commit()
    try:
        db._uom_ensured = True
    except (AttributeError, TypeError):
        pass


def load_conversions(db) -> list:
    """Zwraca listę konwersji jako dict-y (kanoniczne jednostki)."""
    ensure_table(db)
    rows = db.execute(
        "SELECT ref_norm, unit_from, unit_to, factor FROM uom_conversion"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["unit_from"] = canonical_unit(d["unit_from"])
        d["unit_to"] = canonical_unit(d["unit_to"])
        try:
            d["factor"] = float(d["factor"])
        except (TypeError, ValueError):
            continue
        out.append(d)
    return out


def get_factor(unit_from, unit_to, ref, conversions) -> float | None:
    """Znajduje przelicznik z `unit_from` na `unit_to` dla danego REF.
    Najpierw reguła per-REF, potem globalna ('*'); obsługuje kierunek odwrotny."""
    uf, ut = canonical_unit(unit_from), canonical_unit(unit_to)
    if not uf or not ut:
        return None
    if uf == ut:
        return 1.0
    rn = normalize_ref(ref)
    # Preferuj regułę dopasowaną do REF, potem globalną.
    for want_ref in ([rn, "*"] if rn else ["*"]):
        for c in conversions:
            if c.get("ref_norm", "*") != want_ref:
                continue
            if c["unit_from"] == uf and c["unit_to"] == ut and c["factor"]:
                return c["factor"]
            if c["unit_from"] == ut and c["unit_to"] == uf and c["factor"]:
                return 1.0 / c["factor"]
    return None


def convert_qty(qty: float, unit_from, unit_to, ref, conversions):
    """Przelicza ilość; zwraca (przeliczona|None, factor|None)."""
    f = get_factor(unit_from, unit_to, ref, conversions)
    if f is None:
        return (None, None)
    try:
        return (float(qty) * f, f)
    except (TypeError, ValueError):
        return (None, None)
