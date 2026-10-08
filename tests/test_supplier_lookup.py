"""tests/test_supplier_lookup.py — wyszukiwanie dostawcy jest bez rozróżniania
wielkości liter (BUGFIX: 'shieldco' nie znajdował 'SHIELDCO', 'fg'≠'FG')."""
import sys, os, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import supplier_master as SUP


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE suppliers (id INTEGER PRIMARY KEY, code TEXT, name TEXT, "
              "producer_code TEXT, active INTEGER DEFAULT 1)")
    c.execute("INSERT INTO suppliers(code,name,producer_code,active) VALUES('SHIELDCO','Shieldco','FG',1)")
    c.commit()
    return c


def test_get_supplier_case_insensitive():
    c = _conn()
    assert SUP.get_supplier(c, "SHIELDCO")["code"] == "SHIELDCO"
    assert SUP.get_supplier(c, "shieldco")["code"] == "SHIELDCO"
    assert SUP.get_supplier(c, " Shieldco ")["code"] == "SHIELDCO"
    assert SUP.get_supplier(c, "NOPE") is None


def test_supplier_for_producer_case_insensitive():
    c = _conn()
    assert SUP.supplier_for_producer(c, "FG")["code"] == "SHIELDCO"
    assert SUP.supplier_for_producer(c, "fg")["code"] == "SHIELDCO"
    assert SUP.supplier_for_producer(c, "") is None
