"""Faza 4 (PUSH-01..05): push wygenerowanych GLB do PalViz.

Env-gated (PALVIZ_PUSH_URL) — bez konfiguracji push jest inertny (wiersz zostaje
'pending', żadnych wywołań sieciowych). HTTP idzie przez seam _http_post, który
testy mockują — nie potrzeba realnego `requests` ani sieci.
"""
import sqlite3

import pytest

import palviz_push as pp


def _db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE artwork_render_registry (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fid INTEGER, ref_norm TEXT DEFAULT '', variant TEXT NOT NULL,
        source_hash TEXT DEFAULT '', glb_path TEXT NOT NULL,
        created_by INTEGER, created_at TEXT DEFAULT (datetime('now')),
        last_confirmed_at TEXT DEFAULT (datetime('now')),
        push_status TEXT DEFAULT 'pending', push_at TEXT, push_error TEXT DEFAULT '',
        push_attempts INTEGER DEFAULT 0
    )""")
    con.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "event TEXT, username TEXT, detail TEXT, extra TEXT, "
                "created_at TEXT DEFAULT (datetime('now')))")
    con.execute("INSERT INTO artwork_render_registry (ref_norm, variant, source_hash, glb_path) "
                "VALUES ('BTPC6060','marm','abc123','/tmp/x.glb')")
    con.commit()
    return con


def test_idempotency_key_is_stable_registry_pk():
    key1 = pp.idempotency_key(1)
    key2 = pp.idempotency_key(1)
    assert key1 == key2
    assert pp.idempotency_key(2) != key1
    assert "1" in key1


def test_disabled_when_no_url(monkeypatch):
    monkeypatch.delenv("PALVIZ_PUSH_URL", raising=False)
    assert pp.is_enabled() is False
    db = _db()
    res = pp.push_row(db, 1)
    assert res["status"] == "disabled"
    # wiersz nietknięty — nadal pending, żadnego wywołania sieci
    row = db.execute("SELECT push_status FROM artwork_render_registry WHERE id=1").fetchone()
    assert row["push_status"] == "pending"


def test_push_success_marks_sent(monkeypatch):
    monkeypatch.setenv("PALVIZ_PUSH_URL", "https://palviz.example/api/renders")
    monkeypatch.setenv("PALVIZ_API_KEY", "secret")
    calls = {}

    def fake_post(url, payload, headers, timeout):
        calls["url"] = url
        calls["headers"] = headers
        calls["payload"] = payload
        return 200, '{"ok":true}'

    events = []
    monkeypatch.setattr(pp, "_log_audit", lambda ev, *a, **k: events.append(ev))
    monkeypatch.setattr(pp, "_http_post", fake_post)
    db = _db()
    res = pp.push_row(db, 1, user="admin")
    assert res["status"] == "sent"
    row = db.execute("SELECT push_status, push_at, push_attempts FROM artwork_render_registry WHERE id=1").fetchone()
    assert row["push_status"] == "sent"
    assert row["push_at"] is not None
    assert row["push_attempts"] == 1
    # idempotency key wysłany w nagłówku, sekret z env w Authorization
    assert calls["headers"]["Idempotency-Key"] == pp.idempotency_key(1)
    assert "secret" in calls["headers"]["Authorization"]
    # audyt sukcesu
    assert events == ["palviz_push_sent"]


def test_push_failure_marks_failed_with_error(monkeypatch):
    monkeypatch.setenv("PALVIZ_PUSH_URL", "https://palviz.example/api/renders")

    def fake_post(url, payload, headers, timeout):
        return 500, "boom"

    events = []
    monkeypatch.setattr(pp, "_log_audit", lambda ev, *a, **k: events.append(ev))
    monkeypatch.setattr(pp, "_http_post", fake_post)
    db = _db()
    res = pp.push_row(db, 1)
    assert res["status"] == "failed"
    row = db.execute("SELECT push_status, push_error FROM artwork_render_registry WHERE id=1").fetchone()
    assert row["push_status"] == "failed"
    assert "500" in row["push_error"]
    assert events == ["palviz_push_failed"]


def test_network_exception_marks_failed(monkeypatch):
    monkeypatch.setenv("PALVIZ_PUSH_URL", "https://palviz.example/api/renders")

    def fake_post(url, payload, headers, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(pp, "_http_post", fake_post)
    db = _db()
    res = pp.push_row(db, 1)
    assert res["status"] == "failed"
    row = db.execute("SELECT push_status, push_error FROM artwork_render_registry WHERE id=1").fetchone()
    assert row["push_status"] == "failed"
    assert "connection refused" in row["push_error"]


def test_recent_deliveries_orders_and_filters():
    """ADMIN-01: ostatnie dostawy — sort po czasie próby DESC, filtr po statusie."""
    db = _db()
    # dołóż drugi i trzeci wiersz o różnych statusach/czasach
    db.execute("INSERT INTO artwork_render_registry (ref_norm, variant, glb_path, "
               "push_status, push_at, push_attempts) VALUES "
               "('R2','dieline','/tmp/2.glb','sent','2026-09-04 10:00:00',1)")
    db.execute("INSERT INTO artwork_render_registry (ref_norm, variant, glb_path, "
               "push_status, push_at, push_attempts) VALUES "
               "('R3','photo','/tmp/3.glb','failed','2026-09-04 11:00:00',2)")
    db.commit()

    allrows = pp.recent_deliveries(db, limit=10)
    order = [r["ref_norm"] for r in allrows]
    # sort po czasie próby DESC: R3 (11:00) przed R2 (10:00)
    assert order.index("R3") < order.index("R2")
    assert {"ref_norm", "variant", "push_status", "push_at", "push_error",
            "push_attempts", "id"} <= set(allrows[0].keys())

    failed = pp.recent_deliveries(db, status="failed", limit=10)
    assert [r["push_status"] for r in failed] == ["failed"]

    assert len(pp.recent_deliveries(db, limit=1)) == 1


def test_push_group_fids_pushes_latest_per_variant(monkeypatch):
    """BATCH-01: push całej grupy — najnowszy render per (fid, variant), pomija
    starsze wiersze historii. Podsumowanie liczy sent/failed/disabled."""
    monkeypatch.setenv("PALVIZ_PUSH_URL", "https://palviz.example/api/renders")
    monkeypatch.setattr(pp, "_log_audit", lambda *a, **k: None)
    pushed = []
    monkeypatch.setattr(pp, "_http_post", lambda url, p, h, t: (pushed.append(p["registry_id"]), (200, "ok"))[1])

    db = _db()
    db.execute("DELETE FROM artwork_render_registry")
    # fid=1 dieline: dwa wiersze historii (nowszy id=2 wygrywa)
    db.execute("INSERT INTO artwork_render_registry (id, fid, variant, glb_path, created_at) "
               "VALUES (1,1,'dieline','/a.glb','2026-09-01 10:00:00')")
    db.execute("INSERT INTO artwork_render_registry (id, fid, variant, glb_path, created_at) "
               "VALUES (2,1,'dieline','/a2.glb','2026-09-02 10:00:00')")
    # fid=2 marm
    db.execute("INSERT INTO artwork_render_registry (id, fid, variant, glb_path, created_at) "
               "VALUES (3,2,'marm','/b.glb','2026-09-01 10:00:00')")
    db.commit()

    summary = pp.push_group_fids(db, [1, 2], user="admin")
    assert summary["sent"] == 2 and summary["failed"] == 0
    assert sorted(pushed) == [2, 3]           # najnowszy dieline (2) + marm (3), NIE 1


def test_retry_backoff_blocks_rapid_retry(monkeypatch):
    """PUSH-04: bounded backoff — ponowienie tuż po ostatniej próbie jest odrzucane."""
    monkeypatch.setenv("PALVIZ_PUSH_URL", "https://palviz.example/api/renders")
    monkeypatch.setattr(pp, "_http_post", lambda *a, **k: (500, "boom"))
    db = _db()
    pp.push_row(db, 1)                       # 1. próba → failed, świeży push_at
    res = pp.retry_row(db, 1, user="admin")  # natychmiastowy retry
    assert res["status"] == "backoff"        # zablokowany, nie kolejna próba
    assert db.execute("SELECT push_attempts FROM artwork_render_registry WHERE id=1").fetchone()[0] == 1
