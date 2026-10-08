"""#6/#20 Sprint 2: przeszukiwalna biblioteka masterów — browse() z filtrami i paginacją."""
import app as _app_mod
import artwork_index as _ai


def _seed(db):
    _ai.ensure_table(db)
    db.execute("DELETE FROM artwork_index")
    rows = [
        # ref_norm, ref_code, filename, rel_path, packaging_type, revision, revision_rank, ean
        ("BT100", "BT-100", "bt100_carton_rev_a.pdf", "z/bt100_a.pdf", "karton", "A", 1, "5900000000017"),
        ("BT100", "BT-100", "bt100_carton_rev_b.pdf", "z/bt100_b.pdf", "karton", "B", 2, "5900000000017"),
        ("BT200", "BT-200", "bt200_pouch_rev_a.pdf", "z/bt200_a.pdf", "op",     "A", 1, ""),
        ("XY777", "XY-777", "xy777_single.pdf",       "z/xy777.pdf",  "sztuka", "",  0, ""),
    ]
    for r in rows:
        db.execute(
            "INSERT INTO artwork_index(ref_norm,ref_code,filename,rel_path,packaging_type,"
            "revision,revision_rank,ean) VALUES(?,?,?,?,?,?,?,?)", r)
    db.commit()


def test_browse_all_and_pagination():
    db = _app_mod.get_db()
    try:
        _seed(db)
        res = _ai.browse(db, per_page=2, page=1)
        assert res["total"] == 4
        assert res["pages"] == 2
        assert len(res["rows"]) == 2
        res2 = _ai.browse(db, per_page=2, page=2)
        assert len(res2["rows"]) == 2
    finally:
        db.close()


def test_browse_query_filters_filename_and_ref():
    db = _app_mod.get_db()
    try:
        _seed(db)
        assert _ai.browse(db, q="pouch")["total"] == 1          # po nazwie pliku
        assert _ai.browse(db, q="BT-200")["total"] == 1         # po ref_code
        assert _ai.browse(db, q="bt100")["total"] == 2          # dwie rewizje
    finally:
        db.close()


def test_browse_packaging_filter():
    db = _app_mod.get_db()
    try:
        _seed(db)
        assert _ai.browse(db, packaging="karton")["total"] == 2
        assert _ai.browse(db, packaging="sztuka")["total"] == 1
        assert set(_ai.packaging_types(db)) == {"karton", "op", "sztuka"}
    finally:
        db.close()


def test_browse_like_escaping():
    """'_' i '%' w zapytaniu muszą matchować literalnie (nie jako wildcard LIKE)."""
    db = _app_mod.get_db()
    try:
        _ai.ensure_table(db)
        db.execute("DELETE FROM artwork_index")
        for i, (fn, rp) in enumerate([("dlt_a_b.pdf", "z/1.pdf"), ("dlt_aXb.pdf", "z/2.pdf")]):
            db.execute("INSERT INTO artwork_index(ref_norm,ref_code,filename,rel_path) "
                       "VALUES('','',?,?)", (fn, rp))
        db.commit()
        # '_' jako literał: łapie tylko a_b, nie aXb
        res = _ai.browse(db, q="a_b")
        names = {r["filename"] for r in res["rows"]}
        assert names == {"dlt_a_b.pdf"}, names
    finally:
        db.close()


def test_browse_current_only_drops_older_revisions():
    db = _app_mod.get_db()
    try:
        _seed(db)
        res = _ai.browse(db, current_only=True)
        # BT100 ma 2 rewizje → zostaje tylko rank 2 (B); BT200 i XY777 bez konkurencji.
        assert res["total"] == 3
        bt100 = [r for r in res["rows"] if r["ref_code"] == "BT-100"]
        assert len(bt100) == 1 and bt100[0]["revision"] == "B"
    finally:
        db.close()
