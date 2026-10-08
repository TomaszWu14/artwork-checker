"""Zapis kopii modelu 3D (GLB + PNG) do wskazanego katalogu — _a3d_copy_to_dir."""
import os
import pytest
import app as _app_mod


def test_copies_glb_and_png(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    dest = tmp_path / "dest"; dest.mkdir()
    glb = src / "1_abc.glb"; glb.write_bytes(b"glTF-fake")
    (src / "1_abc.png").write_bytes(b"PNG-fake")
    saved = _app_mod._a3d_copy_to_dir(str(glb), str(dest))
    assert sorted(os.path.basename(p) for p in saved) == ["1_abc.glb", "1_abc.png"]
    assert (dest / "1_abc.glb").read_bytes() == b"glTF-fake"


def test_missing_png_copies_only_glb(tmp_path):
    glb = tmp_path / "1_x.glb"; glb.write_bytes(b"g")
    dest = tmp_path / "d"; dest.mkdir()
    saved = _app_mod._a3d_copy_to_dir(str(glb), str(dest))
    assert [os.path.basename(p) for p in saved] == ["1_x.glb"]


def test_copy_roots_allowlist(tmp_path, monkeypatch):
    """Z ustawionym ARTWORK_COPY_ROOTS cel poza korzeniem jest odrzucany."""
    glb = tmp_path / "1_x.glb"; glb.write_bytes(b"g")
    allowed = tmp_path / "ok"; allowed.mkdir()
    outside = tmp_path / "nope"; outside.mkdir()
    monkeypatch.setenv("ARTWORK_COPY_ROOTS", str(allowed))
    assert _app_mod._a3d_copy_to_dir(str(glb), str(allowed))          # pod korzeniem: OK
    with pytest.raises(ValueError, match="dozwolonymi"):
        _app_mod._a3d_copy_to_dir(str(glb), str(outside))


def test_nonexistent_dir_raises(tmp_path):
    glb = tmp_path / "1_x.glb"; glb.write_bytes(b"g")
    with pytest.raises(ValueError, match="nie istnieje"):
        _app_mod._a3d_copy_to_dir(str(glb), str(tmp_path / "brak"))
