"""#5 spójność: single-worker 3D trzyma _HEAVY_SEM (jak batch).

VTK/pyvista bywa nietrwały współbieżnie — generacja+render w _artwork_3d_worker
musi być objęta tym samym semaforem co batch (_artwork_3d_batch_worker).
"""
import app as _app_mod
import artwork_3d


def test_single_worker_holds_heavy_sem(monkeypatch, tmp_path):
    sem = _app_mod._HEAVY_SEM
    before = sem._value
    seen = {}

    def fake_gen(pdf, glb, dims_override=None):
        seen["during"] = sem._value          # ile pozostało wolnych slotów w trakcie
        return {"dims_mm": [1, 2, 3], "package_type": "", "ean": "",
                "panels_found": [], "warnings": []}

    monkeypatch.setattr(artwork_3d, "generate_artwork_3d", fake_gen)
    monkeypatch.setattr(artwork_3d, "render_glb_preview", lambda *a, **k: False)

    tmp_pdf = tmp_path / "x.pdf"
    tmp_pdf.write_bytes(b"%PDF-")
    with _app_mod.app.app_context():
        _app_mod._artwork_3d_worker("nojob", str(tmp_pdf), str(tmp_path / "o.glb"),
                                    str(tmp_path), "o.glb", "o.png", None,
                                    "it_user", "x.pdf")

    assert seen["during"] == before - 1, "semafor musi być trzymany podczas generacji"
    assert sem._value == before, "semafor musi być zwolniony po zakończeniu"
