"""Faza 4 portalu: browse() dołącza status pliku i filtruje pominięte (hide_ignored)."""
import app as _app_mod
import artwork_index as _ai
import artwork_manage as _am


def _seed(db):
    _ai.ensure_table(db)
    _am.ensure_status_table(db)
    db.execute("DELETE FROM artwork_index")
    db.execute("DELETE FROM artwork_file_status")
    rows = [
        ("BT100", "BT-100", "bt100_carton.pdf", "z/a.pdf", "karton", "A", 1, ""),
        ("BT200", "BT-200", "bt200_pouch.pdf", "z/b.pdf", "op", "A", 1, ""),
        ("XY777", "XY-777", "xy777_single.pdf", "z/c.pdf", "sztuka", "", 0, ""),
    ]
    for r in rows:
        db.execute("INSERT INTO artwork_index(ref_norm,ref_code,filename,rel_path,"
                   "packaging_type,revision,revision_rank,ean) VALUES(?,?,?,?,?,?,?,?)", r)
    db.commit()


def test_browse_attaches_status_default_active():
    db = _app_mod.get_db()
    try:
        _seed(db)
        res = _ai.browse(db)
        assert res["total"] == 3
        assert all(r["status"] == "active" for r in res["rows"])
    finally:
        db.close()


def test_browse_reflects_ignored_status():
    db = _app_mod.get_db()
    try:
        _seed(db)
        _am.set_file_status(db, "z/b.pdf", "ignored", user="t")
        rows = {r["rel_path"]: r["status"] for r in _ai.browse(db)["rows"]}
        assert rows["z/b.pdf"] == "ignored"
        assert rows["z/a.pdf"] == "active"
    finally:
        db.close()


def test_browse_hide_ignored_excludes():
    db = _app_mod.get_db()
    try:
        _seed(db)
        _am.set_file_status(db, "z/b.pdf", "ignored", user="t")
        _am.set_file_status(db, "z/c.pdf", "archived", user="t")
        res = _ai.browse(db, hide_ignored=True)
        paths = {r["rel_path"] for r in res["rows"]}
        assert paths == {"z/a.pdf"}
        assert res["total"] == 1
        # bez filtra nadal widać wszystkie
        assert _ai.browse(db, hide_ignored=False)["total"] == 3
    finally:
        db.close()


def test_browse_hide_ignored_combines_with_query():
    db = _app_mod.get_db()
    try:
        _seed(db)
        _am.set_file_status(db, "z/a.pdf", "ignored", user="t")
        # q łapie BT-100 (ignored) → z hide_ignored znika
        assert _ai.browse(db, q="BT-100", hide_ignored=True)["total"] == 0
        assert _ai.browse(db, q="BT-100", hide_ignored=False)["total"] == 1
    finally:
        db.close()
