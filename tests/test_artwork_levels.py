"""tests/test_artwork_levels.py — dobór i potwierdzanie artworków per poziom opakowania.

Ten sam REF ma różne pliki dla OP (pouch) i KAR (carton). Sprawdzamy, że:
- lookup_best wybiera plik właściwy dla poziomu,
- KAR nie dostaje pouch (gdy nie ma cartonu → None),
- potwierdzenie jest per (REF, poziom) i da się je cofnąć.
"""
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import artwork_index as ai


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    return db


def _entries():
    return [
        {"filename": "BT-PC6060_pouch.pdf", "rel_path": "Z/BT-PC6060_pouch.pdf",
         "source_mtime": "2026-05-10"},
        {"filename": "BT-PC6060_carton.pdf", "rel_path": "Z/BT-PC6060_carton.pdf",
         "source_mtime": "2026-05-10"},
    ]


def test_lookup_best_picks_level_specific_file():
    db = _db()
    ai.sync_entries(db, _entries())
    assert "carton" in ai.lookup_best(db, "BT-PC6060", "KAR")["filename"]
    assert "pouch" in ai.lookup_best(db, "BT-PC6060", "OP")["filename"]


def test_carton_not_matched_to_pouch_only():
    db = _db()
    ai.sync_entries(db, [{"filename": "BT-PC6060_pouch.pdf",
                          "rel_path": "Z/BT-PC6060_pouch.pdf", "source_mtime": "2026-05-10"}])
    # Tylko pouch w indeksie — dla KAR nie zwracamy błędnie pouch.
    assert ai.lookup_best(db, "BT-PC6060", "KAR") is None
    assert ai.lookup_best(db, "BT-PC6060", "OP") is not None


def test_confirm_and_unconfirm_per_level():
    db = _db()
    ai.sync_entries(db, _entries())
    ai.confirm_artwork(db, "BT-PC6060", "Z/BT-PC6060_pouch.pdf", level="OP")
    ai.confirm_artwork(db, "BT-PC6060", "Z/BT-PC6060_carton.pdf", level="KAR")
    assert "pouch" in ai.get_confirmed(db, "BT-PC6060", "OP")["filename"]
    assert "carton" in ai.get_confirmed(db, "BT-PC6060", "KAR")["filename"]
    # Cofnięcie tylko KAR nie rusza OP.
    ai.unconfirm_artwork(db, "BT-PC6060", "KAR")
    assert ai.get_confirmed(db, "BT-PC6060", "KAR") is None
    assert ai.get_confirmed(db, "BT-PC6060", "OP") is not None


def test_legacy_confirmation_not_shown_for_conflicting_level():
    db = _db()
    ai.sync_entries(db, _entries())
    # Stare potwierdzenie bez poziomu (pouch) — nie pokazuj go jako master kartonu.
    ai.confirm_artwork(db, "BT-PC6060", "Z/BT-PC6060_pouch.pdf", level="")
    assert ai.get_confirmed(db, "BT-PC6060", "OP") is not None     # zgodny poziom
    assert ai.get_confirmed(db, "BT-PC6060", "KAR") is None        # kolidujący poziom


def test_suggest_by_text_prefers_level_file():
    db = _db()
    ai.sync_entries(db, _entries())
    sug = ai.suggest_by_text(db, "BT-PC6060 podkład", limit=5, level="KAR")
    assert sug and "carton" in sug[0]["filename"]
