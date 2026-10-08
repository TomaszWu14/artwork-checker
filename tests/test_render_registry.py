"""Faza 1 (rejestr renderów): register_render() — dedup po source_hash / append-only
historia / audyt (REG-01..05). Direct-DB, bez Flask test clienta — wzór
tests/test_artwork_browse.py. Tabela żyje tylko w migrate_db.py (nie w app.py's
init_db()), więc migrate_db.run() musi zostać wywołane raz, żeby istniała w
izolowanej bazie testowej z conftest.py."""
import pytest

import migrate_db
import app as _app_mod
import artwork_render_registry as _arr
from constants import RenderVariant

migrate_db.run()


def _clean(db):
    db.execute("DELETE FROM artwork_render_registry")
    db.execute("DELETE FROM audit_log WHERE event='render_registered'")
    db.commit()


def test_table_created():
    db = _app_mod.get_db()
    try:
        row = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='artwork_render_registry'"
        ).fetchone()
        assert row is not None
        # Ponowna migracja nie rzuca (idempotencja REG-01).
        migrate_db.run()
    finally:
        db.close()


def test_schema_no_blob():
    db = _app_mod.get_db()
    try:
        cols = db.execute("PRAGMA table_info(artwork_render_registry)").fetchall()
        names = {c[1] for c in cols}
        assert names == {
            "id", "fid", "ref_norm", "variant", "source_hash", "glb_path",
            "created_by", "created_at", "last_confirmed_at",
            # Faza 4 (PUSH-02): status wypchnięcia wiersza do PalViz
            "push_status", "push_at", "push_error", "push_attempts",
        }
        assert not any("BLOB" in (c[2] or "").upper() for c in cols)
    finally:
        db.close()


def test_register_render_dedup_and_append():
    db = _app_mod.get_db()
    try:
        _clean(db)
        r1 = _arr.register_render(
            db, fid=101, variant=RenderVariant.DIELINE.value,
            # Bandit B108: stała ścieżka to tylko dane testowe zapisywane w rejestrze — nic nie trafia na dysk.
            source_hash="abc", glb_path="/tmp/1.glb")  # nosec B108
        assert r1["action"] == "inserted"

        # Force an old last_confirmed_at so the bump is observable without sleeping
        # past SQLite's 1-second datetime('now') resolution.
        db.execute(
            "UPDATE artwork_render_registry SET last_confirmed_at='2000-01-01 00:00:00' WHERE id=?",
            (r1["id"],))
        db.commit()

        r2 = _arr.register_render(
            db, fid=101, variant=RenderVariant.DIELINE.value,
            # Bandit B108: stała ścieżka to tylko dane testowe zapisywane w rejestrze — nic nie trafia na dysk.
            source_hash="abc", glb_path="/tmp/1.glb")  # nosec B108
        assert r2["action"] == "bumped"
        assert r2["id"] == r1["id"]
        row2 = db.execute(
            "SELECT last_confirmed_at FROM artwork_render_registry WHERE id=?",
            (r1["id"],)).fetchone()
        assert row2["last_confirmed_at"] != "2000-01-01 00:00:00"

        count = db.execute(
            "SELECT COUNT(*) FROM artwork_render_registry WHERE fid=101").fetchone()[0]
        assert count == 1

        r3 = _arr.register_render(
            db, fid=101, variant=RenderVariant.DIELINE.value,
            # Bandit B108: stała ścieżka to tylko dane testowe zapisywane w rejestrze — nic nie trafia na dysk.
            source_hash="def", glb_path="/tmp/2.glb")  # nosec B108
        assert r3["action"] == "inserted"
        assert r3["id"] != r1["id"]
        count2 = db.execute(
            "SELECT COUNT(*) FROM artwork_render_registry WHERE fid=101").fetchone()[0]
        assert count2 == 2
    finally:
        db.close()


def test_register_render_empty_hash_always_inserts():
    db = _app_mod.get_db()
    try:
        _clean(db)
        r1 = _arr.register_render(
            db, fid=202, variant=RenderVariant.PHOTO.value,
            # Bandit B108: stała ścieżka to tylko dane testowe zapisywane w rejestrze — nic nie trafia na dysk.
            source_hash="", glb_path="/tmp/a.glb")  # nosec B108
        r2 = _arr.register_render(
            db, fid=202, variant=RenderVariant.PHOTO.value,
            # Bandit B108: stała ścieżka to tylko dane testowe zapisywane w rejestrze — nic nie trafia na dysk.
            source_hash="", glb_path="/tmp/b.glb")  # nosec B108
        assert r1["action"] == "inserted"
        assert r2["action"] == "inserted"
        assert r1["id"] != r2["id"]
        count = db.execute(
            "SELECT COUNT(*) FROM artwork_render_registry WHERE fid=202").fetchone()[0]
        assert count == 2
    finally:
        db.close()


def test_audit_on_register():
    db = _app_mod.get_db()
    try:
        _clean(db)
        _arr.register_render(
            db, fid=303, variant=RenderVariant.DIELINE.value,
            # Bandit B108: stała ścieżka to tylko dane testowe zapisywane w rejestrze — nic nie trafia na dysk.
            source_hash="xyz", glb_path="/tmp/c.glb")  # nosec B108
        row = db.execute(
            "SELECT event FROM audit_log WHERE event='render_registered' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        assert row is not None
    finally:
        db.close()


def test_variant_validation():
    db = _app_mod.get_db()
    try:
        with pytest.raises(ValueError):
            _arr.register_render(
                # Bandit B108: stała ścieżka to tylko dane testowe zapisywane w rejestrze — nic nie trafia na dysk.
                db, fid=404, variant="bogus", source_hash="x", glb_path="/tmp/d.glb")  # nosec B108
    finally:
        db.close()


def test_marm_source_hash_stable():
    dims_mm = (100.0, 200.0, 50.0)
    h1 = _arr.marm_source_hash("GS17", "PC", dims_mm)
    h2 = _arr.marm_source_hash("GS17", "PC", dims_mm)
    assert h1 == h2
    assert h1 != _arr.marm_source_hash("GS18", "PC", dims_mm)
    assert h1 != _arr.marm_source_hash("GS17", "KAR", dims_mm)
    assert h1 != _arr.marm_source_hash("GS17", "PC", (101.0, 200.0, 50.0))


def test_marm_source_hash_dim_unit_normalized():
    import material_uom as _mu
    row_cm = {"width": 10, "height": 20, "length": 5, "dim_unit": "CM"}
    row_mm = {"width": 100, "height": 200, "length": 50, "dim_unit": "MM"}
    dims_cm = _mu._dims_mm(row_cm)
    dims_mm = _mu._dims_mm(row_mm)
    assert dims_cm == dims_mm   # sama geometria, różne jednostki wejściowe
    h1 = _arr.marm_source_hash("GS17", "PC", dims_cm)
    h2 = _arr.marm_source_hash("GS17", "PC", dims_mm)
    assert h1 == h2


def test_photo_source_hash():
    b1 = b"\x89PNGfakebytes1"
    b2 = b"\x89PNGfakebytes2"
    h1 = _arr.photo_source_hash([b1, b2])
    h2 = _arr.photo_source_hash([b1, b2])
    assert h1 == h2
    assert h1 != _arr.photo_source_hash([b2, b1])
    assert _arr.photo_source_hash([]) == ""
