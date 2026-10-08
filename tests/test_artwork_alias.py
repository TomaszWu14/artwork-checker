import sqlite3, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import artwork_index as ai
def _db():
    db=sqlite3.connect(":memory:"); db.row_factory=sqlite3.Row; ai.ensure_table(db); return db
def test_alias_backfill_and_lookup():
    db=_db()
    db.execute("INSERT INTO artwork_index(ref_norm,ref_code,filename,rel_path,revision_rank) "
               "VALUES('','','easycare_nitrile_carton_S_a100.pdf','Z/easycare_nitrile_carton_S_a100.pdf',2)")
    assert ai.lookup_best(db,'RNBS10001') is None
    r=ai.add_alias(db,'RNBS10001','easycare_nitrile_carton_S')
    assert r['matched']==1
    b=ai.lookup_best(db,'RNBS10001')
    assert b and b['ref_norm']=='RNBS10001'
def test_alias_applied_on_sync():
    db=_db()
    ai.add_alias(db,'RNBS10001','easycare_nitrile_carton_S')
    ai.sync_entries(db,[{'filename':'easycare_nitrile_carton_S_a100.pdf','rel_path':'Z/x.pdf'}])
    assert ai.lookup_best(db,'RNBS10001') is not None


def test_add_alias_replace_swaps_cleanly():
    import sqlite3
    import artwork_index as ai
    db = sqlite3.connect(":memory:"); db.row_factory = sqlite3.Row
    ai.ensure_table(db)
    for fn in ("easycare_carton_a100.pdf", "easycare_glove_a100_v2.pdf"):
        db.execute("INSERT INTO artwork_index(ref_norm,ref_code,filename,rel_path) VALUES('','',?,?)", (fn, "Z/"+fn))
    db.commit()
    # pierwszy wybór
    ai.add_alias(db, "RNBS10001", "easycare_carton_a100", replace=True)
    assert ai.lookup_best(db, "RNBS10001")["filename"] == "easycare_carton_a100.pdf"
    # podmiana błędnego wyboru — tylko jeden alias, nowy plik wygrywa, brak „ducha"
    ai.add_alias(db, "RNBS10001", "easycare_glove_a100_v2", replace=True)
    assert ai.lookup_best(db, "RNBS10001")["filename"] == "easycare_glove_a100_v2.pdf"
    assert len([a for a in ai.list_aliases(db) if a["ref_norm"] == "RNBS10001"]) == 1
