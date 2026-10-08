"""#3 Sprint 1: kontrola własności (IDOR) na in-memory cache porównań artworków.

Guard w /api/artwork/page/<cache_id> musi odmówić, gdy wpis należy do innego
użytkownika LUB ma nieznanego właściciela (owner_id=None) — wcześniej `owner_id is None`
omijało kontrolę i czyniło wpis world-readable.
"""
import app as _app_mod


class _FakeResult:
    """Atrapa wyniku porównania — page_images wołane dopiero PO bramce własności."""
    def page_images(self, idx):
        return {"pages": [], "idx": idx}


def test_page_denies_other_user(user_client):
    _app_mod._cache_put("idor_other", _FakeResult(), owner_id=999999)
    r = user_client.get("/api/artwork/page/idor_other/0")
    assert r.status_code == 403, f"obcy wpis powinien dać 403, dostał {r.status_code}"


def test_page_denies_unknown_owner(user_client):
    _app_mod._cache_put("idor_none", _FakeResult(), owner_id=None)
    r = user_client.get("/api/artwork/page/idor_none/0")
    assert r.status_code == 403, f"wpis bez właściciela powinien dać 403, dostał {r.status_code}"


def test_page_owner_passes_guard(user_client):
    with user_client.session_transaction() as s:
        uid = s["user_id"]
    _app_mod._cache_put("idor_own", _FakeResult(), owner_id=uid)
    r = user_client.get("/api/artwork/page/idor_own/0")
    # Właściciel przechodzi bramkę własności — dalej może paść 500 (dummy obiekt bez
    # page_images), ale NIE 403.
    assert r.status_code != 403, "właściciel nie powinien być blokowany"
