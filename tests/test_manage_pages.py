"""Smoke: strony portalu zarządzania renderują się dla managera i są gated dla usera."""


def test_manage_pages_render_for_manager(manager_client):
    for path in ("/artwork/manage", "/artwork/manage/unbound", "/artwork/manage/gaps"):
        r = manager_client.get(path)
        assert r.status_code == 200, f"{path} → {r.status_code}"

    kpi = manager_client.get("/api/artwork/manage/kpi")
    assert kpi.status_code == 200
    data = kpi.get_json()
    for key in ("total", "unbound", "obsolete", "duplicates", "gaps", "ignored"):
        assert key in data


def test_manage_hub_gated_for_user(user_client):
    r = user_client.get("/artwork/manage")
    # require_role blokuje: przekierowanie lub 403 (nie 200 z treścią pulpitu)
    assert r.status_code in (302, 303, 403), r.status_code
