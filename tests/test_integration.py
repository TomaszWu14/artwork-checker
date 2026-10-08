"""tests/test_integration.py — testy integracyjne przez Flask test_client().

Wcześniej zestaw testów nie dotykał warstwy HTTP/auth w ogóle. Te testy jadą
przez PRAWDZIWY request/response: health, logowanie (sukces/błąd/CSRF), bramkę
auth (przekierowanie anonima), gating ról (403), nagłówki bezpieczeństwa oraz
kształt handlerów 404 (JSON dla /api/*, HTML poza nim).

Nie dotykają ciężkich ścieżek ML (OCR/porównania) — tylko lekkie trasy, tak by
CI kończył w sekundy bez torch/paddleocr.

Fixtures: patrz tests/conftest.py. Cały plik pomija się bez Flaska.
"""
import pytest

pytest.importorskip("flask")

# Token używany w testach robiących ręczny POST /login (musi się zgadzać z tym,
# co dany test wstawia do sesji przez session_transaction). Fixtures logowania
# w conftest.py używają własnego — nie muszą być tożsame.
CSRF_TOKEN = "test-csrf-token-local"


# ── Health ────────────────────────────────────────────────────────────────────
def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data.get("db") == "ok"


# ── Strona logowania ──────────────────────────────────────────────────────────
def test_login_page_renders(client):
    resp = client.get("/login")
    assert resp.status_code == 200
    assert b"identifier" in resp.data  # pole formularza login/email


# ── Bramka auth ───────────────────────────────────────────────────────────────
def test_root_redirects_anonymous_to_login(client):
    resp = client.get("/")
    assert resp.status_code == 302
    assert "/login" in resp.headers.get("Location", "")


def test_protected_page_redirects_anonymous(client):
    # /artwork jest login_required — anonim ma dostać przekierowanie na /login.
    resp = client.get("/artwork")
    assert resp.status_code == 302
    assert "/login" in resp.headers.get("Location", "")


# ── Logowanie: sukces / błędne hasło / brak CSRF ──────────────────────────────
def test_login_success_grants_access(admin_client):
    # admin_client jest już po udanym logowaniu; chroniona strona ma zwrócić 200.
    resp = admin_client.get("/artwork")
    assert resp.status_code == 200


def test_login_wrong_password_no_redirect(client):
    with client.session_transaction() as sess:
        sess["_csrf_token"] = CSRF_TOKEN
    resp = client.post("/login", data={
        "identifier": "it_admin",
        "password": "zle-haslo",
        "_csrf_token": CSRF_TOKEN,
    })
    # Błędne hasło → NIE przekierowuje (renderuje formularz z błędem), status 200.
    assert resp.status_code == 200
    assert b"user_id" not in resp.data  # brak sesji
    # Nadal anonim — chroniona strona przekierowuje.
    assert client.get("/artwork").status_code == 302


def test_login_without_csrf_is_rejected(client):
    # Brak tokenu CSRF w sesji i w formularzu → csrf_protect zwraca 403.
    resp = client.post("/login", data={
        "identifier": "it_admin",
        "password": "itpass-admin-123",
    })
    assert resp.status_code == 403


def test_login_bad_csrf_is_rejected(client):
    with client.session_transaction() as sess:
        sess["_csrf_token"] = CSRF_TOKEN
    resp = client.post("/login", data={
        "identifier": "it_admin",
        "password": "itpass-admin-123",
        "_csrf_token": "wrong-token",
    })
    assert resp.status_code == 403


def test_login_non_ascii_csrf_rejected_cleanly(client):
    # Regresja: token CSRF ze znakami spoza ASCII (np. polskimi) rzucał w
    # hmac.compare_digest TypeError → 500 zamiast czystego 403. Ma być 403.
    with client.session_transaction() as sess:
        sess["_csrf_token"] = CSRF_TOKEN
    resp = client.post("/login", data={
        "identifier": "it_admin",
        "password": "itpass-admin-123",
        "_csrf_token": "zły-token-ąęó",
    })
    assert resp.status_code == 403


# ── Wylogowanie ───────────────────────────────────────────────────────────────
def test_logout_clears_session(admin_client):
    assert admin_client.get("/artwork").status_code == 200
    resp = admin_client.get("/logout")
    assert resp.status_code == 302
    # Po wylogowaniu chroniona strona znów przekierowuje na login.
    assert admin_client.get("/artwork").status_code == 302


# ── Gating ról (require_role) ─────────────────────────────────────────────────
def test_admin_endpoint_forbidden_for_manager(manager_client):
    resp = manager_client.get("/api/admin/schema-versions")
    assert resp.status_code == 403
    assert resp.get_json().get("error")  # {"error": "Brak uprawnień"}


def test_admin_endpoint_allowed_for_admin(admin_client):
    resp = admin_client.get("/api/admin/schema-versions")
    assert resp.status_code == 200
    assert isinstance(resp.get_json(), list)


def test_admin_endpoint_redirects_anonymous(client):
    resp = client.get("/api/admin/schema-versions")
    # Brak sesji → require_role przekierowuje na login (nie 403).
    assert resp.status_code == 302
    assert "/login" in resp.headers.get("Location", "")


# ── Nagłówki bezpieczeństwa ───────────────────────────────────────────────────
def test_security_headers_present(client):
    resp = client.get("/login")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options")  # SAMEORIGIN
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "frame-ancestors" in csp
    # Server header ma być usunięty.
    assert "Server" not in resp.headers or resp.headers.get("Server") in (None, "")


# ── Handlery błędów: JSON dla /api/*, HTML poza nim ───────────────────────────
def test_api_404_returns_json(client):
    resp = client.get("/api/nie-ma-takiego-endpointu")
    assert resp.status_code == 404
    assert resp.is_json
    assert resp.get_json().get("error")


def test_html_404_returns_html(client):
    resp = client.get("/nie-ma-takiej-strony-xyz")
    assert resp.status_code == 404
    assert not resp.is_json
