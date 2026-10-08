"""palviz_push.py — push wygenerowanych renderów 3D (GLB) do zewnętrznego PalViz.

Faza 4 (PUSH-01..05). Jednostką pusha jest wiersz `artwork_render_registry`
(Faza 1). Po zaakceptowaniu/zarejestrowaniu renderu app.py odpala push w
daemon-threadzie (bez nowej infry/Redis — istniejący wzorzec).

Bezpieczeństwo/kontrakt:
  • Endpoint i sekret WYŁĄCZNIE z env (`PALVIZ_PUSH_URL`, `PALVIZ_API_KEY`) —
    nigdy hardcode. Bez `PALVIZ_PUSH_URL` push jest inertny (wiersz zostaje
    'pending', zero wywołań sieciowych) — bezpieczny default w dev/CI.
  • Idempotency-Key = stabilny PK wiersza rejestru (PUSH-03). Retry NIE tworzy
    duplikatu po stronie PalViz, o ile PalViz honoruje ten klucz UNIQUE-em.

    >>> KONTRAKT DLA PALVIZ (osobne repo Django) — do uzgodnienia przed prod: <<<
        PalViz przyjmuje POST z nagłówkiem `Idempotency-Key: artwork-reg-<id>`
        i `Authorization: Bearer <PALVIZ_API_KEY>`; body = JSON niżej
        (build_payload). Ponowny POST z tym samym Idempotency-Key MUSI być
        no-op (zwróć 200/409, nie drugi rekord). Transfer bajtów GLB nie jest
        w tym MVP — na razie metadane + referencja pliku; binarkę dołożyć, gdy
        PalViz określi endpoint uploadu.

Moduł „czysty": operuje na połączeniu DB przekazanym z wołającego; HTTP przez
seam `_http_post` (lazy import requests w środku) — testowalny bez sieci.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from core.audit import log_audit as _log_audit

# PUSH-04: minimalny odstęp między próbami pusha tego samego wiersza (bounded
# backoff — ręczne ponowienie, nie automatyczna kolejka). Sekundy.
_RETRY_MIN_INTERVAL_S = 30
_HTTP_TIMEOUT_S = 20


def is_enabled() -> bool:
    """Push aktywny tylko gdy skonfigurowano endpoint w env (PUSH-01)."""
    return bool((os.environ.get("PALVIZ_PUSH_URL") or "").strip())


def idempotency_key(registry_id) -> str:
    """Stabilny, unikalny klucz idempotency = PK wiersza rejestru (PUSH-03)."""
    return f"artwork-reg-{registry_id}"


def build_payload(row) -> dict:
    """Body POST-a do PalViz z wiersza rejestru (dict / sqlite3.Row)."""
    glb_path = row["glb_path"] or ""
    return {
        "idempotency_key": idempotency_key(row["id"]),
        "registry_id": row["id"],
        "ref_norm": row["ref_norm"] or "",
        "variant": row["variant"],
        "source_hash": row["source_hash"] or "",
        "glb_filename": os.path.basename(glb_path),
    }


def _http_post(url, payload, headers, timeout):
    """Seam sieciowy — realny POST. Testy podmieniają tę funkcję (bez requests/sieci).
    Zwraca (status_code:int, text:str). requests importowany leniwie (CI-safe)."""
    import requests
    r = requests.post(url, json=payload, headers=headers, timeout=timeout)
    return r.status_code, (r.text or "")[:500]


def _set_status(db, registry_id, status, error="") -> None:
    db.execute(
        "UPDATE artwork_render_registry SET push_status=?, push_error=?, "
        "push_at=?, push_attempts=push_attempts+1 WHERE id=?",
        (status, error[:500], datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
         registry_id),
    )
    db.commit()


def push_row(db, registry_id, *, user=None) -> dict:
    """Wypycha jeden wiersz rejestru do PalViz. Zwraca {'status': ...}.

    Bez konfiguracji (PALVIZ_PUSH_URL) → 'disabled', wiersz nietknięty. Sukces
    (2xx) → 'sent'; inaczej / wyjątek sieciowy → 'failed' + last error. Każda
    próba zwiększa push_attempts i jest audytowana (PUSH-05)."""
    row = db.execute(
        "SELECT id, ref_norm, variant, source_hash, glb_path FROM artwork_render_registry "
        "WHERE id=?", (registry_id,)
    ).fetchone()
    if not row:
        return {"status": "missing"}
    if not is_enabled():
        return {"status": "disabled"}

    url = os.environ["PALVIZ_PUSH_URL"].strip()
    api_key = (os.environ.get("PALVIZ_API_KEY") or "").strip()
    headers = {"Idempotency-Key": idempotency_key(registry_id),
               "Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = build_payload(row)

    try:
        code, text = _http_post(url, payload, headers, _HTTP_TIMEOUT_S)
    except Exception as e:  # sieć/timeout/DNS — nie wywalamy daemon-threadu
        err = f"{type(e).__name__}: {e}"
        _set_status(db, registry_id, "failed", err)
        _log_audit("palviz_push_failed", user,
                   f"reg={registry_id} variant={row['variant']} err={err}")
        return {"status": "failed", "error": err}

    if 200 <= code < 300:
        _set_status(db, registry_id, "sent", "")
        _log_audit("palviz_push_sent", user,
                   f"reg={registry_id} variant={row['variant']} http={code}")
        return {"status": "sent", "http": code}

    err = f"HTTP {code}: {text}"
    _set_status(db, registry_id, "failed", err)
    _log_audit("palviz_push_failed", user,
               f"reg={registry_id} variant={row['variant']} err={err}")
    return {"status": "failed", "error": err}


def recent_deliveries(db, status="", limit=50) -> list:
    """ADMIN-01: ostatnie próby pusha (wiersze rejestru) do panelu admina.
    Sort po czasie próby malejąco (nie-próbowane na końcu), opcjonalny filtr
    po `push_status`. Zwraca listę dictów z detalem błędu do wglądu + one-click retry."""
    try:
        limit = max(1, min(int(limit or 50), 500))
    except (TypeError, ValueError):
        limit = 50
    where, params = "", []
    status = (status or "").strip()
    if status:
        where = " WHERE push_status=?"
        params.append(status)
    rows = db.execute(
        # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
        "SELECT id, ref_norm, variant, push_status, push_at, push_error, "  # nosec B608
        "push_attempts, glb_path, created_at FROM artwork_render_registry"
        f"{where} ORDER BY COALESCE(push_at, created_at) DESC, id DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    return [dict(r) for r in rows]


def _latest_row_ids_for_fids(db, fids) -> list:
    """Id NAJNOWSZEGO wiersza rejestru per (fid, variant) dla podanych fid-ów
    (append-only → 'aktualny' = MAX(created_at)). Pomija historię."""
    fids = [int(f) for f in fids if f is not None]
    if not fids:
        return []
    rows = db.execute(
        # Bandit B608: interpolowane są tylko placeholdery ? (liczba = długość listy); wartości jako parametry.
        "SELECT id, fid, variant, created_at FROM artwork_render_registry "  # nosec B608
        f"WHERE fid IN ({','.join('?' * len(fids))})", fids
    ).fetchall()
    latest: dict = {}
    for r in rows:
        key = (r["fid"], r["variant"])
        prev = latest.get(key)
        if prev is None or (r["created_at"] or "") >= (prev["created_at"] or ""):
            latest[key] = {"id": r["id"], "created_at": r["created_at"]}
    return [v["id"] for v in latest.values()]


def push_group_fids(db, fids, *, user=None) -> dict:
    """BATCH-01: push najnowszego renderu per (fid, variant) dla całej grupy fid-ów.
    Sekwencyjnie (nie zalewa PalViz), każdy przez push_row (idempotency chroni przed
    duplikatem). Zwraca podsumowanie {sent, failed, disabled, total}."""
    row_ids = _latest_row_ids_for_fids(db, fids)
    summary = {"sent": 0, "failed": 0, "disabled": 0, "total": len(row_ids)}
    for rid in row_ids:
        res = push_row(db, rid, user=user)
        st = res.get("status")
        if st in summary:
            summary[st] += 1
    return summary


def retry_row(db, registry_id, *, user=None) -> dict:
    """PUSH-04: ręczne ponowienie z bounded backoff. Odrzuca ponowienie, jeśli
    ostatnia próba była < _RETRY_MIN_INTERVAL_S temu ('backoff')."""
    row = db.execute(
        "SELECT push_at FROM artwork_render_registry WHERE id=?", (registry_id,)
    ).fetchone()
    if not row:
        return {"status": "missing"}
    last = row["push_at"]
    if last:
        try:
            prev = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - prev).total_seconds() < _RETRY_MIN_INTERVAL_S:
                return {"status": "backoff",
                        "retry_after_s": _RETRY_MIN_INTERVAL_S}
        except (ValueError, TypeError):
            pass  # nieparsowalny timestamp — nie blokuj retry
    return push_row(db, registry_id, user=user)
