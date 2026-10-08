"""tests/test_ensure_memo.py — memoizacja ensure_* per-połączenie.

Na PostgreSQL ciężkie DDL (CREATE + ALTER + commity) nie może wykonywać się
przy każdym getterze w pętli po REF-ach. Guard memoizuje po atrybucie połączenia.
sqlite3.Connection nie przyjmuje atrybutów (więc ensure leci za każdym razem —
tanie lokalnie), ale wrapper _PGConnection już tak — i to tam liczy się efekt.
Tu symulujemy połączenie przyjmujące atrybuty i sprawdzamy, że drugie wywołanie
ensure NIE odpala ponownie DDL.
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import material_master as mm
import uom
import artwork_index as ai


class _AttrConn:
    """Owija sqlite3.Connection, ale (w przeciwieństwie do niej) przyjmuje
    dowolne atrybuty — jak produkcyjny _PGConnection. Liczy wywołania execute."""
    def __init__(self):
        self._c = sqlite3.connect(":memory:")
        self._c.row_factory = sqlite3.Row
        self.exec_count = 0

    def execute(self, sql, params=()):
        self.exec_count += 1
        return self._c.execute(sql, params)

    def commit(self):
        self._c.commit()

    def rollback(self):
        self._c.rollback()


def test_material_master_ensure_memoized_on_attr_conn():
    db = _AttrConn()
    mm.ensure_table(db)
    assert getattr(db, "_mm_ensured", False) is True
    after_first = db.exec_count
    assert after_first > 0
    # Drugie wywołanie short-circuit → zero kolejnych execute().
    mm.ensure_table(db)
    assert db.exec_count == after_first


def test_uom_and_artwork_ensure_memoized():
    db = _AttrConn()
    uom.ensure_table(db)
    n = db.exec_count
    uom.ensure_table(db)
    assert db.exec_count == n
    ai.ensure_table(db)
    m = db.exec_count
    ai.ensure_table(db)
    assert db.exec_count == m


def test_sqlite_connection_still_ensures_each_call():
    # Na czystym sqlite3.Connection atrybut się nie ustawia → ensure działa jak
    # dotąd (idempotentny CREATE IF NOT EXISTS), brak wyjątku.
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    mm.ensure_table(db)
    mm.ensure_table(db)        # nie może rzucić
    assert mm.get_material(db, "NOPE") is None
