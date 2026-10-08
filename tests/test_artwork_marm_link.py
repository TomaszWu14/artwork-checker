"""MARM-01/02: trwałe powiązanie artwork (REF) → wiersz MARM (ref, unit).

Wzorzec propose/correct/remember — jak uczenie orientacji 3D: ustawiane ręcznie,
zawsze nadpisywalne, czytane jako domyślne przy otwarciu generatora 3D z MARM.
"""
import sqlite3

import migrate_db
import app as _app_mod
import artwork_marm_link as aml

migrate_db.run()


def test_migration_creates_table():
    """Tabela artwork_marm_link żyje w migrate_db.py — musi powstać w bazie appki."""
    db = _app_mod.get_db()
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='artwork_marm_link'"
    ).fetchone()
    assert row is not None
    migrate_db.run()  # idempotencja — ponowna migracja nie rzuca


def test_roundtrip_through_app_db():
    db = _app_mod.get_db()
    db.execute("DELETE FROM artwork_marm_link WHERE art_ref_norm = 'BTPC6060'")
    db.commit()
    aml.set_link(db, "BT-PC6060", "300000", "OPZ", "admin")
    assert aml.get_link(db, "BT-PC6060")["marm_unit"] == "OPZ"


def _db():
    """Świeże połączenie SQLite z samą tabelą linku (bez pełnego migrate)."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE artwork_marm_link (
        art_ref_norm TEXT PRIMARY KEY,
        marm_ref     TEXT DEFAULT '',
        marm_unit    TEXT DEFAULT '',
        updated_by   TEXT,
        updated_at   TEXT DEFAULT (datetime('now'))
    )""")
    return con


def test_get_missing_returns_none():
    assert aml.get_link(_db(), "BT-PC6060") is None


def test_set_then_get_roundtrip():
    db = _db()
    aml.set_link(db, "BT-PC6060", "300000", "KAR", "admin")
    link = aml.get_link(db, "BT-PC6060")
    assert link is not None
    assert link["marm_ref"] == "300000"
    assert link["marm_unit"] == "KAR"


def test_lookup_is_ref_norm_insensitive():
    """Powiązanie kluczowane po znormalizowanym REF — 'bt pc6060' == 'BT-PC6060'."""
    db = _db()
    aml.set_link(db, "BT-PC6060", "300000", "KAR", "admin")
    assert aml.get_link(db, "bt pc6060")["marm_unit"] == "KAR"


def test_overwrite_remembers_latest():
    """Korekta (propose/correct/remember): druga ustawka nadpisuje, nie duplikuje."""
    db = _db()
    aml.set_link(db, "BT-PC6060", "300000", "KAR", "admin")
    aml.set_link(db, "BT-PC6060", "300000", "OP", "admin")
    assert aml.get_link(db, "BT-PC6060")["marm_unit"] == "OP"
    n = db.execute("SELECT COUNT(*) FROM artwork_marm_link").fetchone()[0]
    assert n == 1


def test_empty_ref_not_stored():
    db = _db()
    aml.set_link(db, "", "300000", "KAR", "admin")
    assert db.execute("SELECT COUNT(*) FROM artwork_marm_link").fetchone()[0] == 0
