"""Phase 2 (SRCH-02): browse() batched has_3d registry join (D-03).

"ma 3D" = any variant present (dieline/marm/photo), identical semantics to
Phase 1's artwork_render_registry.coverage(). Filled by exactly ONE batched
`ref_norm IN (...)` query per page — never a per-row loop."""
import migrate_db
import app as _app_mod
import artwork_index as _ai
import artwork_render_registry as _arr
from constants import RenderVariant

migrate_db.run()


def _clean(db):
    _ai.ensure_table(db)
    db.execute("DELETE FROM artwork_index")
    db.execute("DELETE FROM artwork_render_registry")
    db.commit()


def _seed_row(db, ref_norm, ref_code, filename, rel_path):
    db.execute(
        "INSERT INTO artwork_index(ref_norm,ref_code,filename,rel_path) VALUES(?,?,?,?)",
        (ref_norm, ref_code, filename, rel_path))
    db.commit()


def test_has_3d_true_when_any_variant():
    db = _app_mod.get_db()
    try:
        _clean(db)
        _seed_row(db, "R1", "R-1", "r1.pdf", "z/r1.pdf")
        _arr.register_render(db, ref_norm="R1", variant=RenderVariant.DIELINE.value,
                              # Bandit B108: stała ścieżka to tylko dane testowe zapisywane w rejestrze — nic nie trafia na dysk.
                              source_hash="h1", glb_path="/tmp/r1.glb")  # nosec B108
        res = _ai.browse(db)
        row = next(r for r in res["rows"] if r["ref_norm"] == "R1")
        assert row["has_3d"] is True
        assert row["has_dieline"] is True
        assert row["has_marm"] is False
        assert row["has_photo"] is False
    finally:
        db.close()


def test_has_3d_false_when_no_registry_row():
    db = _app_mod.get_db()
    try:
        _clean(db)
        _seed_row(db, "R2", "R-2", "r2.pdf", "z/r2.pdf")
        res = _ai.browse(db)
        row = next(r for r in res["rows"] if r["ref_norm"] == "R2")
        assert row["has_3d"] is False
        assert row["has_dieline"] is False
        assert row["has_marm"] is False
        assert row["has_photo"] is False
    finally:
        db.close()


def test_any_variant_union():
    db = _app_mod.get_db()
    try:
        _clean(db)
        _seed_row(db, "R3", "R-3", "r3.pdf", "z/r3.pdf")
        _arr.register_render(db, ref_norm="R3", variant=RenderVariant.MARM.value,
                              # Bandit B108: stała ścieżka to tylko dane testowe zapisywane w rejestrze — nic nie trafia na dysk.
                              source_hash="h1", glb_path="/tmp/r3a.glb")  # nosec B108
        _arr.register_render(db, ref_norm="R3", variant=RenderVariant.PHOTO.value,
                              # Bandit B108: stała ścieżka to tylko dane testowe zapisywane w rejestrze — nic nie trafia na dysk.
                              source_hash="h2", glb_path="/tmp/r3b.glb")  # nosec B108
        res = _ai.browse(db)
        row = next(r for r in res["rows"] if r["ref_norm"] == "R3")
        assert row["has_marm"] is True
        assert row["has_photo"] is True
        assert row["has_3d"] is True
    finally:
        db.close()


def test_single_batched_query_per_page():
    db = _app_mod.get_db()
    try:
        _clean(db)
        for i in range(5):
            rn = f"BQ{i}"
            _seed_row(db, rn, rn, f"bq{i}.pdf", f"z/bq{i}.pdf")
            _arr.register_render(db, ref_norm=rn, variant=RenderVariant.DIELINE.value,
                                  # Bandit B108: stała ścieżka to tylko dane testowe zapisywane w rejestrze — nic nie trafia na dysk.
                                  source_hash=f"h{i}", glb_path=f"/tmp/bq{i}.glb")  # nosec B108
        calls = []
        # sqlite3.Connection.execute is read-only (can't be monkeypatched) —
        # set_trace_callback is the native hook for observing every executed
        # SQL statement without touching the object's attributes.
        db.set_trace_callback(
            lambda sql: calls.append(sql) if "artwork_render_registry" in sql else None)
        try:
            res = _ai.browse(db, per_page=10, page=1)
        finally:
            db.set_trace_callback(None)
        assert len(res["rows"]) == 5
        assert len(calls) == 1, f"expected exactly 1 registry query, got {len(calls)}: {calls}"
    finally:
        db.close()


def test_empty_page_no_registry_query():
    db = _app_mod.get_db()
    try:
        _clean(db)
        db.execute(
            "INSERT INTO artwork_index(ref_norm,ref_code,filename,rel_path) "
            "VALUES('','','no_ref.pdf','z/no_ref.pdf')")
        db.commit()
        calls = []
        db.set_trace_callback(
            lambda sql: calls.append(sql) if "artwork_render_registry" in sql else None)
        try:
            _ai.browse(db)
        finally:
            db.set_trace_callback(None)
        assert calls == []
    finally:
        db.close()
