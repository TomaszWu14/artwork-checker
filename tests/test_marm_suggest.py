"""MARM-SUGG-01: rankingowane sugestie auto-linku MARM dla REF artworku.

Dopasowanie exact→fuzzy po material_uom_dims; tylko REF-y z renderowalnymi
jednostkami (get_ref_units niepuste)."""
import material_uom as _mu


def _db_with(rows):
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    _mu.ensure_table(con)
    for ref_code, unit, w, h, l in rows:
        con.execute(
            "INSERT INTO material_uom_dims (ref_code, ref_norm, unit, width, height, "
            "length, dim_unit) VALUES (?,?,?,?,?,?, 'MM')",
            (ref_code, _mu.normalize_ref(ref_code), unit, w, h, l))
    con.commit()
    return con


def test_exact_ref_ranked_first():
    db = _db_with([
        ("300000", "KAR", 400, 300, 200),
        ("300001", "KAR", 400, 300, 200),
        ("399999", "KAR", 100, 100, 100),
    ])
    sug = _mu.suggest_refs(db, "300000")
    assert sug[0]["ref_code"] == "300000"
    assert sug[0]["score"] >= sug[-1]["score"]        # malejąco po trafności
    assert sug[0]["units"][0]["unit"] == "KAR"        # jednostki renderowalne dołączone


def test_only_renderable_refs():
    """REF z samymi zerowymi wymiarami (nierenderowalny) nie jest sugerowany."""
    db = _db_with([
        ("300000", "KAR", 400, 300, 200),
        ("300000", "JU", 0, 0, 0),        # ten wariant odpada (zerowe wymiary)
        ("ZERO01", "KAR", 0, 0, 0),       # cały REF nierenderowalny
    ])
    refs = {s["ref_code"] for s in _mu.suggest_refs(db, "3000")}
    assert "300000" in refs
    assert "ZERO01" not in refs


def test_empty_ref_returns_nothing():
    assert _mu.suggest_refs(_db_with([("300000", "KAR", 1, 1, 1)]), "") == []


def test_no_match_returns_empty():
    db = _db_with([("AAAAAA", "KAR", 10, 10, 10)])
    # zupełnie inny REF — brak sensownych kandydatów
    assert _mu.suggest_refs(db, "ZZZZZZ") == []
