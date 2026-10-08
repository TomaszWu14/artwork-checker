import sqlite3, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import supplier_master as sm

def _db():
    db = sqlite3.connect(":memory:"); db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE suppliers(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, "
               "name TEXT NOT NULL, country TEXT DEFAULT 'XX', currency TEXT DEFAULT 'USD', "
               "detect_keywords_json TEXT DEFAULT '[]', notes TEXT DEFAULT '', active INTEGER DEFAULT 1)")
    return db

def test_import_and_link():
    db = _db()
    rows = [
        ["kod_dostawcy","nazwa","kod_producenta","kraj","waluta","incoterms",
         "warunki_platnosci","lead_time_dni","slowa_kluczowe","email"],
        ["SHIELDCO","Shieldco Medical","OML-CHINA","CN","USD","FOB Shanghai",
         "30 days","45","Shieldco;FG-","li@shieldco.cn"],
    ]
    assert sm.import_workbook(db, rows) == {"imported": 1}
    s = sm.get_supplier(db, "SHIELDCO")
    assert s["producer_code"] == "OML-CHINA"
    assert s["incoterms"] == "FOB Shanghai"
    assert s["lead_time_days"] == 45
    import json
    assert json.loads(s["detect_keywords_json"]) == ["Shieldco", "FG-"]
    assert sm.supplier_for_producer(db, "OML-CHINA")["code"] == "SHIELDCO"
    assert sm.supplier_for_producer(db, "NOPE") is None

def test_requires_code_and_name():
    db = _db()
    rows = [["kod_dostawcy","nazwa"], ["", "X"], ["C1", ""], ["C2", "Ok"]]
    assert sm.import_workbook(db, rows)["imported"] == 1


def test_lead_time_parses_first_integer_run():
    db = _db()
    sm.import_workbook(db, [
        ["kod_dostawcy", "nazwa", "lead_time_dni"],
        ["A", "Aco", "2-3 dni"],
        ["B", "Bco", "10.5"],
        ["C", "Cco", "ok. 45"],
    ])
    assert sm.get_supplier(db, "A")["lead_time_days"] == 2
    assert sm.get_supplier(db, "B")["lead_time_days"] == 10
    assert sm.get_supplier(db, "C")["lead_time_days"] == 45


def test_list_search_escapes_like_wildcards():
    db = _db()
    sm.import_workbook(db, [
        ["kod_dostawcy", "nazwa"],
        ["ACME", "Acme 100% Cotton"],
        ["OTHER", "Zupełnie inny"],
    ])
    # Dosłowne '%' nie może działać jak wildcard — trafia tylko w "Acme 100%…".
    hits = sm.list_suppliers(db, "100%")
    assert [h["code"] for h in hits] == ["ACME"]
    # Pusty wzorzec '%' nie zwraca wszystkiego przez przypadek.
    only_other = sm.list_suppliers(db, "inny")
    assert [h["code"] for h in only_other] == ["OTHER"]
