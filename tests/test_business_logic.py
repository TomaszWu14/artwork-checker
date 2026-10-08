"""
tests/test_business_logic.py — testy jednostkowe kluczowej logiki biznesowej
w normalizer.py: porównania liczb, walidacja sum (qty×price=net), matching REF
i równoważność terminów płatności.

Uruchom: pytest tests/
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from normalizer import (
    numbers_equal,
    numbers_differ_only_by_rounding,
    normalize_ref,
    refs_match,
    normalize_company_name,
    normalize_payment_terms,
    payment_terms_equal,
    validate_checksum,
)


# ── numbers_equal ─────────────────────────────────────────────────────────────

class TestNumbersEqual:
    def test_exact(self):
        assert numbers_equal("100", "100") is True

    def test_mixed_locale_formats_equal(self):
        # US 13,500.00 vs EU 13.500,00 must compare equal
        assert numbers_equal("13,500.00", "13.500,00") is True

    def test_within_tolerance(self):
        # 0.5% drift accepted at 1% tolerance, rejected at 0
        assert numbers_equal("1000", "1005", tolerance_pct=1.0) is True
        assert numbers_equal("1000", "1005") is False

    def test_unparseable_returns_none(self):
        assert numbers_equal("abc", "100") is None

    def test_near_zero_absolute_tolerance(self):
        # Tylko szum zaokrąglenia poniżej epsilona jest "równy zeru"…
        assert numbers_equal("0", "0.00005") is True
        # …a realna cena sub-centowa różni się od zera (nie maskujemy braku ceny).
        assert numbers_equal("0", "0.005") is False
        assert numbers_equal("0", "5") is False


# ── numbers_differ_only_by_rounding ───────────────────────────────────────────

class TestRounding:
    def test_rounding_difference(self):
        assert numbers_differ_only_by_rounding("0.0257", "0.03", decimal_places=2) is True

    def test_real_difference(self):
        assert numbers_differ_only_by_rounding("0.02", "0.03", decimal_places=2) is False


# ── normalize_ref / refs_match ────────────────────────────────────────────────

class TestRef:
    def test_separators_stripped(self):
        assert normalize_ref("NL753-S-40") == normalize_ref("NL753S40")

    def test_zeros_are_significant(self):
        # Zera są ZNACZĄCE — NIE są ścinane (S040 ≠ S40 to różne kody/rozmiary)
        assert normalize_ref("NL753S040") == "NL753S040"
        assert normalize_ref("NL753S040") != normalize_ref("NL753S40")
        assert refs_match("NL753S040", "NL753S40")[0] is False

    def test_case_and_spaces(self):
        assert normalize_ref("  nl753 s 40 ") == "NL753S40"

    def test_refs_match_exact_after_normalization(self):
        ok, conf = refs_match("NL753-S-40", "nl753s40")
        assert ok is True
        assert conf == 1.0

    def test_refs_match_clearly_different(self):
        ok, conf = refs_match("ABC123", "XYZ999")
        assert ok is False
        assert conf == 0.0

    def test_refs_match_empty(self):
        assert refs_match("", "NL753") == (False, 0.0)


# ── normalize_company_name ────────────────────────────────────────────────────

class TestCompanyName:
    def test_legal_form_normalized(self):
        a = normalize_company_name("ACME SP. Z O.O.")
        b = normalize_company_name("Acme Sp z o o")
        assert a == b
        assert "SPZOO" in a


# ── payment terms ─────────────────────────────────────────────────────────────

class TestPaymentTerms:
    def test_sap_code_mapped(self):
        assert normalize_payment_terms("IW04") == "30 days"

    def test_days_extracted_from_text(self):
        assert normalize_payment_terms("Payment within 45 days") == "45 days"

    def test_equal_via_sap_map(self):
        # IW04 → '30 days' equals literal '30 days'
        assert payment_terms_equal("IW04", "30 days") is True

    def test_equal_via_day_count(self):
        assert payment_terms_equal("Payment within 30 days", "30 days") is True

    def test_not_equal(self):
        assert payment_terms_equal("30 days", "60 days") is False


# ── validate_checksum (qty × price = net) ─────────────────────────────────────

class TestValidateChecksum:
    def test_net_fields_sum_to_declared(self):
        items = [{"net": "100.00"}, {"net": "250.50"}]
        res = validate_checksum(items, "350.50")
        assert res["valid"] is True
        assert res["diff"] == 0.0

    def test_qty_times_price_when_net_absent(self):
        items = [{"qty": "10", "price": "5.00"}, {"qty": "2", "price": "3.00"}]
        res = validate_checksum(items, "56.00")  # 50 + 6
        assert res["valid"] is True

    def test_mismatch_flagged(self):
        items = [{"net": "100.00"}]
        res = validate_checksum(items, "150.00")
        assert res["valid"] is False
        assert res["diff"] == 50.0

    def test_unparseable_total(self):
        res = validate_checksum([{"net": "10"}], "n/a")
        assert res["valid"] is None
        assert res["error"]


class TestChecksumTolerance:
    def test_drift_fails_at_default_passes_at_higher_tolerance(self):
        items = [{"net": "1000.00"}]
        # 0.2% drift
        assert validate_checksum(items, "1002.00")["valid"] is False
        assert validate_checksum(items, "1002.00", tolerance_pct=0.3)["valid"] is True
