"""
tests/test_artwork_ocr_model.py — OCR używa szybkiego modelu (Haiku) niezależnie
od modelu analizy AI.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import artwork_comparator as ac


def test_ocr_model_defaults_to_haiku():
    os.environ.pop("ARTWORK_OCR_MODEL", None)
    assert "haiku" in ac._current_ocr_model().lower()


def test_ocr_model_env_override():
    os.environ["ARTWORK_OCR_MODEL"] = "claude-sonnet-4-6"
    try:
        assert ac._current_ocr_model() == "claude-sonnet-4-6"
    finally:
        os.environ.pop("ARTWORK_OCR_MODEL", None)


def test_ocr_model_independent_of_analysis_model():
    # Zmiana modelu analizy nie wpływa na model OCR.
    os.environ.pop("ARTWORK_OCR_MODEL", None)
    ac.set_ai_model("claude-opus-4-7")
    try:
        assert ac._current_ai_model() == "claude-opus-4-7"
        assert "haiku" in ac._current_ocr_model().lower()
    finally:
        ac.set_ai_model(ac._DEFAULT_AI_MODEL)
