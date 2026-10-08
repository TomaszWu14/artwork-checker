"""Reguła kartonów: pusta/niezadrukowana ścianka jest zawsze górą lub dołem
(enforce_plain_top_bottom). Fireuje tylko dla realnie pustych par; drukowane pudełka
(wszystkie ścianki z nadrukiem) muszą zostać nietknięte (brak fałszywego obrotu)."""
import pytest

# CI celowo nie instaluje ciężkich zależności (numpy/PIL wchodzą z torch/opencv), a
# import na sztywno wywracał CAŁĄ kolekcję testów — nie tylko ten plik. Repo używa
# do tego importorskip; ten moduł go pomijał i przez to gałąź nigdy nie była zielona.
np = pytest.importorskip("numpy")
Image = pytest.importorskip("PIL.Image")

import artwork_3d as a3  # noqa: E402


def _plain():
    return Image.new("RGB", (80, 80), (240, 240, 240))


def _color(seed):
    rng = np.random.RandomState(seed)
    return Image.fromarray((rng.rand(80, 80, 3) * 255).astype("uint8"), "RGB")


def _panels(blank_pair):
    names = ["front", "back", "left", "right", "top", "bottom"]
    return {n: (_plain() if n in blank_pair else _color(i))
            for i, n in enumerate(names)}


def test_blank_front_back_moved_to_top_bottom():
    info = a3.DielineInfo(); info.w_mm, info.h_mm, info.d_mm = 100.0, 60.0, 40.0
    out = a3.enforce_plain_top_bottom(_panels({"front", "back"}), info)
    assert a3.colorfulness(out["top"]) < 1 and a3.colorfulness(out["bottom"]) < 1
    assert info.h_mm == 40.0 and info.d_mm == 60.0     # h↔d zamienione


def test_blank_left_right_moved_to_top_bottom():
    info = a3.DielineInfo(); info.w_mm, info.h_mm, info.d_mm = 100.0, 60.0, 40.0
    out = a3.enforce_plain_top_bottom(_panels({"left", "right"}), info)
    assert a3.colorfulness(out["top"]) < 1 and a3.colorfulness(out["bottom"]) < 1


def test_blank_already_top_bottom_is_noop():
    info = a3.DielineInfo(); info.w_mm, info.h_mm, info.d_mm = 100.0, 60.0, 40.0
    out = a3.enforce_plain_top_bottom(_panels({"top", "bottom"}), info)
    assert a3.colorfulness(out["top"]) < 1          # puste zostają na górze/dole
    assert info.h_mm == 60.0                        # wymiary bez zmian


def test_all_printed_no_rotation():
    # Drukowane pudełko (żadna ścianka nie jest blank) — brak obrotu (zero fałszywych zamian).
    info = a3.DielineInfo(); info.w_mm, info.h_mm, info.d_mm = 210.0, 120.0, 55.0
    out = a3.enforce_plain_top_bottom(_panels(set()), info)
    assert (info.w_mm, info.h_mm, info.d_mm) == (210.0, 120.0, 55.0)


def test_rule_runs_before_orientation_in_pipeline():
    """Strażnik kolejności — to był konflikt przy scalaniu z main.

    enforce_plain_top_bottom PRZENOSI panele między pozycjami (front→góra), a
    orient_panel dokłada fallbackową PANEL_ROTATION zależną od nazwy-POZYCJI. Jeśli
    reguła zadziała PO orientacji, panel przeniesiony na górę poniesie rotację swojej
    starej pozycji i napis wyjdzie odwrócony. Wymaga też kompletu 6 ścian, więc musi
    być po complete_panels."""
    import inspect
    import artwork_3d as a3

    src = inspect.getsource(a3.generate_artwork_3d)
    order = [src.index(name) for name in
             ("complete_panels(", "enforce_plain_top_bottom(", "orient_panel(")]
    assert order == sorted(order), (
        "kolejność w generate_artwork_3d musi być: complete_panels → "
        "enforce_plain_top_bottom → orient_panel")
