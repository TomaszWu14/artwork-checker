"""Regresja #1 (Sprint 1): trasy szablonów PalViz muszą wymagać roli manager.

Bug: `if not require_role("manager")` w blueprints/palviz.py nigdy nie blokował
(require_role zwraca dekorator = zawsze truthy), więc zwykły `user` obchodził bramkę.
Fix: dekorator @require_role("manager"). Te testy pilnują, że user dostaje 403,
a manager przechodzi bramkę uprawnień (nie 403).
"""
from tests.conftest import CSRF_TOKEN, set_session_csrf


def _post(client, url, **kw):
    set_session_csrf(client, CSRF_TOKEN)
    return client.post(url, data={"_csrf_token": CSRF_TOKEN, **kw})


def test_templates_save_blocks_plain_user(user_client):
    r = _post(user_client, "/api/artwork/templates/save")
    assert r.status_code == 403, f"user nie powinien mieć dostępu, dostał {r.status_code}"


def test_templates_analyze_blocks_plain_user(user_client):
    r = _post(user_client, "/api/artwork/templates/analyze")
    assert r.status_code == 403, f"user nie powinien mieć dostępu, dostał {r.status_code}"


def test_templates_save_allows_manager(manager_client):
    # Manager przechodzi bramkę roli — dalej może być 400 (brak danych), ale NIE 403.
    r = _post(manager_client, "/api/artwork/templates/save")
    assert r.status_code != 403, "manager nie powinien być blokowany rolą"
