"""tests/test_artwork_naming.py — parser nazw masterów ACME."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from artwork_naming import (parse_master_filename as P, revision_sort_key,
                            pick_current_revisions, format_master_label, _roman_to_int)


def test_format_label():
    assert format_master_label("700001_carton Rev.02.pdf") == "700001 – carton – Rev.02"
    assert format_master_label("QY17050820-SS_box.pdf") == "QY17050820-SS – box"
    assert format_master_label("QZ-1001 2_carton sticker.pdf") == "QZ-1001 2 – carton sticker"


def test_basic_ref_and_type():
    r = P("GS1710123-SS_carton_sticker.pdf")
    assert r["ref"] == "GS1710123-SS"
    assert r["packaging_type"] == "carton sticker"


def test_longest_type_wins():
    # "carton sticker" musi złapać się przed "carton"
    assert P("KDP-17-45_carton sticker.pdf")["packaging_type"] == "carton sticker"
    assert P("AT-SD-LAP3-SP_carton.pdf")["packaging_type"] == "carton"


def test_ean_and_revision():
    r = P("805016_pouch_2 Rev.00_5900010800810.pdf")
    assert r["ref"] == "805016"
    assert r["packaging_type"] == "pouch"
    assert r["ean"] == "5900010800810"
    assert r["revision"].lower().startswith("rev")
    assert r["revision_rank"] == 0


def test_revision_rank_numeric():
    assert P("GS1310081_carton_2 Rev.02.pdf")["revision_rank"] == 2
    assert P("X-1_box v3.pdf")["revision_rank"] == 3


def test_revision_roman():
    r = P("Instrukcja GAZA lux S Wyd.III.pdf")
    assert r["revision_rank"] == 3


def test_copy_counter_stripped():
    assert P("CN-14-40_pouch (2).pdf")["ref"] == "CN-14-40"


def test_no_type_still_ref():
    r = P("Poly Spike V Plus_B & G.pdf")
    assert r["ref"]                      # niepuste
    assert r["packaging_type"] == ""


def test_roman_helper():
    assert _roman_to_int("III") == 3
    assert _roman_to_int("IV") == 4
    assert _roman_to_int("IX") == 9


def test_revision_sort_picks_highest():
    a = P("REF1_box Rev.00.pdf"); b = P("REF1_box Rev.02.pdf")
    chosen = max([a, b], key=lambda p: revision_sort_key(p))
    assert chosen["revision_rank"] == 2
    # fallback po dacie gdy brak znacznika
    c = P("REF2_box.pdf"); d = P("REF2_box.pdf")
    assert revision_sort_key(c, "2026-01-01") > revision_sort_key(d, "2025-01-01")


def test_pick_current_revision_rank_wins():
    # Ten sam REF+opakowanie: wyższy Rev wygrywa niezależnie od daty.
    files = [
        {"id": 1, "filename": "GS1310081_carton Rev.00.pdf", "modified_at": "2026-05-01"},
        {"id": 2, "filename": "GS1310081_carton Rev.02.pdf", "modified_at": "2020-01-01"},
    ]
    out = pick_current_revisions(files)
    assert len(out) == 1 and out[0]["id"] == 2


def test_pick_current_date_breaks_tie():
    # Brak znacznika Rev → wygrywa najnowszy modified_at.
    files = [
        {"id": 1, "filename": "QZ-1001 2_pouch.pdf", "modified_at": "2025-03-01"},
        {"id": 2, "filename": "QZ-1001 2_pouch.pdf", "modified_at": "2026-08-01"},
    ]
    out = pick_current_revisions(files)
    assert len(out) == 1 and out[0]["id"] == 2


def test_pick_current_separates_packaging_and_drops_refless():
    files = [
        {"id": 1, "filename": "700001_carton.pdf", "modified_at": "2024-01-01"},
        {"id": 2, "filename": "700001_pouch.pdf", "modified_at": "2024-01-01"},
        {"id": 3, "filename": "_.pdf", "modified_at": "2024-01-01"},  # bez REF → pomiń
    ]
    out = pick_current_revisions(files)
    assert len(out) == 2                     # carton i pouch osobno; szum odrzucony
    assert {o["packaging_type"] for o in out} == {"carton", "pouch"}


# --- słownik poziomów: bag + warianty z myślnikiem ------------------------

def test_bag_recognised_after_underscore():
    r = P("BQES24080_bag.pdf")
    assert r["packaging_type"] == "bag"
    assert r["ref"] == "BQES24080"        # "bag" nie zostaje w REF-ie


def test_bag_not_matched_inside_product_name():
    # "Vomit Bag" / "Reservoir Bag" to nazwy wyrobu — poziomem jest label/pouch.
    assert P("2023 Vomit Bag label - 4.pdf")["packaging_type"] == "label"
    assert P("Vomit Bag carton ww02 ACME.pdf")["packaging_type"] == "carton"
    assert P("038-81-920SIL Silicone Reservoir Bag Pouch Medium.pdf")["packaging_type"] == "pouch"


def test_hyphen_variants_map_to_canonical_form():
    # Zapis z konwencji (myślnik) musi dać tę samą formę kanoniczną co stary
    # zapis z podkreśleniem — inaczej grupy REF+opakowanie rozjechałyby się.
    assert P("INS-1204_carton-sticker.pdf")["packaging_type"] == "carton sticker"
    assert P("INS-1204_carton_sticker.pdf")["packaging_type"] == "carton sticker"
    assert P("811210_box-print.pdf")["packaging_type"] == "box print"
    assert P("811210_carton-print.pdf")["packaging_type"] == "carton print"


def test_hyphen_sticker_beats_bare_carton():
    # Regresja: przed zmianą "carton-sticker" czytało się jako zwykły "carton".
    r = P("YW-07-B_carton-sticker.pdf")
    assert r["packaging_type"] == "carton sticker"
    assert r["ref"] == "YW-07-B"


# --- walidator konwencji ---------------------------------------------------

from artwork_naming import validate_master_filename as V  # noqa: E402


def _codes(filename):
    return {p["code"] for p in V(filename)["problems"]}


def test_validator_accepts_conforming_name():
    r = V("805016_pouch_5900010800810_Rev.02_2026-05-14.pdf")
    assert r["ok"] and r["problems"] == []
    assert r["fields"]["ref"] == "805016"
    assert r["fields"]["level"] == "pouch"
    assert r["fields"]["ean"] == "5900010800810"


def test_validator_accepts_noean_placeholder():
    assert V("805016_insert_noEAN_Rev.02_2026-05-14.pdf")["ok"]


def test_validator_accepts_supplier_suffix():
    r = V("805016_pouch_5900010800810_Rev.02_2026-05-14_XY-print.pdf")
    assert r["ok"]
    assert r["fields"]["suffix"] == "XY-print"


def test_validator_catches_bad_ean_checksum():
    # Literówka w EAN nie daje dziś żadnego błędu — kod po prostu znika z odczytu.
    assert _codes("805016_pouch_5900010800811_Rev.02_2026-05-14.pdf") == {"ean_checksum"}


def test_validator_rejects_abbreviated_level():
    assert "level" in _codes("805016_CS_5900010800810_Rev.02_2026-05-14.pdf")


def test_validator_rejects_loose_revision_and_date():
    codes = _codes("805016_pouch_5900010800810_Rev.2_14.05.2026.pdf")
    assert "revision" in codes and "date" in codes


def test_validator_rejects_impossible_date():
    assert "date" in _codes("805016_pouch_5900010800810_Rev.02_2026-13-45.pdf")


def test_validator_reports_invisible_and_special_chars():
    codes = _codes("Acme Medicare PF•L.pdf")
    assert "chars" in codes and "fields" in codes
    msg = " ".join(p["msg"] for p in V("805016_pouch x_noEAN_Rev.02_2026-05-14.pdf")["problems"])
    assert "U+00A0" in msg          # twarda spacja nazwana wprost


def test_validator_counts_fields():
    assert "fields" in _codes("805016_pouch.pdf")


def test_validator_never_raises_on_garbage():
    for junk in ["", ".pdf", "___", "a_b_c_d_e", None]:
        assert isinstance(V(junk)["ok"], bool)
