"""
tests/test_gs1_gtin.py — walidacja cyfry kontrolnej GTIN i brakujących
obowiązkowych AI (UDI medyczne) w GS1-128.

Uruchom: pytest tests/
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from barcode_validator import gtin_check_digit_valid, parse_gs1_128


class TestGtinCheckDigit:
    def test_valid_ean13_as_gtin(self):
        assert gtin_check_digit_valid("5901234123457") is True

    def test_invalid_check_digit(self):
        assert gtin_check_digit_valid("5901234123450") is False

    def test_wrong_length(self):
        assert gtin_check_digit_valid("12345") is False

    def test_empty(self):
        assert gtin_check_digit_valid("") is False


class TestGs1Parsing:
    def test_gtin_valid_flag_and_no_missing(self):
        # GTIN-14 zbuilt from a valid GTIN-13 + recomputed check digit
        # (00)+5901234123457 -> recompute: use a known-good GTIN-14
        res = parse_gs1_128("(01)05901234123457(17)301231(10)LOT5")
        # 05901234123457 is GTIN-14; flag should be boolean, not None
        assert res["gtin"] == "05901234123457"
        assert isinstance(res["gtin_valid"], bool)
        # all three mandatory AIs present
        assert res["missing_mandatory_ai"] == []

    def test_missing_mandatory_ai_flagged(self):
        res = parse_gs1_128("(01)05901234123457")
        # LOT (10) and EXP (17) absent -> flagged
        assert len(res["missing_mandatory_ai"]) == 2

    def test_invalid_gtin_adds_error(self):
        res = parse_gs1_128("(01)05901234123450(17)301231(10)LOT5")
        assert res["gtin_valid"] is False
        assert any("GTIN" in e for e in res["errors"])


# ── Regresja: niemożliwe daty GS1 muszą być ZGŁOSZONE, nie cicho przepuszczone ──
def test_gs1_invalid_expiry_is_flagged():
    r = parse_gs1_128("(17)260230")   # 30 lutego — nie istnieje
    assert any("Nieprawid" in e for e in r["errors"]), r

def test_gs1_invalid_prod_date_is_flagged():
    r = parse_gs1_128("(11)260132")   # 32 stycznia
    assert any("Nieprawid" in e for e in r["errors"]), r

def test_gs1_end_of_month_dd00_still_valid():
    r = parse_gs1_128("(17)260200")   # DD=00 → ostatni dzień lutego
    assert r["exp_date"] == "2026-02-28"
    assert r["errors"] == []

def test_gs1_valid_date_no_error():
    r = parse_gs1_128("(17)260101")
    assert r["exp_date"] == "2026-01-01"
    assert r["errors"] == []
