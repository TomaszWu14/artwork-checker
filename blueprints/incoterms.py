"""
blueprints/incoterms.py — słownik Incoterms (strona + CRUD).

Pierwszy blueprint wydzielony z app.py jako wzorzec. Importuje wyłącznie core/db
(blueprint → core, nigdy → app). Tabelę incoterms_dictionary „posiada" ten moduł;
app.py (_load_incoterms_dict, do wzbogacania porównań) importuje stąd
ensure_incoterms_table.
"""
from __future__ import annotations

import re

from flask import Blueprint, render_template, request, jsonify, session

from db import get_db, IntegrityError
from core.security import login_required, require_role, csrf_protect

bp = Blueprint("incoterms", __name__)

_SEED = [
    ("CFR", "Koszty i fracht"),
    ("CIF", "Koszty, ubezpiecz. i fracht"),
    ("CIP", "Ubezpiecz., wolne od opł. przew."),
    ("CPT", "Franco fracht"),
    ("DAF", "Dostawa do granicy"),
    ("DAP", "Dostarczony do miejsca"),
    ("DAT", "Dostarczony do terminalu"),
    ("DDP", "Dostarczony cło opłacone"),
    ("DDU", "Dostarczony cło nieopłacone"),
    ("DEQ", "Franco nabrzeżne (oclone)"),
    ("DES", "Dostawa ze statku"),
    ("EXW", "Z zakładu"),
    ("FAS", "Franco wzdłuż burty statku"),
    ("FCA", "Dostarczony do przewoźnika"),
    ("FH",  "Przewóz opłacony"),
    ("FOB", "Dostarczony na statek"),
    ("UN",  "Pełna opłata przewozowa"),
]
_initialized = False


def ensure_incoterms_table(db):
    """Tworzy i zasila tabelę incoterms_dictionary (z dostarczonego Excela)."""
    global _initialized
    if _initialized:
        return
    db.execute(
        "CREATE TABLE IF NOT EXISTS incoterms_dictionary ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " code TEXT UNIQUE NOT NULL,"
        " description_pl TEXT DEFAULT '',"
        " description_en TEXT DEFAULT '',"
        " updated_at TEXT DEFAULT (datetime('now')))"
    )
    if db.execute("SELECT COUNT(*) FROM incoterms_dictionary").fetchone()[0] == 0:
        for code, pl in _SEED:
            db.execute("INSERT OR IGNORE INTO incoterms_dictionary(code, description_pl) VALUES(?,?)",
                       (code, pl))
    db.commit()
    _initialized = True


@bp.route("/incoterms")
@login_required
def incoterms_page():
    return render_template("incoterms_dictionary.html",
                           username=session["username"], role=session["role"])


@bp.route("/api/incoterms", methods=["GET"])
@login_required
def api_incoterms_list():
    db = get_db()
    try:
        ensure_incoterms_table(db)
        rows = db.execute(
            "SELECT id, code, description_pl, description_en FROM incoterms_dictionary "
            "ORDER BY code"
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()


@bp.route("/api/incoterms", methods=["POST"])
@login_required
@require_role("manager")
@csrf_protect
def api_incoterms_add():
    data = request.get_json(silent=True) or {}
    code = re.sub(r'[^A-Za-z0-9]', '', str(data.get("code") or "")).upper()[:10]
    if not code:
        return jsonify({"error": "Kod incoterm jest wymagany"}), 400
    pl = str(data.get("description_pl") or "").strip()[:200]
    en = str(data.get("description_en") or "").strip()[:200]
    db = get_db()
    try:
        ensure_incoterms_table(db)
        try:
            db.execute("INSERT INTO incoterms_dictionary(code, description_pl, description_en) "
                       "VALUES(?,?,?)", (code, pl, en))
            db.commit()
        except IntegrityError:
            return jsonify({"error": f"Kod {code} już istnieje"}), 409
        row = db.execute("SELECT id FROM incoterms_dictionary WHERE code=?", (code,)).fetchone()
        return jsonify({"ok": True, "id": row["id"] if row else None}), 201
    finally:
        db.close()


@bp.route("/api/incoterms/<int:iid>", methods=["PUT"])
@login_required
@require_role("manager")
@csrf_protect
def api_incoterms_update(iid):
    data = request.get_json(silent=True) or {}
    db = get_db()
    try:
        ensure_incoterms_table(db)
        db.execute(
            "UPDATE incoterms_dictionary SET description_pl=?, description_en=?, "
            "updated_at=datetime('now') WHERE id=?",
            (str(data.get("description_pl") or "").strip()[:200],
             str(data.get("description_en") or "").strip()[:200], iid))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@bp.route("/api/incoterms/<int:iid>", methods=["DELETE"])
@login_required
@require_role("manager")
@csrf_protect
def api_incoterms_delete(iid):
    db = get_db()
    try:
        ensure_incoterms_table(db)
        db.execute("DELETE FROM incoterms_dictionary WHERE id=?", (iid,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()
