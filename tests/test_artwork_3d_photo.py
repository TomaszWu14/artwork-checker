"""Model 3D ze zdjęć: _a3d_photo_panels — komplet 6 ścian z częściowego zestawu."""
import pytest

PIL = pytest.importorskip("PIL")  # CI ma Pillow w prod deps, ale zabezpieczenie
from PIL import Image  # noqa: E402
import app as _app_mod  # noqa: E402


def test_missing_panels_get_blanks_with_bg_color():
    red = Image.new("RGB", (100, 60), (200, 30, 30))
    panels = _app_mod._a3d_photo_panels({"front": red}, 210, 120, 55)
    assert set(panels) == {"front", "back", "left", "right", "top", "bottom"}
    assert panels["front"] is red
    # blank w kolorze tła zdjęcia (róg = czerwony)
    assert panels["back"].getpixel((0, 0)) == (200, 30, 30)


def test_fill_color_palette_overrides_corner_color():
    # Flow z MARM: jawny fill_color → jednolita barwa gładkiego boxu, niezależnie od zdjęć.
    red = Image.new("RGB", (100, 60), (200, 30, 30))
    carton = _app_mod._a3d_photo_panels({"front": red}, 210, 120, 55, fill_color="carton")
    assert carton["back"].getpixel((0, 0)) == (198, 166, 120)
    white = _app_mod._a3d_photo_panels({"front": red}, 210, 120, 55, fill_color="white")
    assert white["back"].getpixel((0, 0)) == (255, 255, 255)


def test_no_photos_renders_solid_box():
    # Bez zdjęć (render tylko z wymiarów MARM) — wszystkie 6 ścian w wybranym kolorze.
    panels = _app_mod._a3d_photo_panels({}, 100, 80, 60, fill_color="white")
    assert set(panels) == {"front", "back", "left", "right", "top", "bottom"}
    assert all(p.getpixel((0, 0)) == (255, 255, 255) for p in panels.values())
    # Domyślnie (fill_color=None, brak zdjęć) → karton.
    default = _app_mod._a3d_photo_panels({}, 100, 80, 60)
    assert default["front"].getpixel((0, 0)) == (198, 166, 120)


def test_marm_routes_registered():
    rules = {r.rule for r in _app_mod.app.url_map.iter_rules()}
    assert "/artwork/3d/marm" in rules
    assert "/api/materials/marm-import" in rules
    assert "/api/materials/marm/search" in rules


def test_blank_proportions_follow_face_dims():
    img = Image.new("RGB", (10, 10), (255, 255, 255))
    p = _app_mod._a3d_photo_panels({"front": img}, w_mm=200, h_mm=100, d_mm=50)
    # left/right = głębokość × wysokość; top/bottom = szerokość × głębokość
    lw, lh = p["left"].size
    assert lw / lh == pytest.approx(50 / 100, rel=0.1)
    tw, th = p["top"].size
    assert tw / th == pytest.approx(200 / 50, rel=0.1)


def test_unwarp_axis_aligned_quad_crops_region():
    """Quad pokrywający prostokąt 50..150 na obrazie 200×200 → wycinek ~100×100
    w kolorze regionu (NW SW SE NE, znormalizowane)."""
    img = Image.new("RGB", (200, 200), (255, 255, 255))
    img.paste((200, 30, 30), (50, 50, 150, 150))
    out = _app_mod._a3d_photo_unwarp(img, "0.25,0.25 0.25,0.75 0.75,0.75 0.75,0.25")
    assert abs(out.width - 100) <= 2 and abs(out.height - 100) <= 2
    assert out.getpixel((out.width // 2, out.height // 2)) == (200, 30, 30)


def test_unwarp_bad_quad_raises():
    img = Image.new("RGB", (10, 10))
    with pytest.raises(ValueError):
        _app_mod._a3d_photo_unwarp(img, "0,0 1,1")


def test_photo_route_registered():
    rules = {r.rule for r in _app_mod.app.url_map.iter_rules()}
    assert "/artwork/3d/photo" in rules
    assert "/api/artwork/3d/photo" in rules
