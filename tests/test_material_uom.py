"""tests/test_material_uom.py — wymiary opakowań per (REF, jednostka) z MARM."""
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import material_uom as mu


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    return db


# Nagłówek + wiersze jak w prawdziwym eksporcie MARM (kolejność kolumn z pliku).
HEADER = ["Materiał", "Alternatywna jednostka miary", "Mianownik", "Licznik",
          "Wykorz. zdoln. prod.", "Szerokość", "Wysokość", "Długość",
          "Jednostka wymiaru", "Waga brutto", "Jednostka wagi", "Objętość",
          "Jednostka objętości", "Kod EAN/UPC"]


def _rows(*data):
    return [HEADER, *[list(r) for r in data]]


def test_import_and_cm_to_mm():
    db = _db()
    raw = _rows(
        ["300000", "JU", 1, 1, 0, 0, 0, 0, "", 0, "", 0, "", ""],           # zera → nierenderowalne
        ["300000", "SZT", 1, 1, 0, 80, 15, 120, "CM", 15, "KG", 144, "CD3", "5901234567890"],
    )
    res = mu.import_marm_rows(db, raw)
    assert res["imported"] == 2
    assert res["refs"] == 1

    units = mu.get_ref_units(db, "300000")
    # JU (same zera) odfiltrowane; zostaje SZT z konwersją CM→mm (×10).
    assert len(units) == 1
    u = units[0]
    assert u["unit"] == "SZT"
    assert (u["w_mm"], u["h_mm"], u["d_mm"]) == (800.0, 150.0, 1200.0)
    assert u["ean"] == "5901234567890"


def test_import_is_upsert_no_duplicates():
    db = _db()
    mu.import_marm_rows(db, _rows(["A1", "KAR", 1, 10, 0, 30, 20, 40, "CM", 1, "KG", 0, "", ""]))
    mu.import_marm_rows(db, _rows(["A1", "KAR", 1, 10, 0, 35, 25, 45, "CM", 1, "KG", 0, "", ""]))
    n = db.execute("SELECT COUNT(*) FROM material_uom_dims").fetchone()[0]
    assert n == 1
    u = mu.get_ref_units(db, "A1")[0]
    assert (u["w_mm"], u["h_mm"], u["d_mm"]) == (350.0, 250.0, 450.0)   # ostatni wygrywa


def test_dim_unit_mm_and_out_of_range_filtered():
    db = _db()
    mu.import_marm_rows(db, _rows(
        ["B2", "OP", 1, 1, 0, 100, 60, 200, "MM", 0, "", 0, "", ""],   # już w mm → bez ×10
        ["B2", "KAR", 1, 1, 0, 500, 15, 120, "CM", 0, "", 0, "", ""],  # 5000 mm > 2000 → odpada
    ))
    units = {u["unit"]: u for u in mu.get_ref_units(db, "B2")}
    assert set(units) == {"OP"}
    assert (units["OP"]["w_mm"], units["OP"]["h_mm"], units["OP"]["d_mm"]) == (100.0, 60.0, 200.0)


def test_factor_from_numerator_denominator():
    db = _db()
    # Licznik=10 / Mianownik=1 → 1 KAR = 10 JU (factor=10).
    mu.import_marm_rows(db, _rows(["C3", "KAR", 1, 10, 0, 30, 20, 40, "CM", 0, "", 0, "", ""]))
    assert mu.get_ref_units(db, "C3")[0]["factor"] == 10.0


def test_search_refs_only_with_dims():
    db = _db()
    mu.import_marm_rows(db, _rows(
        ["300000", "SZT", 1, 1, 0, 80, 15, 120, "CM", 0, "", 0, "", ""],
        ["300001", "JU", 1, 1, 0, 0, 0, 0, "", 0, "", 0, "", ""],       # bez wymiarów
    ))
    found = [r["ref_code"] for r in mu.search_refs(db, "3000")]
    assert "300000" in found
    assert "300001" not in found       # brak renderowalnych jednostek
    assert mu.search_refs(db, "") == []


def test_ref_normalization_matches():
    db = _db()
    mu.import_marm_rows(db, _rows(["NL753-S-40", "KAR", 1, 1, 0, 30, 20, 40, "CM", 0, "", 0, "", ""]))
    # zapytanie bez myślników trafia po ref_norm
    assert mu.get_ref_units(db, "NL753S40")
    assert [r["ref_code"] for r in mu.search_refs(db, "nl753s40")] == ["NL753-S-40"]


def test_bad_header_raises():
    db = _db()
    try:
        mu.import_marm_rows(db, [["foo", "bar"], ["1", "2"]])
        assert False, "oczekiwano ValueError"
    except ValueError:
        pass
