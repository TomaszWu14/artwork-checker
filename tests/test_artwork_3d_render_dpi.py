"""render_panels: DPI dobierane pod docelową teksturę (MAX_TEXTURE_PX), nie 40 Mpx.
Duży panel nie może być renderowany wielokrotnie większy, niż finalna tekstura."""
import pytest
import artwork_3d as a3d


_picked_dpi = a3d.pick_panel_dpi   # testujemy realną funkcję, nie replikę


def test_big_panel_capped_near_texture_size():
    # karton 800×600 mm ≈ 2268×1701 pt; przy 220 DPI byłoby ~6900 px szerokości
    use_dpi = _picked_dpi(2268, 1701)
    longest_px = 2268 / 72 * use_dpi
    assert longest_px <= 2 * a3d.MAX_TEXTURE_PX * 1.01   # ~zapas na int()
    assert use_dpi >= 40


def test_small_panel_keeps_full_dpi():
    # panel 100×50 mm ≈ 283×142 pt — 220 DPI daje ~865 px, poniżej celu
    assert _picked_dpi(283, 142) == a3d.DEFAULT_DPI


def test_render_panels_uses_capped_dpi(tmp_path):
    """End-to-end na malutkim PDF: render przechodzi i respektuje limit pikseli."""
    pymupdf = pytest.importorskip("pymupdf")   # CI nie instaluje ciężkich depsów
    pdf = tmp_path / "t.pdf"
    doc = pymupdf.open()
    doc.new_page(width=2268, height=1701)   # duża strona w pt
    doc.save(str(pdf)); doc.close()
    panels = a3d.render_panels(str(pdf), 0, {"front": (0, 0, 800, 600)})
    img = panels["front"]
    assert max(img.size) <= 2 * a3d.MAX_TEXTURE_PX * 1.01
