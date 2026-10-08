"""
tests/test_ai_json.py — odporne wyłuskiwanie obiektu JSON z odpowiedzi modelu.

Uruchom: pytest tests/
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from ai_validator import _extract_json_object


def test_clean_object():
    assert _extract_json_object('{"a": 1}') == {"a": 1}


def test_trailing_garbage_after_object():
    assert _extract_json_object('{"a": 1} oraz dodatkowy tekst') == {"a": 1}


def test_leading_prose_before_object():
    assert _extract_json_object('Oto wynik: {"ok": true}') == {"ok": True}


def test_nested_objects():
    assert _extract_json_object('{"x": {"y": 2}} ...') == {"x": {"y": 2}}


def test_braces_inside_strings_ignored():
    assert _extract_json_object('{"s": "ma } w środku"}') == {"s": "ma } w środku"}


def test_no_object():
    assert _extract_json_object("brak json tutaj") is None


def test_malformed_returns_none():
    assert _extract_json_object('{"a": ') is None
