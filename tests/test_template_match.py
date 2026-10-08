"""Testy dieline_templates (Faza 2b): sygnatura, regiony, dopasowanie z bazy."""
import sqlite3

import pytest

import dieline_templates as dt


def _pdf_with_vlines(path, w_mm=400, h_mm=300, xs_mm=(100, 200, 300)):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    p = doc.new_page(width=w_mm / 25.4 * 72, height=h_mm / 25.4 * 72)
    for x in xs_mm:                                    # długie pionowe linie cięcia
        xp = x / 25.4 * 72
        p.draw_line((xp, 0), (xp, p.rect.height), color=(0, 0, 0))
    doc.save(path)
    doc.close()


def test_signature_captures_page_and_vcuts(tmp_path):
    pytest.importorskip("fitz")
    p = str(tmp_path / "d.pdf")
    _pdf_with_vlines(p, 400, 300, (100, 200, 300))
    sig = dt.signature(p)
    assert sig["page_w_mm"] == 400 and sig["page_h_mm"] == 300
    # 3 cięcia przy 0.25 / 0.50 / 0.75 szerokości
    for want in (0.25, 0.50, 0.75):
        assert any(abs(v - want) < 0.03 for v in sig["vcuts"]), sig["vcuts"]
    assert sig["key"].startswith("400x300|")


def test_same_layout_same_key(tmp_path):
    pytest.importorskip("fitz")
    a = str(tmp_path / "a.pdf"); b = str(tmp_path / "b.pdf")
    _pdf_with_vlines(a); _pdf_with_vlines(b)
    assert dt.signature(a)["key"] == dt.signature(b)["key"]   # ta sama rodzina


def test_make_and_locate(tmp_path):
    pytest.importorskip("fitz")
    p = str(tmp_path / "d.pdf")
    _pdf_with_vlines(p, 400, 300)
    sig = dt.signature(p)
    regions = {"front": (100, 50, 200, 250), "back": (200, 50, 300, 250)}
    tpl = dt.make_template_from_regions("test", sig, regions)
    # normalizacja 0..1
    assert tpl.panels["front"] == (0.25, 50 / 300, 0.5, 250 / 300)
    # rzut z powrotem na mm dla tej samej strony = oryginalne regiony
    rc = dt.locate_panels_by_template(tpl, 400, 300)
    assert abs(rc["front"][0] - 100) < 1e-6 and abs(rc["front"][3] - 250) < 1e-6


def test_save_and_match(tmp_path):
    pytest.importorskip("fitz")
    db = sqlite3.connect(":memory:")
    db.executescript("""CREATE TABLE dieline_templates(id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, sig_key TEXT UNIQUE, page_w_mm REAL, page_h_mm REAL,
        panels_json TEXT, orient_json TEXT, created_by INTEGER, created_at TEXT)""")
    p = str(tmp_path / "d.pdf"); _pdf_with_vlines(p, 400, 300)
    sig = dt.signature(p)
    tpl = dt.make_template_from_regions("rodzina-X", sig, {"front": (100, 50, 200, 250)})
    dt.save_template(db, tpl)
    got = dt.match_template(db, sig["key"])
    assert got and got.name == "rodzina-X"
    assert "front" in got.panels
    assert dt.match_template(db, "999x999|") is None       # brak = None


def test_build_bundle_rects_override(tmp_path):
    pytest.importorskip("trimesh"); pytest.importorskip("fitz")
    import artwork_palviz as a3d
    fitz = pytest.importorskip("fitz")
    # prosty arkusz z treścią + wymiary w tekście
    W, H, D = 210, 120, 55
    pw, ph = 400, 300
    doc = fitz.open(); pg = doc.new_page(width=pw / 25.4 * 72, height=ph / 25.4 * 72)
    pg.insert_text((30, 30), f"{W} x {H} x {D} mm", fontsize=9)
    pg.draw_rect(pg.rect, color=None, fill=(0.8, 0.8, 0.75))
    p = str(tmp_path / "d.pdf"); doc.save(p); doc.close()
    # override regionów (mm) — 6 ścian w obrębie strony
    rects = {f: (10, 10, 60, 60) for f in a3d.FACES}
    res = a3d.build_bundle(p, str(tmp_path / "b.glb"), str(tmp_path / "faces"),
                           level="box", sku="X", rects=rects,
                           orient={"top": 90, "front": 270})
    assert not res["needs_manual_dims"]
    assert any("szablon" in w for w in res["warnings"])
    assert res["panels_found"] == 6


def test_rot_for_override():
    import artwork_palviz as a3d
    # override z szablonu wygrywa z globalną ORIENT
    assert a3d._rot_for("top", {"top": 90}) == 90
    assert a3d._rot_for("top", None) == a3d.ORIENT["top"]      # fallback globalny
    assert a3d._rot_for("front", {}) == a3d.ORIENT.get("front", 0)
    assert a3d._rot_for("left", {"left": 450}) == 90           # normalizacja %360
