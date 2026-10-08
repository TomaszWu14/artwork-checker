"""#2 Sprint 1: walidacja uploadu po magic-bytes (nie tylko rozszerzeniu).

Plik z rozszerzeniem .pdf, ale treścią nie-PDF, musi zostać odrzucony (400),
zanim trafi do ciężkiego przetwarzania.
"""
import io

from tests.conftest import CSRF_TOKEN, set_session_csrf
import app as _app_mod


def test_helper_rejects_non_pdf(tmp_path):
    p = tmp_path / "fake.pdf"
    p.write_bytes(b"NOT A PDF AT ALL")
    assert _app_mod._validate_upload_magic(str(p)) is False


def test_helper_accepts_pdf(tmp_path):
    p = tmp_path / "real.pdf"
    p.write_bytes(b"%PDF-1.7\n%eof")
    assert _app_mod._validate_upload_magic(str(p)) is True


def test_helper_accepts_png(tmp_path):
    p = tmp_path / "img.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    assert _app_mod._validate_upload_magic(str(p)) is True


def test_magic_ok_shared_helper():
    # obie funkcje walidacyjne wołają _magic_ok — sprawdź go bezpośrednio
    assert _app_mod._magic_ok(b"%PDF-1.7") is True
    assert _app_mod._magic_ok(b"\x89PNG\r\n\x1a\n\x00\x00") is True
    assert _app_mod._magic_ok(b"RIFF\x00\x00\x00\x00WEBP") is True
    assert _app_mod._magic_ok(b"NOT A PDF") is False


def test_stream_and_path_agree(tmp_path):
    # stream i path muszą dawać ten sam wynik (wspólny _magic_ok)
    payload = b"\xff\xd8\xff\xe0" + b"\x00" * 12
    p = tmp_path / "img.jpg"
    p.write_bytes(payload)

    class _FS:
        stream = io.BytesIO(payload)
    assert _app_mod._validate_upload_magic(str(p)) is True
    assert _app_mod._validate_upload_magic_stream(_FS()) is True


def test_compare_rejects_spoofed_pdf(admin_client):
    set_session_csrf(admin_client, CSRF_TOKEN)
    data = {
        "_csrf_token": CSRF_TOKEN,
        "file_a": (io.BytesIO(b"this is not a pdf"), "a.pdf"),
        "file_b": (io.BytesIO(b"this is not a pdf either"), "b.pdf"),
    }
    r = admin_client.post("/api/artwork/compare", data=data,
                          content_type="multipart/form-data")
    assert r.status_code == 400, f"spoofowany PDF powinien dać 400, dostał {r.status_code}"
    body = r.get_data(as_text=True).lower()
    assert "rozszerzenie" in body, body  # komunikat: "...rozszerzenie nie zgadza się z treścią"
