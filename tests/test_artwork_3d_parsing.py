"""
tests/test_artwork_3d_parsing.py — odczyt tabelki metadanych artworku (artwork_3d).

Trzy błędy złapane na realnych masterach REF 805016 (pouch / box / carton):
  1. jednostka cm brana za mm → bryła 10× za mała, po cichu,
  2. zaznaczenie checkboxa nieczytane → każdy plik wychodził "karton transportowy",
  3. kolejność wymiarów z tabelki brana dosłownie jako (W, H, D).

Testy są CZYSTE — artwork_3d importuje na poziomie modułu tylko stdlib (pymupdf/trimesh
są lazy), więc suite nie potrzebuje ani PDF-a, ani zależności ML.
Uruchom: pytest tests/
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from artwork_3d import detect_package_type, parse_dimensions_mm, pick_checked_option


# ── 1. Wymiary: jednostka ────────────────────────────────────────────────────

def test_dims_without_unit_are_mm():
    assert parse_dimensions_mm("Rozmiar [mm] 145 x 66 x 274") == (145.0, 66.0, 274.0)


def test_dims_explicit_mm():
    assert parse_dimensions_mm("100 x 200 x 300 mm") == (100.0, 200.0, 300.0)


def test_dims_in_cm_are_converted_to_mm():
    # Realny zapis z mastera 805016_carton: wzięty dosłownie dawał bryłę 10× za małą.
    assert parse_dimensions_mm("Wymiary kartonu MEAS: 35,5 x 29,5 x 31 cm") == (355.0, 295.0, 310.0)


def test_dims_comma_decimal():
    assert parse_dimensions_mm("12,5 x 20,5 x 30 mm") == (12.5, 20.5, 30.0)


def test_dims_out_of_range_rejected():
    # Poniżej 5 mm — to nie są wymiary opakowania (np. fragment numeru albo daty).
    assert parse_dimensions_mm("1 x 2 x 3 mm") is None


def test_dims_cm_range_check_applies_after_conversion():
    # 0,3 cm = 3 mm → poniżej progu 5 mm, więc odrzucone (zakres liczony PO przeliczeniu).
    assert parse_dimensions_mm("0,3 x 0,3 x 0,3 cm") is None


def test_dims_keep_written_order():
    # parse_dimensions_mm NIE zgaduje (W, H, D) — kolejność rozstrzyga locate_panels
    # na rysunku. Test przypina kontrakt, żeby nikt nie „poprawił" tego w parserze.
    assert parse_dimensions_mm("10 x 20 x 30") == (10.0, 20.0, 30.0)


def test_dims_none_when_absent():
    assert parse_dimensions_mm("Kod EAN: 5900010800827") is None
    assert parse_dimensions_mm("") is None


# ── 2. Checkbox typu opakowania ──────────────────────────────────────────────
# Współrzędne odwzorowują tabelkę z mastera 805016_box.pdf (zaznaczone "pośrednie").

OPCJE = [("indywidualne", 1264.0, 187.0),
         ("pośrednie", 1264.0, 198.8),
         ("karton transportowy", 1264.0, 210.6)]
PUSTY_1 = (1255.1, 184.5, 1261.0, 190.2, False)
PELNY_2 = (1255.4, 196.1, 1261.3, 201.8, True)
PUSTY_3 = (1255.2, 208.3, 1261.1, 214.0, False)


def test_picks_the_filled_checkbox():
    assert pick_checked_option(OPCJE, [PUSTY_1, PELNY_2, PUSTY_3]) == "pośrednie"


def test_no_checkbox_filled_returns_empty():
    assert pick_checked_option(OPCJE, [PUSTY_1, PUSTY_3]) == ""


def test_two_filled_is_ambiguous_not_a_guess():
    # Tabelka wypełniona błędnie — lepiej nic nie zwrócić niż zgadnąć intencję autora.
    pelny_1 = PUSTY_1[:4] + (True,)
    assert pick_checked_option(OPCJE, [pelny_1, PELNY_2]) == ""


def test_mark_in_another_row_is_ignored():
    daleko_w_pionie = (1255.4, 260.0, 1261.3, 265.7, True)
    assert pick_checked_option(OPCJE, [daleko_w_pionie]) == ""


def test_mark_too_far_left_is_ignored():
    # Kwadrat z zupełnie innej kolumny tabelki nie jest checkboxem tej etykiety.
    daleko_w_poziomie = (900.0, 196.1, 905.9, 201.8, True)
    assert pick_checked_option(OPCJE, [daleko_w_poziomie]) == ""


def test_mark_on_the_right_of_label_is_ignored():
    po_prawej = (1300.0, 196.1, 1305.9, 201.8, True)
    assert pick_checked_option(OPCJE, [po_prawej]) == ""


def test_empty_inputs():
    assert pick_checked_option([], [PELNY_2]) == ""
    assert pick_checked_option(OPCJE, []) == ""


# ── 3. Fallback tekstowy ─────────────────────────────────────────────────────

def test_text_fallback_cannot_tell_which_is_checked():
    # Tabelka wymienia wszystkie trzy etykiety, więc po samym tekście nie da się poznać
    # zaznaczenia — fallback zwraca pierwszą z listy i JEST oznaczany ostrzeżeniem
    # w extract_dieline_info. Test pilnuje, że to świadome zachowanie, nie regresja.
    tabelka = "Typ opakowania: indywidualne pośrednie karton transportowy"
    assert detect_package_type(tabelka) == "karton transportowy"


def test_text_fallback_single_type():
    assert detect_package_type("Typ opakowania: indywidualne") == "indywidualne"
    assert detect_package_type("") == ""
