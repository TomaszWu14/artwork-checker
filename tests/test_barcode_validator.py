"""
tests/test_barcode_validator.py — testy jednostkowe dla barcode_validator.py

Uruchom: pytest tests/
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from barcode_validator import validate_ean13, parse_gs1_128


# ── validate_ean13 ────────────────────────────────────────────────────────────

class TestValidateEAN13:
    """validate_ean13() returns a dict with 'valid' bool key."""

    def test_valid_ean(self):
        result = validate_ean13("4006381333931")  # standard GS1 test EAN
        assert result["valid"] is True

    def test_invalid_checksum(self):
        result = validate_ean13("4006381333930")  # last digit wrong (should be 1)
        assert result["valid"] is False
        assert result["error"] is not None

    def test_too_short(self):
        result = validate_ean13("590000180134")
        assert result["valid"] is False

    def test_too_long(self):
        result = validate_ean13("59000018013440")
        # 14 digits — GTIN-14 with indicator != 0 or stripped to EAN-13
        assert "valid" in result

    def test_non_digits(self):
        result = validate_ean13("590000180134X")
        assert result["valid"] is False

    def test_empty(self):
        result = validate_ean13("")
        assert result["valid"] is False

    def test_all_zeros(self):
        result = validate_ean13("0000000000000")
        assert result["valid"] is True


# ── parse_gs1_128 ─────────────────────────────────────────────────────────────

class TestParseGS1128:
    """parse_gs1_128() returns a dict with 'gtin', 'lot', 'exp_date', etc."""

    def test_gtin_ai(self):
        result = parse_gs1_128("(01)05900001801344")
        assert result is not None
        assert result.get("gtin") is not None

    def test_lot_ai(self):
        result = parse_gs1_128("(10)LOT12345")
        assert result is not None
        assert result.get("lot") == "LOT12345"

    def test_expiry_ai(self):
        result = parse_gs1_128("(17)251231")
        assert result is not None
        assert result.get("exp_date") is not None

    def test_combined(self):
        result = parse_gs1_128("(01)05900001801344(10)LOT001(17)251231")
        assert result is not None
        assert result.get("gtin") is not None
        assert result.get("lot") is not None

    def test_empty(self):
        result = parse_gs1_128("")
        # Returns empty result dict, not None
        assert isinstance(result, dict)
        assert result.get("gtin") is None

    def test_continuous_recovers_exp_after_variable_lot(self):
        # Format ciągły bez nawiasów: GTIN(01,14) + LOT(10,zmienna) + EXP(17,6).
        # Wcześniej skaner zjadał LOT za daleko i gubił datę ważności.
        result = parse_gs1_128("01" + "05900002608103" + "10ABC" + "17" + "310510")
        assert result.get("gtin") == "05900002608103"
        assert result.get("lot") == "ABC"
        assert result.get("exp_date") == "2031-05-10"

    def test_continuous_fixed_then_variable_last(self):
        # GTIN(14) + EXP(17,6) + LOT(zmienna, ostatnia) — data nie ginie.
        result = parse_gs1_128("01" + "05900002608103" + "17" + "310510" + "10XYZ")
        assert result.get("gtin") == "05900002608103"
        assert result.get("exp_date") == "2031-05-10"
        assert result.get("lot", "").startswith("XYZ")
