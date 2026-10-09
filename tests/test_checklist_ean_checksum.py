"""Regresja: pozycja listy kontrolnej „Suma kontrolna kodów kreskowych (GS1)”
ocenia wyłącznie sumy kontrolne — nie zgodność EAN A↔B ani kod kreskowy↔tekst."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from artwork_report_engine import build_report_from_comparison
from barcode_validator import validate_artwork_barcodes

VALID_A = "5901234123457"
VALID_B = "4006381333931"
INVALID = "5901234123458"


def _checksum_item(ocr_a: str, ocr_b: str) -> dict:
    rep = validate_artwork_barcodes(ocr_a, ocr_b)
    report = build_report_from_comparison({"barcode_report": rep, "field_report": []})
    return next(c for c in report.checklist if c["key"] == "barcode_checksum")


def test_valid_but_different_eans_are_not_a_checksum_error():
    item = _checksum_item(f"EAN {VALID_A}", f"EAN {VALID_B}")
    assert item["status"] == "ok", item


def test_invalid_checksum_is_still_reported():
    item = _checksum_item(f"EAN {VALID_A}", f"EAN {INVALID}")
    assert item["status"] == "error", item
    assert INVALID in item["note"]
