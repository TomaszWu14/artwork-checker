"""Phase 2 (SRCH-01): browse() exact-REF pin + rapidfuzz secondary ordering.

D-02: exact `ref_norm` equality is a hard pin to row 1 of page 1, ahead of any
fuzzy/alias match — even when the WHERE-matched candidate set is large enough
to span multiple pages (RESEARCH Pitfall 1: a naive re-sort of the already-
paginated SQL slice would strand the exact match on a later page)."""
import app as _app_mod
import artwork_index as _ai


def _clean(db):
    _ai.ensure_table(db)
    db.execute("DELETE FROM artwork_index")


def _seed_bulk(db, marker, exact_ref, n=60):
    """`n` rows with alphabetically-early ref_codes (AAA000..AAAnnn), all with
    `marker` embedded in the filename, plus one exact-match row (ref_code=
    exact_ref) whose filename also contains `marker`. A q=<exact_ref> search's
    WHERE clause (filename/ref_code/ref_norm LIKE) therefore matches all n+1
    rows, and — without the SRCH-01 pin — the exact row sorts dead last
    (alphabetically after every 'AAA...' row), landing on page 2+ instead of
    page 1 row 1."""
    for i in range(n):
        ref = f"AAA{i:03d}"
        rn = _ai.normalize_ref(ref)
        fn = f"{ref.lower()}_{marker}_scan.pdf"
        db.execute(
            "INSERT INTO artwork_index(ref_norm,ref_code,filename,rel_path) VALUES(?,?,?,?)",
            (rn, ref, fn, f"z/{i}.pdf"))
    ern = _ai.normalize_ref(exact_ref)
    db.execute(
        "INSERT INTO artwork_index(ref_norm,ref_code,filename,rel_path) VALUES(?,?,?,?)",
        (ern, exact_ref, f"{marker}_exact.pdf", "z/exact.pdf"))
    db.commit()


def test_exact_ref_norm_pinned_first():
    db = _app_mod.get_db()
    try:
        _clean(db)
        _seed_bulk(db, "zzz999", "ZZZ999", n=60)
        res = _ai.browse(db, q="ZZZ999")
        assert res["total"] == 61
        assert res["rows"][0]["ref_code"] == "ZZZ999"
    finally:
        db.close()


def test_exact_pin_survives_pagination():
    db = _app_mod.get_db()
    try:
        _clean(db)
        _seed_bulk(db, "zzz999", "ZZZ999", n=60)
        res = _ai.browse(db, q="ZZZ999", per_page=50, page=1)
        assert res["rows"][0]["ref_code"] == "ZZZ999"
        res2 = _ai.browse(db, q="ZZZ999", per_page=50, page=2)
        assert all(r["ref_code"] != "ZZZ999" for r in res2["rows"]), \
            "exact match must not ALSO surface on page 2 (must not be duplicated)"
    finally:
        db.close()


def test_fuzzy_secondary_orders_rest():
    db = _app_mod.get_db()
    try:
        _clean(db)
        # No exact match for qn="ZZ10". Both rows' filenames contain "zz10" so
        # both satisfy the WHERE clause, but "ZZ100"'s ref_norm is a much
        # closer rapidfuzz match to "ZZ10" than "AA999"'s. Alphabetically
        # ref_code "AA-999" sorts BEFORE "ZZ-100" (A<Z) — so this only passes
        # once fuzzy relevance (not alphabetical order) drives the ranking.
        db.execute(
            "INSERT INTO artwork_index(ref_norm,ref_code,filename,rel_path) VALUES(?,?,?,?)",
            ("AA999", "AA-999", "aa999_zz10_batch.pdf", "z/far.pdf"))
        db.execute(
            "INSERT INTO artwork_index(ref_norm,ref_code,filename,rel_path) VALUES(?,?,?,?)",
            ("ZZ100", "ZZ-100", "zz100_carton.pdf", "z/close.pdf"))
        db.commit()
        res = _ai.browse(db, q="zz10")
        assert res["total"] == 2
        assert res["rows"][0]["ref_code"] == "ZZ-100"
    finally:
        db.close()


def test_alias_baked_refnorm_in_pin_tier():
    """Aliases are backfilled into ref_norm at ingest (sync_entries/add_alias)
    — browse() must pin on that already-baked ref_norm, with no separate
    artwork_alias read-time lookup."""
    db = _app_mod.get_db()
    try:
        _clean(db)
        db.execute(
            "INSERT INTO artwork_index(ref_norm,ref_code,filename,rel_path) VALUES(?,?,?,?)",
            ("ALIASTARGET", "ALIAS-TARGET", "some_weird_filename_aliastarget.pdf", "z/alias.pdf"))
        db.execute(
            "INSERT INTO artwork_index(ref_norm,ref_code,filename,rel_path) VALUES(?,?,?,?)",
            ("ZZALIASTARGETZZ", "OTHER", "aliastarget_other.pdf", "z/other.pdf"))
        db.commit()
        res = _ai.browse(db, q="ALIASTARGET")
        assert res["total"] == 2
        assert res["rows"][0]["ref_norm"] == "ALIASTARGET"
    finally:
        db.close()


def test_empty_q_unchanged():
    db = _app_mod.get_db()
    try:
        _clean(db)
        db.execute(
            "INSERT INTO artwork_index(ref_norm,ref_code,filename,rel_path) VALUES(?,?,?,?)",
            ("ZZZ", "Z-ITEM", "z_item.pdf", "z/z.pdf"))
        db.execute(
            "INSERT INTO artwork_index(ref_norm,ref_code,filename,rel_path) VALUES(?,?,?,?)",
            ("AAA", "A-ITEM", "a_item.pdf", "z/a.pdf"))
        db.commit()
        res = _ai.browse(db, q="")
        assert res["total"] == 2
        assert [r["ref_code"] for r in res["rows"]] == ["A-ITEM", "Z-ITEM"]
    finally:
        db.close()
