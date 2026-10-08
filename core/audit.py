"""
core/audit.py — zapis zdarzeń do tabeli audit_log.

Wydzielone z app.py (helper używany w ~setkach miejsc), by blueprinty mogły go
importować bez cyklu. Zależy tylko od warstwy db i flask.session/request.
"""
from __future__ import annotations

import json as _json
import logging

from flask import session, request

from db import get_db

logger = logging.getLogger("doccompare")


def log_audit(event: str, username: str = None, detail: str = "",
              duration_ms: int = None, extra: dict = None):
    """Zapisuje zdarzenie do audit_log (event + opcjonalnie czas i dane JSON).

    Parametry:
      event       — krótki identyfikator akcji, np. 'artwork_compare'
      detail      — czytelny opis (max 500 znaków)
      duration_ms — czas wykonania w ms (opcjonalnie)
      extra       — słownik z dowolnymi danymi analitycznymi (serializowany JSON)
    """
    try:
        # detail may arrive as None or a non-str — coerce safely before slicing.
        detail = str(detail or "")[:500]
        # session / request are request-bound; in a background thread there is no
        # request context, so accessing them raises RuntimeError. Resolve them
        # defensively and fall back to safe defaults.
        try:
            _user = username or session.get("username", "?")
        except Exception:
            _user = username or "?"
        try:
            _ip = request.remote_addr or ""
        except Exception:
            _ip = ""
        db = get_db()
        try:
            db.execute(
                "INSERT INTO audit_log(event,username,detail,ip,duration_ms,extra,created_at) "
                "VALUES(?,?,?,?,?,?,datetime('now'))",
                (event,
                 _user,
                 detail,
                 _ip,
                 duration_ms,
                 _json.dumps(extra, ensure_ascii=False, default=str) if extra else None)
            )
            db.commit()
        finally:
            db.close()
    except Exception as _e:
        logger.debug("_log_audit failed (non-fatal): %s", _e)
