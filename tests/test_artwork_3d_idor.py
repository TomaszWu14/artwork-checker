"""IDOR na trasach async-3D: /status, /batch/status, /batch/zip.

Wszystkie trzy przechodzą teraz przez wspólny helper _owner_forbidden — obcy
job (należący do innego user_id) musi dać 403, właściciela helper przepuszcza.
"""
import app as _app_mod


def _make_job(owner_id):
    return _app_mod._async_job_create(owner_id)


def test_3d_status_denies_other_user(user_client):
    jid = _make_job(999999)
    r = user_client.get(f"/api/artwork/3d/status/{jid}")
    assert r.status_code == 403, f"obcy job powinien dać 403, dostał {r.status_code}"


def test_batch_status_denies_other_user(user_client):
    jid = _make_job(999999)
    r = user_client.get(f"/api/artwork/3d/batch/status/{jid}")
    assert r.status_code == 403, f"obcy batch job → 403, dostał {r.status_code}"


def test_batch_zip_denies_other_user(user_client):
    jid = _make_job(999999)
    r = user_client.get(f"/api/artwork/3d/batch/zip/{jid}")
    assert r.status_code == 403, f"obcy batch zip → 403, dostał {r.status_code}"


def test_3d_status_owner_passes_guard(user_client):
    with user_client.session_transaction() as s:
        uid = s["user_id"]
    jid = _make_job(uid)
    r = user_client.get(f"/api/artwork/3d/status/{jid}")
    assert r.status_code != 403, "właściciel nie powinien być blokowany"
