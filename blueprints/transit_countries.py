"""
blueprints/transit_countries.py — master data „Państwa tranzytowe".

Prosta lista krajów tranzytu (dodawanie/usuwanie) używana przy zakładaniu
dostawy: przeznaczenie = Magazyn Radom albo Tranzyt – <kraj>. Importuje tylko
core/db (blueprint → core, nigdy → app). Tabelę transit_countries „posiada" ten
moduł; app.py (formularz nowej dostawy) importuje stąd list_countries.
"""
from __future__ import annotations

from flask import Blueprint, render_template, request, jsonify, session

from db import get_db, IntegrityError
from core.security import login_required, require_role, csrf_protect
from core.audit import log_audit

bp = Blueprint("transit_countries", __name__)

_SEED = ["Iberia", "Zjednoczone Emiraty Arabskie"]
_initialized = False


def ensure_table(db):
    """Tworzy i zasila tabelę transit_countries (idempotentnie)."""
    global _initialized
    if _initialized:
        return
    db.execute(
        "CREATE TABLE IF NOT EXISTS transit_countries ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " name TEXT UNIQUE NOT NULL,"
        " active INTEGER DEFAULT 1,"
        " created_at TEXT DEFAULT (datetime('now')))"
    )
    if db.execute("SELECT COUNT(*) FROM transit_countries").fetchone()[0] == 0:
        for name in _SEED:
            db.execute("INSERT OR IGNORE INTO transit_countries(name) VALUES(?)", (name,))
    db.commit()
    _initialized = True


def list_countries(db, active_only: bool = True) -> list:
    """Lista krajów tranzytu (dict-y) — używana też przez formularz nowej dostawy."""
    ensure_table(db)
    sql = "SELECT id, name, active FROM transit_countries"
    if active_only:
        sql += " WHERE COALESCE(active,1)=1"
    sql += " ORDER BY name"
    return [dict(r) for r in db.execute(sql).fetchall()]


@bp.route("/transit-countries")
@login_required
def transit_countries_page():
    return render_template("transit_countries.html",
                           username=session["username"], role=session["role"])


@bp.route("/api/transit-countries", methods=["GET"])
@login_required
def api_transit_list():
    db = get_db()
    try:
        return jsonify(list_countries(db, active_only=False))
    finally:
        db.close()


@bp.route("/api/transit-countries", methods=["POST"])
@login_required
@require_role("manager")
@csrf_protect
def api_transit_add():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()[:120]
    if not name:
        return jsonify({"error": "Podaj nazwę państwa"}), 400
    db = get_db()
    try:
        ensure_table(db)
        try:
            cur = db.execute("INSERT INTO transit_countries(name) VALUES(?)", (name,))
            db.commit()
        except IntegrityError:
            return jsonify({"error": f"„{name}” już jest na liście"}), 409
        log_audit("transit_country_add", session.get("username"), name)
        return jsonify({"ok": True, "id": cur.lastrowid, "name": name}), 201
    finally:
        db.close()


@bp.route("/api/transit-countries/<int:cid>", methods=["DELETE"])
@require_role("manager")
@csrf_protect
def api_transit_delete(cid):
    db = get_db()
    try:
        ensure_table(db)
        db.execute("DELETE FROM transit_countries WHERE id=?", (cid,))
        db.commit()
        log_audit("transit_country_delete", session.get("username"), str(cid))
        return jsonify({"ok": True})
    finally:
        db.close()
