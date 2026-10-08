"""
core/security.py — współdzielone elementy bezpieczeństwa: role, dekoratory
(login_required / require_role / csrf_protect), sesja i CSRF.

Wydzielone z app.py, by blueprinty mogły importować dekoratory bez cyklu importów
(blueprint → core, NIGDY core → app). Operują wyłącznie na flask.session / request
oraz na warstwie db — żadnej zależności od obiektu `app`.
"""
from __future__ import annotations

import logging
import secrets
from functools import wraps

from flask import session, request, redirect, url_for, jsonify, render_template

from db import get_db
from constants import ROLE_LEVEL

logger = logging.getLogger("doccompare")

import sqlite3 as _sqlite3

# Errors that indicate a transient DB outage (DB down, pool exhausted, connection
# dropped). On these we fail OPEN so a momentary outage does not log everyone out.
# Any OTHER exception (e.g. a programming/SQL error) is treated as fail-CLOSED.
_DB_OUTAGE_ERRORS: tuple = (_sqlite3.OperationalError, _sqlite3.InterfaceError)
try:  # psycopg2 is only present in production / when installed
    import psycopg2 as _psycopg2
    _DB_OUTAGE_ERRORS = _DB_OUTAGE_ERRORS + (
        _psycopg2.OperationalError, _psycopg2.InterfaceError)
except Exception:
    pass


# ── Role ──────────────────────────────────────────────────────────────────────
def role_level(role: str) -> int:
    return ROLE_LEVEL.get(role, 0)


def can_see_all(role: str) -> bool:
    return role_level(role) >= role_level("superuser")


def can_delete(role: str) -> bool:
    return role_level(role) >= role_level("superuser")


def is_admin(role: str) -> bool:
    return role == "admin"


# ── Sesja ─────────────────────────────────────────────────────────────────────
def check_session_active() -> bool:
    """Return False if the session user is deactivated or deleted in the DB."""
    uid = session.get("user_id")
    if uid is None:
        return False
    db = None
    try:
        db = get_db()
        row = db.execute(
            "SELECT COALESCE(is_active,1) FROM users WHERE id=?", (uid,)
        ).fetchone()
        return row is not None and row[0] == 1
    except _DB_OUTAGE_ERRORS as _e:
        # Transient DB outage — fail-open to avoid locking out all users.
        logger.warning("check_session_active: DB outage, failing open: %s", _e)
        return True
    except Exception as _e:
        # Unexpected error (e.g. programming/SQL error) — fail-closed for safety.
        logger.warning("check_session_active: unexpected error, failing closed: %s", _e)
        return False
    finally:
        if db is not None:
            try:
                db.close()   # zwolnij połączenie z puli na każdym żądaniu (login_required)
            except Exception:
                pass


# ── Dekoratory dostępu ────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if not check_session_active():
            session.clear()
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def require_role(min_role: str):
    """Dekorator: wymaga minimum danej roli."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))
            if not check_session_active():
                session.clear()
                return redirect(url_for("login"))
            if role_level(session.get("role", "user")) < role_level(min_role):
                return jsonify({"error": "Brak uprawnień"}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator


# ── CSRF ──────────────────────────────────────────────────────────────────────
def get_csrf_token() -> str:
    """Return (and lazily create) a per-session CSRF token."""
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_hex(32)
    return session["_csrf_token"]


def verify_csrf() -> bool:
    """Return True when the submitted CSRF token matches the session token.

    Porównanie stałoczasowe. Token kodujemy do bajtów: hmac.compare_digest na
    stringach rzuca TypeError dla znaków spoza ASCII, więc spreparowany token
    (np. z polskimi znakami) crashował widok 500 zamiast czystego 403.
    """
    import hmac as _hmac_mod
    tok = request.form.get("_csrf_token") or request.headers.get("X-CSRF-Token", "")
    expected = session.get("_csrf_token", "")
    if not tok or not expected:
        return False
    try:
        return _hmac_mod.compare_digest(
            tok.encode("utf-8", "ignore"), expected.encode("utf-8", "ignore"))
    except (TypeError, AttributeError):
        return False


def csrf_protect(f):
    """Decorator: validates CSRF token on POST/PUT/DELETE/PATCH requests."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            if not verify_csrf():
                logger.warning("CSRF validation failed: %s %s from %s",
                               request.method, request.path, request.remote_addr)
                if request.is_json or request.headers.get("X-CSRF-Token") is not None:
                    return jsonify({"error": "Błąd bezpieczeństwa — nieprawidłowy token CSRF"}), 403
                return render_template("login.html",
                                       error="Błąd bezpieczeństwa — odśwież stronę i spróbuj ponownie."), 403
        return f(*args, **kwargs)
    return decorated
