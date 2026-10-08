"""Faza 1 (rejestr renderów): coverage() — widok „co mamy", LEFT JOIN po
fid|ref_norm (D-04), filtry REF/lvl1, paginacja server-side (COV-01..03).
Direct-DB, bez Flask test clienta — wzór tests/test_artwork_browse.py."""
import migrate_db
import app as _app_mod
import artwork_render_registry as _arr
from constants import RenderVariant
from uom import normalize_ref

migrate_db.run()


def _clean(db):
    db.execute("DELETE FROM library_files")
    db.execute("DELETE FROM artwork_render_registry")
    db.commit()


def _seed_library(db, rows):
    """rows: iterable of (rel_path, filename, ref, lvl1)."""
    for r in rows:
        db.execute(
            "INSERT INTO library_files(rel_path, filename, ref, lvl1) VALUES (?,?,?,?)", r)
    db.commit()


def test_coverage_left_join():
    db = _app_mod.get_db()
    try:
        _clean(db)
        _seed_library(db, [
            ("z/gs17.pdf", "gs17.pdf", "GS17", "Marka"),
            ("z/gs18.pdf", "gs18.pdf", "GS18", "Marka"),
        ])
        fid_gs17 = db.execute("SELECT id FROM library_files WHERE ref='GS17'").fetchone()["id"]
        _arr.register_render(
            db, fid=fid_gs17, variant=RenderVariant.DIELINE.value,
            # Bandit B108: stała ścieżka to tylko dane testowe zapisywane w rejestrze — nic nie trafia na dysk.
            source_hash="h1", glb_path="/tmp/gs17.glb")  # nosec B108

        res = _arr.coverage(db)
        by_ref = {r["ref"]: r for r in res["items"]}
        assert by_ref["GS17"]["has_dieline"]
        assert not by_ref["GS17"]["has_marm"]
        assert not by_ref["GS17"]["has_photo"]
        assert not by_ref["GS18"]["has_dieline"]
        assert not by_ref["GS18"]["has_marm"]
        assert not by_ref["GS18"]["has_photo"]
    finally:
        db.close()


def test_coverage_surfaces_push_status():
    """PUSH-02: coverage() zwraca status pusha najświeższego renderu per wariant,
    z registry_id do retry. Domyślnie 'pending'; po zmianie statusu — odzwierciedla."""
    db = _app_mod.get_db()
    try:
        _clean(db)
        _seed_library(db, [("z/ps1.pdf", "ps1.pdf", "PS1", "Marka")])
        fid = db.execute("SELECT id FROM library_files WHERE ref='PS1'").fetchone()["id"]
        reg = _arr.register_render(
            db, fid=fid, variant=RenderVariant.DIELINE.value,
            # Bandit B108: stała ścieżka to tylko dane testowe zapisywane w rejestrze — nic nie trafia na dysk.
            source_hash="h1", glb_path="/tmp/ps1.glb")  # nosec B108
        item = {r["ref"]: r for r in _arr.coverage(db)["items"]}["PS1"]
        assert item["push"][RenderVariant.DIELINE.value]["status"] == "pending"
        assert item["push"][RenderVariant.DIELINE.value]["registry_id"] == reg["id"]

        db.execute("UPDATE artwork_render_registry SET push_status='sent' WHERE id=?", (reg["id"],))
        db.commit()
        item2 = {r["ref"]: r for r in _arr.coverage(db)["items"]}["PS1"]
        assert item2["push"][RenderVariant.DIELINE.value]["status"] == "sent"
    finally:
        db.close()


def test_coverage_flags_stale_dieline():
    """STALE-01: dieline render nieaktualny, gdy zapisany source_hash ≠ bieżący
    library_files.checksum (plik zmieniony na Z:\\ po renderze)."""
    db = _app_mod.get_db()
    try:
        _clean(db)
        db.execute("INSERT INTO library_files(rel_path, filename, ref, lvl1, checksum) "
                   "VALUES ('z/st1.pdf','st1.pdf','ST1','Marka','NEWHASH')")
        fid = db.execute("SELECT id FROM library_files WHERE ref='ST1'").fetchone()["id"]
        # render zapisany ze STARYM hashem — plik się od tego czasu zmienił
        _arr.register_render(db, fid=fid, variant=RenderVariant.DIELINE.value,
                             # Bandit B108: stała ścieżka to tylko dane testowe zapisywane w rejestrze — nic nie trafia na dysk.
                             source_hash="OLDHASH", glb_path="/tmp/st1.glb")  # nosec B108
        item = {r["ref"]: r for r in _arr.coverage(db)["items"]}["ST1"]
        assert item["dieline_stale"] is True

        # ten sam hash → nieaktualność znika
        _arr.register_render(db, fid=fid, variant=RenderVariant.DIELINE.value,
                             # Bandit B108: stała ścieżka to tylko dane testowe zapisywane w rejestrze — nic nie trafia na dysk.
                             source_hash="NEWHASH", glb_path="/tmp/st1b.glb")  # nosec B108
        item2 = {r["ref"]: r for r in _arr.coverage(db)["items"]}["ST1"]
        assert item2["dieline_stale"] is False
    finally:
        db.close()


def test_coverage_variant_badges():
    db = _app_mod.get_db()
    try:
        _clean(db)
        _seed_library(db, [("z/gs19.pdf", "gs19.pdf", "GS19", "Marka")])
        fid = db.execute("SELECT id FROM library_files WHERE ref='GS19'").fetchone()["id"]
        _arr.register_render(
            db, fid=fid, variant=RenderVariant.DIELINE.value,
            # Bandit B108: stała ścieżka to tylko dane testowe zapisywane w rejestrze — nic nie trafia na dysk.
            source_hash="h1", glb_path="/tmp/a.glb")  # nosec B108
        _arr.register_render(
            db, fid=fid, variant=RenderVariant.PHOTO.value,
            # Bandit B108: stała ścieżka to tylko dane testowe zapisywane w rejestrze — nic nie trafia na dysk.
            source_hash="h2", glb_path="/tmp/b.glb")  # nosec B108

        res = _arr.coverage(db)
        row = next(r for r in res["items"] if r["ref"] == "GS19")
        assert row["has_dieline"]
        assert row["has_photo"]
        assert not row["has_marm"]
    finally:
        db.close()


def test_coverage_filters():
    db = _app_mod.get_db()
    try:
        _clean(db)
        _seed_library(db, [
            ("z/a/qy17050820-ss.pdf", "QY17050820-SS.pdf", "QY17050820-SS", "MarkaA"),
            ("z/b/xy100.pdf", "xy100.pdf", "XY100", "MarkaB"),
        ])

        res_ref = _arr.coverage(db, ref="QY17")
        assert res_ref["total"] == 1
        assert res_ref["items"][0]["ref"] == "QY17050820-SS"

        res_lvl = _arr.coverage(db, lvl1="MarkaB")
        assert res_lvl["total"] == 1
        assert res_lvl["items"][0]["ref"] == "XY100"

        res_all = _arr.coverage(db)
        assert res_all["total"] == 2
    finally:
        db.close()


def test_coverage_refnorm_fallback():
    db = _app_mod.get_db()
    try:
        _clean(db)
        _seed_library(db, [("z/gs.pdf", "gs.pdf", "QY1705-SS", "Marka")])
        # Wiersz rejestru BEZ fid — MARM/accept-path łączy się po ref_norm (D-04).
        _arr.register_render(
            db, ref_norm=normalize_ref("QY1705-SS"), variant=RenderVariant.MARM.value,
            # Bandit B108: stała ścieżka to tylko dane testowe zapisywane w rejestrze — nic nie trafia na dysk.
            source_hash="h", glb_path="/tmp/marm.glb")  # nosec B108

        res = _arr.coverage(db)
        row = next(r for r in res["items"] if r["ref"] == "QY1705-SS")
        assert row["has_marm"]
    finally:
        db.close()


def test_coverage_pagination():
    db = _app_mod.get_db()
    try:
        _clean(db)
        _seed_library(db, [
            ("z/1.pdf", "f1.pdf", "R1", "M"),
            ("z/2.pdf", "f2.pdf", "R2", "M"),
            ("z/3.pdf", "f3.pdf", "R3", "M"),
        ])

        res = _arr.coverage(db, per_page=2, page=1)
        assert res["total"] == 3
        assert res["total_pages"] == 2
        assert len(res["items"]) == 2

        res2 = _arr.coverage(db, per_page=2, page=2)
        assert len(res2["items"]) == 1
    finally:
        db.close()
