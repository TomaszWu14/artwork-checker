"""Fallback shapely w wykrywaniu paneli 3D — klasyfikacja ścianek po rozmiarze/pozycji.

Testujemy czystą logikę `_match_faces_to_panels` (bez PDF): zestaw wieloboków o
rozmiarach paneli w×h / d×h / w×d ma zostać poprawnie przypisany do 6 ścian.
`_polygonize_faces` (wymaga strony PyMuPDF) zweryfikowano na realnym dielinie ręcznie.
"""
import artwork_3d as a3


def test_match_faces_assigns_all_six():
    w, h, d = 100.0, 60.0, 40.0
    faces = [
        (0, 0, 100, 60),        # front  (w×h)
        (200, 0, 300, 60),      # back   (w×h)
        (0, 100, 40, 160),      # left   (d×h)
        (60, 100, 100, 160),    # right  (d×h)
        (0, 200, 100, 240),     # top    (w×d)
        (0, 300, 100, 340),     # bottom (w×d)
    ]
    res = a3._match_faces_to_panels(faces, w, h, d, tol=3.0)
    assert set(res.keys()) == {"front", "back", "left", "right", "top", "bottom"}, res


def test_match_faces_orientation_insensitive():
    # ścianka obrócona (h×w zamiast w×h) też ma się dopasować
    w, h, d = 100.0, 60.0, 40.0
    faces = [(0, 0, 60, 100)]   # 60×100 = h×w (front obrócony)
    res = a3._match_faces_to_panels(faces, w, h, d, tol=3.0)
    assert "front" in res, res


def test_match_faces_empty():
    assert a3._match_faces_to_panels([], 100, 60, 40, tol=3.0) == {}
