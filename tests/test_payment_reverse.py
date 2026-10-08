"""
tests/test_payment_reverse.py — odwrotne mapowanie terminu płatności → kody SAP.

Uruchom: pytest tests/
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from normalizer import payment_text_to_codes


def test_plain_days_returns_all_matching_codes():
    assert payment_text_to_codes("30 days") == ["IW04", "N030", "ZB30"]


def test_qualifier_preserved_not_collapsed():
    # 'after BL' must not collapse to the plain 30-day codes
    assert payment_text_to_codes("30 days after BL") == ["IW30"]


def test_input_is_code():
    assert payment_text_to_codes("IW04") == ["IW04"]


def test_free_text_day_count_fallback():
    assert payment_text_to_codes("Payment within 60 days") == ["IW07", "N060", "ZB60"]


def test_extra_map_merged():
    codes = payment_text_to_codes("custom term", {"ZZ99": "custom term"})
    assert "ZZ99" in codes


def test_no_match():
    assert payment_text_to_codes("xyz") == []


def test_empty():
    assert payment_text_to_codes("") == []
