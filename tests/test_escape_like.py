"""
tests/test_escape_like.py — escapowanie wartości do bezpiecznego LIKE.

Uruchom: pytest tests/
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db import escape_like


def test_escapes_wildcards_and_backslash():
    assert escape_like("a_b%c\\") == "a\\_b\\%c\\\\"


def test_plain_text_unchanged():
    assert escape_like("ABC123") == "ABC123"


def test_none_and_empty():
    assert escape_like(None) == ""
    assert escape_like("") == ""


def test_only_wildcards():
    assert escape_like("%_") == "\\%\\_"
