"""Testy pure-logic dla artwork_3d (bez ML/serwera). trimesh przez importorskip."""
import os
import pytest

import json

import artwork_palviz as a3d
from artwork_palviz import (DielineInfo, locate_panels, _DIM_RE, ORIENT, FACES,
                        FACE_DIMS, _fit_face, PALVIZ_LEVELS)


# ─── parsowanie wymiarów ──────────────────────────────────────────────────────
@pytest.mark.parametrize("txt,exp", [
    ("210 x 120 x 55 mm", (210, 120, 55)),
    ("290 x 250 x 222", (290, 250, 222)),
    ("Rozmiar: 210×120×55mm", (210, 120, 55)),
    ("100x50x25", (100, 50, 25)),
    ("  8 x 8 x 8 ", (8, 8, 8)),
])
def test_dim_regex(txt, exp):
    m = _DIM_RE.search(txt)
    assert m and tuple(int(g) for g in m.groups()) == exp


def test_dim_regex_none():
    assert _DIM_RE.search("brak wymiarów tutaj") is None


# ─── locate_panels — matematyka na syntetycznych danych ───────────────────────
def test_locate_panels_1to1():
    W, H, D = 210, 120, 55
    info = DielineInfo(w_mm=W, h_mm=H, d_mm=D,
                       page_w_mm=2 * D + 2 * W, page_h_mm=H + 2 * D)
    r = locate_panels(info)
    assert set(r) == set(FACES)
    # rozmiary paneli = wymiary ścian
    def wh(rc): return (round(rc[2] - rc[0]), round(rc[3] - rc[1]))
    assert wh(r["front"]) == (W, H)
    assert wh(r["back"]) == (W, H)
    assert wh(r["left"]) == (D, H)
    assert wh(r["right"]) == (D, H)
    assert wh(r["top"]) == (W, D)
    assert wh(r["bottom"]) == (W, D)
    # brak warningu przy 1:1
    assert not info.warnings
    # top nad frontem, bottom pod frontem
    assert r["top"][3] <= r["front"][1] + 0.01
    assert r["bottom"][1] >= r["front"][3] - 0.01
    # panele pasa na tej samej wysokości
    assert abs(r["front"][1] - r["left"][1]) < 0.01


def test_locate_panels_scale_warns():
    W, H, D = 290, 250, 222
    # strona 2× szersza niż pas → skala ≠ 1:1
    info = DielineInfo(w_mm=W, h_mm=H, d_mm=D,
                       page_w_mm=2 * (2 * D + 2 * W), page_h_mm=1000)
    locate_panels(info)
    assert any("heurystycznie" in w for w in info.warnings)


def test_locate_panels_no_dims():
    assert locate_panels(DielineInfo()) == {}


# ─── tabela rotacji UV/paneli ─────────────────────────────────────────────────
def test_orient_table():
    assert set(ORIENT) == set(FACES)
    assert ORIENT["top"] == 180        # klapa góra obrócona względem frontu
    assert ORIENT["front"] == 0


# ─── build_glb — realny plik GLB ──────────────────────────────────────────────
def test_build_glb_roundtrip(tmp_path):
    trimesh = pytest.importorskip("trimesh")
    from PIL import Image
    W, H, D = 210, 120, 55
    info = DielineInfo(w_mm=W, h_mm=H, d_mm=D)
    panels = {f: Image.new("RGB", (64, 64), (i * 30 % 255, 100, 150))
              for i, f in enumerate(FACES)}
    out = str(tmp_path / "box.glb")
    a3d.build_glb(panels, info, out)

    assert os.path.exists(out)
    with open(out, "rb") as fh:
        assert fh.read(4) == b"glTF"          # magic

    scene = trimesh.load(out)
    ext = scene.bounding_box.extents          # metry
    got = sorted(round(x, 4) for x in ext)
    want = sorted([W / 1000, H / 1000, D / 1000])
    for a, b in zip(got, want):
        assert abs(a - b) < 1e-3


# ─── PalViz export: proporcje _fit_face (±2%) ─────────────────────────────────
@pytest.mark.parametrize("aw,ah", [(210, 120), (55, 120), (210, 55), (300, 300)])
def test_fit_face_aspect(aw, ah):
    pytest.importorskip("PIL")
    from PIL import Image
    out = _fit_face(Image.new("RGB", (40, 30)), aw, ah, min_px=1024)
    assert max(out.size) >= 1024
    got, want = out.width / out.height, aw / ah
    assert abs(got - want) / want <= 0.02          # ±2% jak w self-check PalViz


def test_face_dims_map_matches_spec():
    # front/back=l×h(W,H), left/right=w×h(D,H), top/bottom=l×w(W,D)
    assert FACE_DIMS["front"] == ("w_mm", "h_mm")
    assert FACE_DIMS["left"] == ("d_mm", "h_mm")
    assert FACE_DIMS["top"] == ("w_mm", "d_mm")


# ─── PalViz export: pełny przebieg na syntetycznym PDF ────────────────────────
def _synth_dieline(path, W=210, H=120, D=55, blank_bottom=True):
    fitz = pytest.importorskip("fitz")
    pw, ph = (2 * D + 2 * W), (H + 2 * D)          # mm, skala 1:1
    doc = fitz.open()
    page = doc.new_page(width=pw / 25.4 * 72, height=ph / 25.4 * 72)
    page.insert_text((30, 30), f"Wymiary {W} x {H} x {D} mm", fontsize=9)
    # kolorowe wypełnienie całej strony → każdy crop ma treść (std > próg)
    for i in range(0, int(page.rect.width), 12):
        page.draw_rect(fitz.Rect(i, 0, i + 6, page.rect.height),
                       color=None, fill=(0.2 + (i % 40) / 80, 0.5, 0.7))
    doc.save(path)
    doc.close()


def test_export_faces_full(tmp_path):
    pytest.importorskip("fitz")
    pdf = str(tmp_path / "d.pdf")
    _synth_dieline(pdf)
    out = str(tmp_path / "palviz")
    res = a3d.export_faces(pdf, out, level="sztuka", sku="ZAR-001")
    assert not res["needs_manual_dims"]

    man = json.load(open(os.path.join(out, "manifest.json"), encoding="utf-8"))
    assert man["level"] == "sztuka" and man["sku"] == "ZAR-001"
    assert set(man["faces"]) == set(FACES)          # sztuka = pełne 6 ścian
    assert man["dims_cm"] == {"l": 21.0, "w": 5.5, "h": 12.0}
    # każdy plik z manifestu istnieje + brak GLB/OBJ
    for face, fn in man["faces"].items():
        assert os.path.exists(os.path.join(out, fn))
    assert not any(f.lower().endswith((".glb", ".obj", ".stl"))
                   for f in os.listdir(out))
    # proporcja każdego zapisanego obrazka ≈ wymiary ścianki (±2%)
    from PIL import Image
    for face, fn in man["faces"].items():
        im = Image.open(os.path.join(out, fn))
        aw = {"front": 210, "back": 210, "left": 55, "right": 55, "top": 210, "bottom": 210}[face]
        ah = {"front": 120, "back": 120, "left": 120, "right": 120, "top": 55, "bottom": 55}[face]
        assert abs(im.width / im.height - aw / ah) / (aw / ah) <= 0.02


def test_faces_for_level():
    assert a3d.faces_for_level("karton") == ("front", "back", "left", "right")
    assert set(a3d.faces_for_level("sztuka")) == set(FACES)
    assert set(a3d.faces_for_level("opz")) == set(FACES)


def test_export_faces_karton_4walls(tmp_path):
    pytest.importorskip("fitz")
    pdf = str(tmp_path / "k.pdf")
    _synth_dieline(pdf)
    out = str(tmp_path / "k")
    res = a3d.export_faces(pdf, out, level="karton", sku="ZAR-KRT")
    man = json.load(open(os.path.join(out, "manifest.json"), encoding="utf-8"))
    # karton: tylko boki, bez góry/dołu
    assert set(man["faces"]) == {"front", "back", "left", "right"}
    assert "top" not in man["faces"] and "bottom" not in man["faces"]
    assert not os.path.exists(os.path.join(out, "top.png"))


def test_contact_sheet(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image
    panels = {f: Image.new("RGB", (60, 40), (i * 30 % 255, 90, 140))
              for i, f in enumerate(FACES)}
    out = str(tmp_path / "_contact.png")
    a3d.save_contact_sheet(panels, out)
    assert os.path.exists(out)
    assert max(Image.open(out).size) > 300      # siatka 3×2, niepusta


def test_export_faces_bad_level(tmp_path):
    pytest.importorskip("fitz")
    pdf = str(tmp_path / "d.pdf")
    _synth_dieline(pdf)
    with pytest.raises(ValueError):
        a3d.export_faces(pdf, str(tmp_path / "o"), level="paleta", sku="x")


def test_export_faces_needs_manual(tmp_path):
    fitz = pytest.importorskip("fitz")
    pdf = str(tmp_path / "nodims.pdf")
    doc = fitz.open(); doc.new_page(width=400, height=400)
    doc.save(pdf); doc.close()
    res = a3d.export_faces(pdf, str(tmp_path / "o"), level="sztuka", sku="x")
    assert res["needs_manual_dims"] is True


# ─── build_bundle — jeden przebieg daje GLB + PalViz ──────────────────────────
def test_build_bundle_full(tmp_path):
    trimesh = pytest.importorskip("trimesh")
    pytest.importorskip("fitz")
    W, H, D = 210, 120, 55
    pdf = str(tmp_path / "d.pdf")
    _synth_dieline(pdf, W, H, D)
    glb = str(tmp_path / "box.glb")
    faces_dir = str(tmp_path / "faces")
    res = a3d.build_bundle(pdf, glb, faces_dir, level="karton", sku="ZAR-1")
    assert not res["needs_manual_dims"]

    # GLB: istnieje, poprawny, właściwe wymiary
    assert os.path.exists(glb) and open(glb, "rb").read(4) == b"glTF"
    ext = sorted(round(x, 4) for x in trimesh.load(glb).bounding_box.extents)
    for a, b in zip(ext, sorted([W / 1000, H / 1000, D / 1000])):
        assert abs(a - b) < 1e-3

    # PalViz: manifest + PNG-i, brak plików 3D w katalogu ścianek
    man = json.load(open(os.path.join(faces_dir, "manifest.json"), encoding="utf-8"))
    assert man["level"] == "karton" and man["sku"] == "ZAR-1"
    assert man["dims_cm"] == {"l": 21.0, "w": 5.5, "h": 12.0}
    for fn in man["faces"].values():
        assert os.path.exists(os.path.join(faces_dir, fn))
    assert not any(f.lower().endswith((".glb", ".obj", ".stl"))
                   for f in os.listdir(faces_dir))
    assert res["faces_written"] == len(man["faces"])
