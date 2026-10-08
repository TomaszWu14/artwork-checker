"""
tests/test_artwork_box_verify.py — weryfikacja boxów po pikselach.

Box OCR-owy rysowany jest tylko wtedy, gdy piksele w danym regionie naprawdę się
różnią — to eliminuje fałszywe boxy z błędów OCR (np. 'klasi' vs 'klasy', gdzie na
obu wydrukach jest to samo).
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

pytest.importorskip("PIL")
np = pytest.importorskip("numpy")
from PIL import Image

import artwork_comparator as ac


def _img(color):
    return Image.new("RGB", (200, 100), color)


def test_region_identical_pixels_not_flagged():
    a = _img((255, 255, 255))
    b = _img((255, 255, 255))
    # Cały region biały na obu — brak różnicy → box odrzucony.
    assert ac._region_pixels_differ(a, b, (10, 10, 90, 90)) is False


def test_region_differing_pixels_flagged():
    a = _img((255, 255, 255))
    b = _img((255, 255, 255))
    # Wypełnij region B czarnym prostokątem — realna różnica → box zachowany.
    arr = np.array(b)
    arr[20:80, 40:160] = 0
    b = Image.fromarray(arr)
    assert ac._region_pixels_differ(a, b, (10, 10, 90, 90)) is True


def test_region_verify_fails_safe():
    # Błędne dane → True (nie usuwamy potencjalnie realnej różnicy).
    assert ac._region_pixels_differ(None, None, (0, 0, 10, 10)) is True


def test_dedupe_pct_boxes():
    boxes = [(10, 10, 20, 20), (10.2, 10.1, 20.3, 20.2), (50, 50, 60, 60)]
    out = ac._dedupe_pct_boxes(boxes)
    assert len(out) == 2
