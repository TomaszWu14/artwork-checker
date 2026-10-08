"""tests/conftest.py — wspólne fixtures dla testów INTEGRACYJNYCH (Flask test_client).

Do tej pory cały zestaw to były testy czystej logiki; nic nie przechodziło przez
prawdziwy request/response, auth ani warstwę HTTP. Te fixtures udostępniają
`client` (anonimowy) oraz zalogowane `admin_client`/`manager_client`, przechodząc
PRAWDZIWY flow logowania (z tokenem CSRF) na izolowanej bazie w temp.

Import `app` ma efekty uboczne na poziomie modułu (tworzy tabele, seeduje userów
z LOSOWYMI hasłami), więc:
  • env (SECRET_KEY / SQLITE_PATH / brak DATABASE_URL) MUSI być ustawiony PRZED importem,
  • znane konta testowe wstrzykujemy sami (seedowych haseł nie znamy — są losowe).

Cały plik pomija się, gdy Flask nie jest zainstalowany (minimalne środowisko).
"""
import os
import tempfile

import pytest

pytest.importorskip("flask")

# ── Izolowane środowisko PRZED importem app ───────────────────────────────────
_TMP = tempfile.mkdtemp(prefix="doccompare_it_")
os.environ.setdefault("SECRET_KEY", "test-integration-secret")
os.environ["SQLITE_PATH"] = os.path.join(_TMP, "it.db")
os.environ.pop("DATABASE_URL", None)   # wymuś SQLite + wyłącz secure-cookie (http w teście)
os.environ.pop("FORCE_HTTPS", None)

import app as _app_mod  # noqa: E402

# Znane konta testowe (seed używa losowych haseł, więc wstrzykujemy własne).
# (username: (hasło, rola))
TEST_USERS = {
    "it_admin":     ("itpass-admin-123",     "admin"),
    "it_manager":   ("itpass-manager-123",   "manager"),
    "it_user":      ("itpass-user-123",      "user"),
}
CSRF_TOKEN = "test-csrf-token-deadbeef"


def _ensure_test_users():
    db = _app_mod.get_db()
    try:
        for uname, (pwd, role) in TEST_USERS.items():
            pwhash = _app_mod.generate_password_hash(pwd)
            row = db.execute("SELECT id FROM users WHERE username=?", (uname,)).fetchone()
            if row:
                db.execute("UPDATE users SET password_hash=?, role=? WHERE username=?",
                           (pwhash, role, uname))
            else:
                db.execute("INSERT INTO users(username, password_hash, role) VALUES (?,?,?)",
                           (uname, pwhash, role))
        db.commit()
    finally:
        db.close()


_ensure_test_users()


@pytest.fixture
def client():
    _app_mod.app.config["TESTING"] = True
    # Wyzeruj rate-limiter logowania (stan w pamięci procesu), żeby kolejność
    # testów nie powodowała fałszywych 429.
    try:
        with _app_mod._login_attempts_lock:
            _app_mod._login_attempts.clear()
    except Exception:
        pass
    return _app_mod.app.test_client()


def login(client, username):
    """Wykonuje PRAWDZIWY flow logowania (z tokenem CSRF). Zwraca odpowiedź POST /login."""
    pwd = TEST_USERS[username][0]
    with client.session_transaction() as sess:
        sess["_csrf_token"] = CSRF_TOKEN
    return client.post("/login", data={
        "identifier": username,
        "password": pwd,
        "_csrf_token": CSRF_TOKEN,
    })


@pytest.fixture
def admin_client(client):
    resp = login(client, "it_admin")
    assert resp.status_code in (302, 303), f"login admina nie przekierował (CSRF/hasło?): {resp.status_code}"
    return client


@pytest.fixture
def manager_client(client):
    resp = login(client, "it_manager")
    assert resp.status_code in (302, 303), f"login managera nie przekierował: {resp.status_code}"
    return client


@pytest.fixture
def user_client(client):
    resp = login(client, "it_user")
    assert resp.status_code in (302, 303), f"login usera nie przekierował: {resp.status_code}"
    return client


def set_session_csrf(client, token):
    """Wstrzykuje token CSRF do sesji zalogowanego klienta (zachowuje user_id),
    by testować chronione POST-y bez parsowania HTML."""
    with client.session_transaction() as sess:
        sess["_csrf_token"] = token
