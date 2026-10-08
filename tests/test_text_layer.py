"""
tests/test_text_layer.py — testy dla artwork_text_layer.find_text_as_graphic

Wykrywanie napisów wklejonych jako grafika (brak w warstwie tekstowej PDF).
Testujemy czysty rdzeń — bez PyMuPDF. Uruchom: pytest tests/
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from artwork_text_layer import find_text_as_graphic, _overlap_ratio


def test_real_text_not_flagged():
    """Napis pokryty boxem warstwy tekstowej PDF nie jest zgłaszany."""
    ocr = [("elastoDERM", (0.06, 0.06, 0.34, 0.11))]
    pdf = [(0.05, 0.05, 0.35, 0.12)]
    assert find_text_as_graphic(ocr, pdf) == []


def test_graphic_text_flagged():
    """Napis bez odpowiednika w warstwie tekstowej → zgłoszony, cover 0."""
    ocr = [("STERILE EO", (0.60, 0.80, 0.88, 0.87))]
    out = find_text_as_graphic(ocr, [(0.05, 0.05, 0.35, 0.12)])
    assert len(out) == 1
    assert out[0]["text"] == "STERILE EO"
    assert out[0]["cover"] == 0.0


def test_fully_rasterized_page():
    """Cała strona bez tekstu (0 boxów) → wszystkie realne napisy zgłoszone."""
    ocr = [("A", (0.0, 0.0, 0.3, 0.1)), ("B", (0.5, 0.5, 0.8, 0.6))]
    out = find_text_as_graphic(ocr, [])
    assert {o["text"] for o in out} == {"A", "B"}


def test_blank_and_noise_ignored():
    """Puste OCR i mikro-boxy (poniżej min_area) są pomijane."""
    ocr = [("   ", (0.1, 0.5, 0.2, 0.55)), ("x", (0.5, 0.5, 0.505, 0.505))]
    assert find_text_as_graphic(ocr, []) == []


def test_partial_cover_below_threshold_flagged():
    """Minimalne zachodzenie tekstu (<min_cover) nadal traktowane jak grafika."""
    ocr = [("LOT 123", (0.0, 0.0, 0.4, 0.1))]
    pdf = [(0.0, 0.0, 0.05, 0.1)]  # pokrycie ~12% < 30%
    assert len(find_text_as_graphic(ocr, pdf)) == 1


def test_overlap_ratio_no_intersection():
    assert _overlap_ratio((0, 0, 0.1, 0.1), (0.5, 0.5, 0.6, 0.6)) == 0.0


def test_overlap_ratio_full_cover():
    # box OCR w całości wewnątrz boxu tekstu → 1.0
    assert _overlap_ratio((0.1, 0.1, 0.2, 0.2), (0.0, 0.0, 1.0, 1.0)) == 1.0
