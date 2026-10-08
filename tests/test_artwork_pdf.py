"""
tests/test_artwork_pdf.py — eksport raportu artworku do PDF.

Pomijany automatycznie, gdy reportlab nie jest zainstalowany (lekkie CI).
Uruchom: pytest tests/
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

pytest.importorskip("reportlab")  # pełny eksport wymaga ReportLab

from artwork_report_engine import build_demo_report, export_to_pdf, ArtworkReport


def test_demo_report_pdf_is_valid():
    pdf = export_to_pdf(build_demo_report())
    assert pdf[:4] == b"%PDF"
    assert len(pdf) > 1000


def test_empty_report_does_not_crash():
    pdf = export_to_pdf(ArtworkReport())
    assert pdf[:4] == b"%PDF"
