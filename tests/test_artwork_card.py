"""
tests/test_artwork_card.py — agregat karty produktu per REF (artwork_card.build_card).

Czysta logika na tymczasowym SQLite (jak test_material_master / test_artwork_index).
Uruchom: pytest tests/
"""
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import material_master as mm
import artwork_index as ai
import artwork_card as card


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    return db


def _seed(db):
    # Master: dane wspólne + poziomy (op ma EAN+wymiar, karton ma EAN+wymiar,
    # opz ma tylko wymiar → EAN powinien wyjść NO-EAN).
    mm.upsert_materials(db, [{
        "ref_code": "805016", "opis_pl": "Opatrunek", "opis_en": "Dressing",
        "rodzina": "elastoDERM", "producer_code": "XN-EDF4",
        "op_ean": "5900010800810", "op_wymiar": "70 x 130 mm",
        "karton_ean": "5900010800827", "karton_wymiar": "145 x 75 x 55 mm",
        "opz_wymiar": "300 x 200 x 150 mm",
    }])
    # Index: dwie rewizje pouch (op) + jeden carton (karton). Nazwy zawierają słowo
    # poziomu, bo lookup_best/_pick_level dopasowuje po nazwie pliku.
    ai.ensure_table(db)
    rn = ai.normalize_ref("805016")
    rows = [
        (rn, "805016", "805016_pouch_5900010800810_Rev.02_14-05-2026.pdf", "p/a.pdf",
         "pouch", "Rev.02", 2, "5900010800810", "2026-05-14"),
        (rn, "805016", "805016_pouch_5900010800810_Rev.01_02-01-2025.pdf", "p/b.pdf",
         "pouch", "Rev.01", 1, "5900010800810", "2025-01-02"),
        (rn, "805016", "805016_carton_5900010800827_Rev.02_14-05-2026.pdf", "p/c.pdf",
         "carton", "Rev.02", 2, "5900010800827", "2026-05-14"),
    ]
    db.executemany(
        "INSERT INTO artwork_index(ref_norm, ref_code, filename, rel_path, "
        "packaging_type, revision, revision_rank, ean, source_mtime) "
        "VALUES(?,?,?,?,?,?,?,?,?)", rows)
    db.commit()


def test_build_card_common_and_ref():
    db = _db(); _seed(db)
    c = card.build_card(db, "805016")
    assert c is not None
    assert c["ref"] == "805016"
    assert c["common"]["opis_en"] == "Dressing"
    assert c["common"]["rodzina"] == "elastoDERM"


def test_build_card_normalizes_ref():
    db = _db(); _seed(db)
    # inny zapis tego samego REF trafia w ref_norm
    assert card.build_card(db, " 805016 ")["ref"] == "805016"


def test_levels_have_ean_dims_and_no_ean_placeholder():
    db = _db(); _seed(db)
    lv = {x["level"]: x for x in card.build_card(db, "805016")["levels"]}
    assert lv["op"]["ean"] == "5900010800810"
    assert lv["op"]["dims"] == "70 x 130 mm"
    assert lv["karton"]["ean"] == "5900010800827"
    # opz ma wymiar, ale brak EAN i brak pliku → placeholder, nie ciąg zer
    assert lv["opz"]["ean"] == "NO-EAN"


def test_level_matches_file_by_packaging():
    db = _db(); _seed(db)
    lv = {x["level"]: x for x in card.build_card(db, "805016")["levels"]}
    # op → plik z 'pouch' w nazwie, najnowsza rewizja
    assert lv["op"]["file"] is not None
    assert "pouch" in lv["op"]["file"]["filename"]
    assert lv["op"]["file"]["revision"] == "Rev.02"
    # karton → plik z 'carton'
    assert "carton" in lv["karton"]["file"]["filename"]


def test_current_rev_is_highest_rank():
    db = _db(); _seed(db)
    c = card.build_card(db, "805016")
    assert c["current_rev"] == "Rev.02"
    assert c["revisions"][0]["revision_rank"] == 2  # posortowane malejąco


def test_unknown_ref_returns_none():
    db = _db(); _seed(db)
    assert card.build_card(db, "NIE-MA-TAKIEGO") is None
    assert card.build_card(db, "") is None
