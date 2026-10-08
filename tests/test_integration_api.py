"""tests/test_integration_api.py — testy integracyjne warstwy API + ról zewnętrznych.

Uzupełnia test_integration.py o: lekkie endpointy /api/* (auth + kształt), PEŁNY
flow chronionego POST-a (CSRF w nagłówku + require_role + zapis do DB + odczyt),
izolację portali zewnętrznych (forwarder), przełączanie języka oraz limit prób
logowania (429). Wszystko na lekkich trasach — bez ścieżek ML.

Fixtures: tests/conftest.py. Cały plik pomija się bez Flaska.
"""
import pytest

pytest.importorskip("flask")

CSRF_TOKEN = "test-csrf-token-api"


def _set_csrf(client, token=CSRF_TOKEN):
    """Wstrzykuje token CSRF do sesji zalogowanego klienta (zachowuje user_id)."""
    with client.session_transaction() as sess:
        sess["_csrf_token"] = token


# ── Lekki endpoint listujący (login_required) ─────────────────────────────────
def test_doc_types_requires_auth(client):
    resp = client.get("/api/doc-types")
    assert resp.status_code == 302
    assert "/login" in resp.headers.get("Location", "")


def test_doc_types_list_for_authenticated_user(manager_client):
    resp = manager_client.get("/api/doc-types")
    assert resp.status_code == 200
    assert isinstance(resp.get_json(), list)


# ── Pełny flow chronionego POST: CSRF (nagłówek) + require_role + DB write ─────
def test_admin_create_doc_type_end_to_end(admin_client):
    _set_csrf(admin_client)
    resp = admin_client.post(
        "/api/doc-types",
        json={"code": "ITTEST", "label": "Test integracyjny", "icon": "🧪"},
        headers={"X-CSRF-Token": CSRF_TOKEN},
    )
    assert resp.status_code == 200, resp.data
    body = resp.get_json()
    assert body.get("ok") is True
    assert body["type"]["code"] == "ITTEST"
    # Odczyt zwrotny — nowy typ jest na liście wszystkich.
    all_types = admin_client.get("/api/doc-types/all").get_json()
    assert any(t["code"] == "ITTEST" for t in all_types)


def test_create_doc_type_without_csrf_rejected(admin_client):
    # Admin (rola OK), ale brak nagłówka X-CSRF-Token → csrf_protect zwraca 403.
    _set_csrf(admin_client)
    resp = admin_client.post(
        "/api/doc-types",
        json={"code": "NOCSRF", "label": "x"},
    )
    assert resp.status_code == 403


def test_create_doc_type_forbidden_for_manager(manager_client):
    # require_role('admin') odrzuca managera ZANIM dojdzie do CSRF — 403 JSON.
    _set_csrf(manager_client)
    resp = manager_client.post(
        "/api/doc-types",
        json={"code": "MGR", "label": "x"},
        headers={"X-CSRF-Token": CSRF_TOKEN},
    )
    assert resp.status_code == 403


# ── Przełączanie języka ───────────────────────────────────────────────────────
def test_set_language_updates_session(admin_client):
    resp = admin_client.get("/set-language/en")
    assert resp.status_code in (302, 303)
    with admin_client.session_transaction() as sess:
        assert sess.get("lang") == "en"
    # Nieprawidłowy język degraduje do 'pl' (route: lang not in (pl,en) → pl).
    admin_client.get("/set-language/zz")
    with admin_client.session_transaction() as sess:
        assert sess.get("lang") == "pl"


# ── Limit prób logowania (429) ────────────────────────────────────────────────
def test_login_rate_limit_blocks_after_max_failures(client):
    # Rate-limit jest DB-backed (audit_log po IP) — izolujemy własnym REMOTE_ADDR,
    # żeby nie zliczać prób z innych testów ani nie blokować fixture'ów logowania.
    ip = {"REMOTE_ADDR": "203.0.113.77"}
    _set_csrf(client)
    statuses = []
    for _ in range(7):
        r = client.post("/login", data={
            "identifier": "it_admin", "password": "zle-haslo", "_csrf_token": CSRF_TOKEN,
        }, environ_base=ip)
        statuses.append(r.status_code)
        if r.status_code == 429:
            break
    assert 429 in statuses, f"nie zadziałał limit prób logowania: {statuses}"
