"""tests/test_material_master.py — master data materiałów + walidacja pozycji."""
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import material_master as mm


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    return db


def test_upsert_and_lookup():
    db = _db()
    rows = [
        {"ref_code": "NL753-S-40", "opis_pl": "Cewnik", "opis_en": "Catheter",
         "ean": "5900010800810", "karton_wymiar": "520 x 380 x 300 mm"},
        {"ref_code": "", "opis_pl": "pusty"},
    ]
    res = mm.upsert_materials(db, rows)
    assert res == {"imported": 1, "skipped": 1}
    assert mm.get_material(db, "NL753-S-40")["opis_en"] == "Catheter"
    # dopasowanie po znormalizowanym kodzie
    assert mm.get_material(db, "NL753S40")["ref_code"] == "NL753-S-40"
    assert mm.get_material(db, "DOES-NOT-EXIST") is None


def test_upsert_is_idempotent_update():
    db = _db()
    mm.upsert_materials(db, [{"ref_code": "A1", "opis_pl": "stary"}])
    mm.upsert_materials(db, [{"ref_code": "A1", "opis_pl": "nowy"}])
    assert mm.get_material(db, "A1")["opis_pl"] == "nowy"
    n = db.execute("SELECT COUNT(*) FROM material_master").fetchone()[0]
    assert n == 1


def test_product_group_and_subgroup_roundtrip():
    """Grupa/podgrupa produktowa to osobne pola od 'rodzina' — po nich łączymy REF-y."""
    db = _db()
    mm.upsert_materials(db, [{
        "ref_code": "NL753-S-40", "opis_pl": "Cewnik", "rodzina": "cewniki",
        "grupa produktowa": "urologia", "podgrupa": "cewniki Foleya",
    }])
    row = mm.get_material(db, "NL753-S-40")
    assert (row["grupa"], row["podgrupa"]) == ("urologia", "cewniki Foleya")
    assert row["rodzina"] == "cewniki"          # stare pole nietknięte


def test_group_not_wiped_by_import_without_it():
    """Import bez kolumny grupy nie kasuje istniejącej klasyfikacji."""
    db = _db()
    mm.upsert_materials(db, [{"ref_code": "A1", "grupa produktowa": "urologia",
                              "podgrupa": "Foley"}])
    mm.upsert_materials(db, [{"ref_code": "A1", "opis_pl": "nowy opis"}])
    row = mm.get_material(db, "A1")
    assert (row["grupa"], row["podgrupa"]) == ("urologia", "Foley")
    assert row["opis_pl"] == "nowy opis"


def test_validate_items():
    db = _db()
    mm.upsert_materials(db, [{"ref_code": "R1", "ean": "5900010800810"}])
    warns = mm.validate_items(
        db,
        [{"ref": "R1", "ean_a": "5900010800810"},   # ok
         {"ref": "UNK"},                              # unknown
         {"ref": "R1", "ean_a": "1111111111111"}],    # ean mismatch
        "ref", "ean_a",
    )
    assert any("UNK" in w for w in warns)
    assert any("≠ master" in w for w in warns)
    assert not any(w.startswith("REF R1 — brak") for w in warns)


def test_normalize_ref():
    assert mm.normalize_ref("NL753-S-40") == mm.normalize_ref("nl753 s 40") == "NL753S40"


def test_base_level_self_conversion_skipped():
    # Poziom bazowy (SZT) z qty_base=1 nie może tworzyć reguły PCS→PCS.
    db = _db()
    rows = [['', 'SZT', 'OP'],
            ['ref_code', 'Podstawowa jednostka miary', 'Ilość podstawowej jednostki miary'],
            ['Z1', 'SZT', '12']]
    mm.import_workbook(db, rows)
    import uom
    rules = uom.load_conversions(db)
    # tylko OP→SZT, żadnej self-reguły PCS→PCS
    assert all(not (r['unit_from'] == r['unit_to']) for r in rules)


def test_bare_ean_under_banner_is_level_not_global():
    db = _db()
    rows = [['', 'KAR'],
            ['ref_code', 'ean'],
            ['E1', '5900010800810']]
    mm.import_workbook(db, rows)
    m = mm.get_material(db, 'E1')
    import json
    levels = json.loads(m['levels_json'])
    # 'ean' pod bannerem KAR → poziom kartonu, nie globalny EAN materiału
    assert m['ean'] == ''
    assert levels.get('karton', {}).get('ean') == '5900010800810'


def test_two_row_header_with_converters():
    db = _db()
    rows = [
        ['', '', '', '', '', 'SZT', 'SZT', 'OP', 'OP', 'KAR', 'KAR', 'PAZ', 'PPA'],
        ['ref_code', 'opis_pl', 'opis_en', 'Podstawowa jednostka miary', 'kod producenta',
         'sztuka_ean', 'Ilość podstawowej jednostki miary',
         'op_ean', 'Ilość podstawowej jednostki miary',
         'karton_ean', 'Ilość podstawowej jednostki miary',
         'Ilość podstawowej jednostki miary PAZ', 'Ilość podstawowej jednostki miary PPA'],
        ['R-1', 'Opis', 'Desc', 'SZT', 'PROD-9',
         '5900010800810', '1', '5900010800018', '12', '5900010800025', '240', '4800', '2400'],
    ]
    res = mm.import_workbook(db, rows)
    assert res['imported'] == 1
    m = mm.get_material(db, 'R-1')
    assert m['base_uom'] == 'SZT'
    assert m['producer_code'] == 'PROD-9'
    # przeliczniki trafiły do uom_conversion
    import uom
    conv = uom.load_conversions(db)
    assert uom.get_factor('OP', 'SZT', 'R-1', conv) == 12.0
    assert uom.get_factor('KAR', 'SZT', 'R-1', conv) == 240.0
    assert uom.get_factor('PAZ', 'SZT', 'R-1', conv) == 4800.0
