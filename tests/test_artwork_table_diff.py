"""
tests/test_artwork_table_diff.py — enumeracja różnic liczbowych w trybie „tabela".

Sprawdza, że porównanie pola tabelarycznego (np. Chemical Permeation) wypisuje
KAŻDĄ różniącą się komórkę, a nie tylko jeden flag boolean.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import artwork_comparator as ac


def test_numeric_diffs_list_enumerates_every_cell():
    # Master vs supplier: K, poziom, P%, T% — wszystkie cztery się różnią.
    nums_a = [-25.6, 2, 17.0, 3.1]
    nums_b = [1.4, 6, 24.0, 10.9]
    diffs = ac._numeric_diffs_list(nums_a, nums_b)
    assert diffs == [(-25.6, 1.4), (2, 6), (17.0, 24.0), (3.1, 10.9)]


def test_numeric_diffs_list_respects_tolerance():
    # Drobna różnica w granicach tolerancji nie jest zgłaszana.
    diffs = ac._numeric_diffs_list([10.0, 20.0], [10.05, 25.0], tolerance=0.1)
    assert diffs == [(20.0, 25.0)]


def test_numeric_diffs_list_reports_missing_value():
    # OCR pominął jedną liczbę po stronie dostawcy → para (a, None).
    diffs = ac._numeric_diffs_list([1.0, 2.0, 3.0], [1.0, 2.0])
    assert diffs == [(3.0, None)]


def test_numeric_diffs_list_identical_is_empty():
    assert ac._numeric_diffs_list([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == []


def test_extract_numbers_from_table_text():
    # Tekst OCR wieloliniowy z wartościami procentowymi i poziomami.
    txt = "K -25,6\nLevel 2\nP 17,0\nT 3,1"
    nums = ac._extract_numbers_from_text(txt)
    assert -25.6 in nums and 2 in nums and 17.0 in nums and 3.1 in nums


def test_distinctive_num_skips_small_integers():
    assert ac._distinctive_num(2) is False       # poziom — pomijamy (powtarza się)
    assert ac._distinctive_num(6) is False
    assert ac._distinctive_num(17.0) is True      # ≥10
    assert ac._distinctive_num(1.4) is True        # ma część dziesiętną
    assert ac._distinctive_num(-25.6) is True


def test_locate_value_boxes_numeric_match(monkeypatch):
    rows = [
        ("K 40% Sodium Hydroxide 6 -25,6", (10, 10, 40, 15)),
        ("P 30% Hydrogen 2 17,0",          (10, 20, 40, 25)),
        ("Level 6 > 480 min",              (10, 40, 40, 45)),
        ("www.example.com",                   (10, 50, 40, 55)),
    ]
    monkeypatch.setattr(ac, "_paddle_readtext_with_boxes", lambda crop: rows)
    # '17' dopasuje '17,0'; '-25.6' dopasuje '-25,6'; nic poza tym.
    boxes = ac._locate_value_boxes(object(), {"-25.6", "17"})
    assert (10, 10, 40, 15) in boxes and (10, 20, 40, 25) in boxes
    assert (10, 40, 40, 45) not in boxes and (10, 50, 40, 55) not in boxes
