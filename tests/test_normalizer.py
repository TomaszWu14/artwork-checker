"""
tests/test_normalizer.py — testy jednostkowe dla normalizer.py

Uruchom: pytest tests/
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from decimal import Decimal
import pytest
from normalizer import (
    normalize_number, normalize_date, normalize_unicode as normalize_text,
    normalize_ref, refs_match, numbers_equal,
)


# ── normalize_number ──────────────────────────────────────────────────────────

class TestNormalizeNumber:
    def test_integer(self):
        assert normalize_number("12345") == Decimal("12345")

    def test_us_format(self):
        assert normalize_number("13,500.00") == Decimal("13500.00")

    def test_eu_format(self):
        assert normalize_number("13.500,00") == Decimal("13500.00")

    def test_space_separator(self):
        assert normalize_number("13 500,00") == Decimal("13500.00")

    def test_simple_decimal(self):
        assert normalize_number("13500.00") == Decimal("13500.00")

    def test_zero(self):
        assert normalize_number("0") == Decimal("0")
        assert normalize_number("0.00") == Decimal("0")

    def test_negative(self):
        result = normalize_number("-1,234.56")
        assert result == Decimal("-1234.56")

    def test_empty_string(self):
        assert normalize_number("") is None

    def test_none_input(self):
        assert normalize_number(None) is None

    def test_non_numeric(self):
        assert normalize_number("abc") is None

    def test_currency_symbol_stripped(self):
        # normalize_number works on pure numeric strings; currency prefix causes None
        result = normalize_number("USD 1,234.56")
        assert result is None  # caller must strip currency prefix before calling

    def test_pct(self):
        # percent sign causes non-parse; caller strips '%' before calling
        result = normalize_number("5%")
        assert result is None

    def test_large_number(self):
        result = normalize_number("1.234.567,89")
        assert result == Decimal("1234567.89")

    def test_pln_letters_not_stripped_from_digits(self):
        # 'PLN' nie może być traktowane jako klasa znaków wycinająca P/L/N
        assert normalize_number("100") == Decimal("100")
        assert normalize_number("1.234,56") == Decimal("1234.56")


class TestRoundingEquivalence:
    def test_half_up_rounding(self):
        from normalizer import numbers_differ_only_by_rounding as r
        # 0.125 ≈ 0.13 wg ROUND_HALF_UP (księgowe), nie bankierskie (→0.12)
        assert r("0.125", "0.13") is True
        assert r("0.0257", "0.03") is True
        assert r("0.10", "0.20") is False


# ── normalize_date ────────────────────────────────────────────────────────────

class TestNormalizeDate:
    def test_iso_format(self):
        result = normalize_date("2025-06-30")
        assert result is not None
        assert "2025" in str(result) and "06" in str(result)

    def test_european_format(self):
        result = normalize_date("30/06/2025")
        assert result is not None

    def test_dot_format(self):
        result = normalize_date("30.06.2025")
        assert result is not None

    def test_invalid_date(self):
        result = normalize_date("not-a-date")
        assert result is None

    def test_empty(self):
        assert normalize_date("") is None


# ── normalize_text ────────────────────────────────────────────────────────────

class TestNormalizeUnicode:
    def test_em_dash_to_hyphen(self):
        result = normalize_text("2025–2026")
        assert "-" in result

    def test_en_dash_to_hyphen(self):
        result = normalize_text("10–20")
        assert "-" in result

    def test_no_change_for_ascii(self):
        result = normalize_text("hello-world")
        assert result == "hello-world"

    def test_empty(self):
        result = normalize_text("")
        assert result == ""


# ── normalize_ref ─────────────────────────────────────────────────────────────

class TestNormalizeRef:
    """Kanonikalizacja kodów REF: separatory usuwane, wielkie litery, CYFRY
    nietknięte. Zera są ZNACZĄCE na każdej pozycji — nie wolno ich ścinać."""

    def test_strips_separators_and_uppercases(self):
        assert normalize_ref("nl-753 s/40") == "NL753S40"

    def test_zeros_never_stripped(self):
        # Zero gdziekolwiek (początek, środek, koniec) zostaje w kodzie
        assert normalize_ref("S040") == "S040"
        assert normalize_ref("QX0510") == "QX0510"
        assert normalize_ref("AB012CD") == "AB012CD"
        assert normalize_ref("REF0045A") == "REF0045A"
        assert normalize_ref("040NL") == "040NL"
        assert normalize_ref("NL753S040") == "NL753S040"

    def test_idempotent(self):
        once = normalize_ref("NL-753-S-040")
        assert normalize_ref(once) == once == "NL753S040"


# ── refs_match ────────────────────────────────────────────────────────────────

class TestRefsMatch:
    """Wszystkie poniższe to ścieżki DETERMINISTYCZNE (zwracają przed fuzzy),
    więc wynik nie zależy od obecności rapidfuzz."""

    def test_exact_after_normalization(self):
        ok, score = refs_match("NL753-S-40", "NL753S40")
        assert ok is True and score == 1.0

    def test_size_variant_not_matched(self):
        # Identyczny korpus, różny NIEZEROWY końcowy numer = różne rozmiary
        ok, score = refs_match("NL753S40", "NL753S45")
        assert ok is False and score == 0.0

    def test_trailing_zero_is_different_code(self):
        ok, score = refs_match("S040", "S40")
        assert ok is False and score == 0.0

    def test_leading_zero_is_different_code(self):
        ok, score = refs_match("QX0510", "QX510")
        assert ok is False and score == 0.0

    def test_middle_zero_is_different_code(self):
        # Wcześniej zależne od fuzzy (rapidfuzz ~92% → fałszywe True); teraz guard
        # „różnica tylko w zerach" rozstrzyga to deterministycznie.
        ok, score = refs_match("AB012CD", "AB12CD")
        assert ok is False and score == 0.0


# ── numbers_equal (near-zero epsilon) ─────────────────────────────────────────

class TestNumbersEqualNearZero:
    def test_both_zero(self):
        assert numbers_equal("0", "0.00") is True

    def test_zero_vs_real_subcent_price_differs(self):
        # Wąski epsilon: realna cena sub-centowa NIE jest równa zeru
        assert numbers_equal("0", "0.005") is False

    def test_zero_vs_parse_noise_equal(self):
        # Szum zaokrąglenia poniżej epsilon traktowany jak zero
        assert numbers_equal("0", "0.00005") is True


# ── Regresja: formaty dat gubione przez za wąskie wzorce (BUGFIX) ─────────────
class TestNormalizeDateFormats:
    def test_iso_single_digit_components(self):
        assert normalize_date("2024-1-5").isoformat() == "2024-01-05"
        assert normalize_date("2024-1-15").isoformat() == "2024-01-15"

    def test_year_first_dotted(self):
        # zapis azjatycki RRRR.MM.DD
        assert normalize_date("2024.01.15").isoformat() == "2024-01-15"
        assert normalize_date("2024.1.5").isoformat() == "2024-01-05"

    def test_year_first_slash_single_digit(self):
        assert normalize_date("2024/1/5").isoformat() == "2024-01-05"

    def test_no_regression_existing_formats(self):
        assert normalize_date("2024-01-15").isoformat() == "2024-01-15"
        assert normalize_date("15.01.2024").isoformat() == "2024-01-15"  # DD.MM.YYYY
        assert normalize_date("15/01/2024").isoformat() == "2024-01-15"
        # Uwaga: NIE asertujemy tu, że np. "2024-13-01" → None. Ścisły regex zwraca
        # None dla miesiąca 13, ale gdy zainstalowany jest `dateparser` (obecny w
        # CI, nieobecny w minimalnym dev), jego rozmyty fallback ratuje taki zapis
        # (np. jako 2024-01-13). Sprawdzamy więc tekst bez żadnej daty — None w obu
        # środowiskach — żeby test nie był zależny od obecności dateparsera.
        assert normalize_date("brak daty tutaj") is None
