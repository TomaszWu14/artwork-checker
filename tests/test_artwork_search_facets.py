"""FACET-01: facety wyszukiwania biblioteki — wariant renderu, ma-MARM-link.

Direct-DB przez migrate_db (tabele artwork_render_registry / artwork_marm_link
żyją w migrate). Wzór jak tests/test_coverage_view.py."""
import migrate_db
import app as _app_mod
import artwork_index as _ai
from constants import RenderVariant

migrate_db.run()


def _clean(db):
    _ai.ensure_table(db)
    db.execute("DELETE FROM artwork_index")
    db.execute("DELETE FROM artwork_render_registry")
    db.execute("DELETE FROM artwork_marm_link")
    db.commit()


def _seed_index(db, ref_norm, ref_code, filename):
    db.execute("INSERT INTO artwork_index (ref_norm, ref_code, filename, rel_path) "
               "VALUES (?,?,?,?)", (ref_norm, ref_code, filename, f"z/{filename}"))
    db.commit()


def test_variant_facet_filters_by_render_variant():
    db = _app_mod.get_db()
    try:
        _clean(db)
        _seed_index(db, "FA1", "FA1", "fa1.pdf")
        _seed_index(db, "FA2", "FA2", "fa2.pdf")
        # FA1 ma render marm, FA2 nie ma nic
        db.execute("INSERT INTO artwork_render_registry (ref_norm, variant, glb_path) "
                   "VALUES ('FA1',?,'/a.glb')", (RenderVariant.MARM.value,))
        db.commit()

        res = _ai.browse(db, variant=RenderVariant.MARM.value)
        refs = {r["ref_code"] for r in res["rows"]}
        assert refs == {"FA1"}
        # bez filtra — oba
        assert _ai.browse(db)["total"] == 2
        # wariant, którego nikt nie ma
        assert _ai.browse(db, variant=RenderVariant.PHOTO.value)["total"] == 0
        # nieznany wariant ignorowany (nie filtruje)
        assert _ai.browse(db, variant="bogus")["total"] == 2
    finally:
        db.close()


def test_marm_linked_facet():
    db = _app_mod.get_db()
    try:
        _clean(db)
        _seed_index(db, "FB1", "FB1", "fb1.pdf")
        _seed_index(db, "FB2", "FB2", "fb2.pdf")
        db.execute("INSERT INTO artwork_marm_link (art_ref_norm, marm_ref, marm_unit) "
                   "VALUES ('FB1','300000','KAR')")
        db.commit()

        res = _ai.browse(db, marm_linked=True)
        assert {r["ref_code"] for r in res["rows"]} == {"FB1"}
        assert _ai.browse(db, marm_linked=False)["total"] == 2
    finally:
        db.close()
