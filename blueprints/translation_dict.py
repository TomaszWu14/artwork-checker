"""
blueprints/translation_dict.py — słownik tłumaczeń (strona + CRUD + apply).

Tabela translation_dictionary tworzona jest przez migrate_db, więc blueprint nie
„posiada" schematu — same trasy. Importuje tylko core/db.
"""
from __future__ import annotations

import logging
import re as _re

from flask import Blueprint, render_template, request, jsonify, session

from db import get_db
from core.security import login_required, require_role, csrf_protect

bp = Blueprint("translation_dict", __name__)
logger = logging.getLogger("doccompare")


@bp.route("/translation-dictionary")
@login_required
def translation_dictionary_page():
    return render_template("translation_dictionary.html",
                           username=session["username"],
                           role=session["role"])


@bp.route("/api/translation-dictionary", methods=["GET"])
@login_required
def api_translation_dict_list():
    db = get_db()
    try:
        rows = db.execute(
            "SELECT id, term_original, lang_from, term_translated, lang_to, context, notes, created_at "
            "FROM translation_dictionary ORDER BY lang_from, term_original LIMIT 1000"
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()


@bp.route("/api/translation-dictionary", methods=["POST"])
@login_required
@require_role("manager")
@csrf_protect
def api_translation_dict_add():
    data = request.get_json(silent=True) or {}
    orig = str(data.get("term_original") or "").strip()[:500]
    trans = str(data.get("term_translated") or "").strip()[:500]
    lang_from = str(data.get("lang_from") or "en").strip().lower()[:5]
    lang_to = str(data.get("lang_to") or "pl").strip().lower()[:5]
    context = str(data.get("context") or "all").strip()[:20]
    notes = str(data.get("notes") or "").strip()[:500]
    if not orig or not trans:
        return jsonify({"error": "term_original i term_translated są wymagane"}), 400
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO translation_dictionary(term_original,lang_from,term_translated,lang_to,context,notes,created_by) "
            "VALUES(?,?,?,?,?,?,?)",
            (orig, lang_from, trans, lang_to, context, notes, session["user_id"])
        )
        new_id = cur.lastrowid
        db.commit()
        return jsonify({"ok": True, "id": new_id}), 201
    except Exception as e:
        if "UNIQUE" in str(e).upper():
            return jsonify({"error": "Termin już istnieje dla tego języka i kontekstu"}), 409
        logger.exception("Unexpected error %s", request.path)
        return jsonify({"error": "Błąd serwera"}), 500
    finally:
        db.close()


@bp.route("/api/translation-dictionary/<int:tid>", methods=["PUT"])
@require_role("manager")
@csrf_protect
def api_translation_dict_update(tid):
    data = request.get_json(silent=True) or {}
    fields, vals = [], []
    _td_max_lens = {"term_original": 500, "term_translated": 500,
                    "lang_from": 5, "lang_to": 5, "context": 20, "notes": 500}
    for k in ("term_original", "term_translated", "lang_from", "lang_to", "context", "notes"):
        if k in data:
            fields.append(f"{k}=?")
            vals.append(str(data[k] or "").strip()[:_td_max_lens.get(k, 500)])
    if not fields:
        return jsonify({"error": "Brak pól do aktualizacji"}), 400
    fields.append("updated_at=datetime('now')")
    vals.append(tid)
    db = get_db()
    try:
        # Bandit B608: nazwy kolumn z twardej białej listy w kodzie; wartości jako parametry ?.
        db.execute(f"UPDATE translation_dictionary SET {', '.join(fields)} WHERE id=?", vals)  # nosec B608
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@bp.route("/api/translation-dictionary/<int:tid>", methods=["DELETE"])
@require_role("manager")
@csrf_protect
def api_translation_dict_delete(tid):
    db = get_db()
    try:
        db.execute("DELETE FROM translation_dictionary WHERE id=?", (tid,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@bp.route("/api/translation-dictionary/apply", methods=["POST"])
@login_required
@csrf_protect
def api_translation_dict_apply():
    """Apply translation dictionary to a given text. Returns normalized text."""
    data = request.get_json(silent=True) or {}
    text = str(data.get("text") or "").strip()[:50_000]
    context = str(data.get("context") or "all")[:50]
    if not text:
        return jsonify({"text": text})
    db = get_db()
    try:
        rows = db.execute(
            "SELECT term_original, term_translated FROM translation_dictionary "
            "WHERE context=? OR context='all' ORDER BY length(term_original) DESC",
            (context,)
        ).fetchall()
    finally:
        db.close()
    if not rows:
        return jsonify({"text": text, "applied": 0})
    # Single-pass replacement via regex to avoid chained substitution
    # (where A→B followed by B→C would turn A into C unexpectedly).
    mapping = {r["term_original"]: r["term_translated"] for r in rows}
    # Drop empty/falsy keys: re.escape("") yields "" which matches at every position
    # in the alternation, corrupting the output.
    keys = [k for k in mapping if k]
    if not keys:
        return jsonify({"text": text, "applied": 0})
    pattern = _re.compile("|".join(_re.escape(k) for k in keys))
    result = pattern.sub(lambda m: mapping[m.group(0)], text)
    return jsonify({"text": result, "applied": len(keys)})
