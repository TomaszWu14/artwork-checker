"""
blueprints/palviz.py — PalViz: eksport 6 PNG/ścianka + manifest (dla GROOVE/PalViz)
oraz kreator szablonów wykrojników. Uzupełnia artwork_3d z main (który robi GLB) —
tu dokładamy płaski eksport per ścianka i szablony regionów.

Blueprint → core/db (nigdy → app). Logika w artwork_palviz.py i dieline_templates.py.
Tabelę dieline_templates „posiada" ten moduł (ensure_dieline_templates_table).
"""
from __future__ import annotations

import os
import uuid

from flask import (Blueprint, render_template, request, jsonify, session,
                   url_for, send_from_directory, abort, current_app)
from werkzeug.utils import secure_filename

from db import get_db
from core.security import login_required, require_role, csrf_protect, can_see_all
from core.audit import log_audit

bp = Blueprint("palviz", __name__)

# asset_type/poziom → poziom akceptowany przez build_bundle
_LEVELS_OK = ("box", "carton", "carton_print", "sztuka", "opz", "karton")


def ensure_dieline_templates_table(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS dieline_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, sig_key TEXT UNIQUE, page_w_mm REAL, page_h_mm REAL,
            panels_json TEXT, orient_json TEXT, created_by INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    db.commit()


def _upload_dir():
    d = current_app.config.get("UPLOAD_FOLDER", "uploads")
    os.makedirs(d, exist_ok=True)
    return d


def _template_apply(pdf_path):
    """(rects, orient) z zapisanego szablonu dopasowanego po sygnaturze, albo (None, None)."""
    try:
        from dieline_templates import signature, match_template, locate_panels_by_template
        sig = signature(pdf_path)
        db = get_db()
        ensure_dieline_templates_table(db)
        tpl = match_template(db, sig["key"])
        if tpl:
            return locate_panels_by_template(tpl, sig["page_w_mm"], sig["page_h_mm"]), (tpl.orient or None)
    except Exception:
        pass
    return None, None


def _manual_dims():
    try:
        w = float(request.form.get("w_mm") or 0)
        h = float(request.form.get("h_mm") or 0)
        d = float(request.form.get("d_mm") or 0)
        return (w, h, d) if (w and h and d) else None
    except (TypeError, ValueError):
        return None


# ─── PalViz: podgląd + eksport (pojedynczy) ───────────────────────────────────
@bp.route("/artwork/palviz")
@login_required
def palviz_page():
    return render_template("artwork_palviz_viewer.html")


@bp.route("/api/artwork/palviz", methods=["POST"])
@login_required
@csrf_protect
def api_palviz_generate():
    """Dieline PDF → podgląd GLB + eksport PalViz (6 PNG/ścianka + manifest) w ZIP."""
    import shutil
    import tempfile
    import zipfile

    file = request.files.get("file")
    level = (request.form.get("level") or "").strip()
    sku = (request.form.get("sku") or "").strip()
    if not file or not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Wymagany plik PDF"}), 400
    if level not in _LEVELS_OK:
        return jsonify({"error": f"level: {', '.join(_LEVELS_OK)}"}), 400
    if not sku:
        return jsonify({"error": "Wymagany SKU"}), 400

    up = _upload_dir()
    glb_dir = os.path.join(up, "palviz_glb"); os.makedirs(glb_dir, exist_ok=True)
    zip_dir = os.path.join(up, "palviz_zip"); os.makedirs(zip_dir, exist_ok=True)
    uid = session["user_id"]; token = uuid.uuid4().hex
    pdf_path = os.path.join(up, f"{uid}_pv_{secure_filename(file.filename)}")
    file.save(pdf_path)
    glb_name = f"{uid}_{token}.glb"; zip_name = f"{uid}_{token}.zip"
    work = tempfile.mkdtemp(prefix="palviz_")
    try:
        from artwork_palviz import build_bundle
        rects, orient = _template_apply(pdf_path)
        res = build_bundle(pdf_path, os.path.join(glb_dir, glb_name), work,
                           level=level, sku=sku, manual_dims=_manual_dims(),
                           rects=rects, orient=orient)
        if res.get("needs_manual_dims"):
            return jsonify({"needs_manual_dims": True, "warnings": res["warnings"]})
        with zipfile.ZipFile(os.path.join(zip_dir, zip_name), "w", zipfile.ZIP_DEFLATED) as zf:
            for fn in os.listdir(work):
                zf.write(os.path.join(work, fn), fn)
        log_audit("palviz_generate", detail=f"{level}/{sku} {file.filename}")
        return jsonify({
            "glb_url": url_for("palviz.palviz_glb", name=glb_name),
            "zip_url": url_for("palviz.palviz_zip", name=zip_name),
            "manifest": res["manifest"], "dims_mm": res["dims_mm"],
            "pkg_type": res["pkg_type"], "faces_written": res["faces_written"],
            "warnings": res["warnings"],
        })
    except Exception:
        # RD-1: nie wyciekaj tracebacku/str(e) do UI — log serwerowy + generyczny PL.
        current_app.logger.exception("PalViz build failed")
        return jsonify({"error": "Nie udało się wygenerować modelu 3D. Spróbuj ponownie "
                                 "lub skontaktuj się z administratorem."}), 500
    finally:
        shutil.rmtree(work, ignore_errors=True)
        try: os.remove(pdf_path)
        except OSError: pass


def _serve(subdir, name, mimetype, attach=False):
    uid = session["user_id"]; safe = secure_filename(name)
    if not safe.startswith(f"{uid}_") and not can_see_all(session["role"]):
        abort(403)
    d = os.path.abspath(os.path.join(_upload_dir(), subdir))
    return send_from_directory(d, safe, mimetype=mimetype, as_attachment=attach)


@bp.route("/artwork/palviz/glb/<name>")
@login_required
def palviz_glb(name):
    return _serve("palviz_glb", name, "model/gltf-binary")


@bp.route("/artwork/palviz/zip/<name>")
@login_required
def palviz_zip(name):
    return _serve("palviz_zip", name, "application/zip", attach=True)


# ─── PalViz: tryb wsadowy ─────────────────────────────────────────────────────
@bp.route("/artwork/palviz/batch")
@login_required
def palviz_batch_page():
    return render_template("artwork_palviz_batch.html")


@bp.route("/api/artwork/palviz/batch", methods=["POST"])
@login_required
@csrf_protect
def api_palviz_batch():
    import shutil
    import tempfile
    import zipfile

    files = request.files.getlist("files[]")
    skus = request.form.getlist("skus[]")
    level = (request.form.get("level") or "").strip()
    if not files or len(skus) != len(files):
        return jsonify({"error": "Pliki i SKU muszą się zgadzać liczbowo"}), 400
    if level not in _LEVELS_OK:
        return jsonify({"error": f"level: {', '.join(_LEVELS_OK)}"}), 400
    if any(not s.strip() for s in skus):
        return jsonify({"error": "Każdy plik wymaga SKU"}), 400

    up = _upload_dir()
    glb_dir = os.path.join(up, "palviz_glb"); os.makedirs(glb_dir, exist_ok=True)
    zip_dir = os.path.join(up, "palviz_zip"); os.makedirs(zip_dir, exist_ok=True)
    uid = session["user_id"]; batch = uuid.uuid4().hex
    work_root = tempfile.mkdtemp(prefix="palviz_batch_")
    combined = f"{uid}_batch_{batch}.zip"
    manual = _manual_dims()
    items = []
    try:
        from artwork_palviz import build_bundle
        for idx, (fs, sku) in enumerate(zip(files, skus)):
            sku = sku.strip()
            if not fs or not fs.filename.lower().endswith(".pdf"):
                items.append({"sku": sku, "error": "Niepoprawny PDF"}); continue
            pdf_path = os.path.join(up, f"{uid}_bt_{batch}_{idx}.pdf"); fs.save(pdf_path)
            faces_dir = os.path.join(work_root, secure_filename(sku) or f"item{idx}")
            glb_name = f"{uid}_{batch}_{idx}.glb"
            try:
                rects, orient = _template_apply(pdf_path)
                res = build_bundle(pdf_path, os.path.join(glb_dir, glb_name), faces_dir,
                                   level=level, sku=sku, manual_dims=manual,
                                   rects=rects, orient=orient)
                if res.get("needs_manual_dims"):
                    items.append({"sku": sku, "error": "Brak wymiarów — podaj wspólne W×H×D"})
                    continue
                items.append({"sku": sku, "glb_url": url_for("palviz.palviz_glb", name=glb_name),
                              "dims_mm": res["dims_mm"], "faces_written": res["faces_written"],
                              "warnings": res["warnings"]})
            except Exception:
                current_app.logger.exception("PalViz batch item failed (sku=%s)", sku)
                items.append({"sku": sku, "error": "Nie udało się wygenerować bryły dla tego SKU."})
            finally:
                try: os.remove(pdf_path)
                except OSError: pass
        ok = [d for d in os.listdir(work_root) if os.path.isdir(os.path.join(work_root, d))]
        if ok:
            with zipfile.ZipFile(os.path.join(zip_dir, combined), "w", zipfile.ZIP_DEFLATED) as zf:
                for sub in ok:
                    for fn in os.listdir(os.path.join(work_root, sub)):
                        zf.write(os.path.join(work_root, sub, fn), os.path.join(sub, fn))
        log_audit("palviz_batch", detail=f"{level} x{len(files)} ok={len(ok)}")
        return jsonify({"items": items,
                        "combined_zip_url": url_for("palviz.palviz_zip", name=combined) if ok else None,
                        "generated": len(ok), "total": len(files)})
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


# ─── Kreator szablonów wykrojników ────────────────────────────────────────────
@bp.route("/artwork/templates")
@login_required
def templates_page():
    return render_template("artwork_templates.html")


@bp.route("/api/artwork/templates/analyze", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_templates_analyze():
    file = request.files.get("file")
    if not file or not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Wymagany plik PDF"}), 400
    up = _upload_dir()
    prev_dir = os.path.join(up, "tpl_preview"); os.makedirs(prev_dir, exist_ok=True)
    uid = session["user_id"]
    pdf_path = os.path.join(up, f"{uid}_tpl_{secure_filename(file.filename)}")
    file.save(pdf_path)
    png_name = f"{uid}_{uuid.uuid4().hex}.png"
    try:
        import fitz
        from dieline_templates import signature, match_template
        doc = fitz.open(pdf_path)
        page = max(doc, key=lambda p: p.rect.width * p.rect.height)
        pix = page.get_pixmap(matrix=fitz.Matrix(110 / 72, 110 / 72), alpha=False)
        pix.save(os.path.join(prev_dir, png_name))
        img_w, img_h = pix.width, pix.height
        doc.close()
        sig = signature(pdf_path)
        db = get_db(); ensure_dieline_templates_table(db)
        existing = match_template(db, sig["key"])
        return jsonify({
            "image_url": url_for("palviz.templates_preview", name=png_name),
            "image_w": img_w, "image_h": img_h,
            "page_w_mm": sig["page_w_mm"], "page_h_mm": sig["page_h_mm"],
            "sig_key": sig["key"], "vcuts": sig["vcuts"], "hcuts": sig["hcuts"],
            "existing_name": existing.name if existing else None,
            "existing_panels": existing.panels if existing else None,
            "existing_orient": (existing.orient if existing else None) or {},
        })
    except Exception:
        # RD-1: nie wyciekaj tracebacku/str(e) do UI — log serwerowy + generyczny PL.
        current_app.logger.exception("PalViz build failed")
        return jsonify({"error": "Nie udało się wygenerować modelu 3D. Spróbuj ponownie "
                                 "lub skontaktuj się z administratorem."}), 500
    finally:
        try: os.remove(pdf_path)
        except OSError: pass


@bp.route("/artwork/templates/preview/<name>")
@login_required
def templates_preview(name):
    d = os.path.abspath(os.path.join(_upload_dir(), "tpl_preview"))
    return send_from_directory(d, secure_filename(name), mimetype="image/png")


@bp.route("/api/artwork/templates/save", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_templates_save():
    d = request.get_json(silent=True) or {}
    sig_key = (d.get("sig_key") or "").strip()
    regions = d.get("regions") or {}
    if not sig_key or not regions:
        return jsonify({"error": "Brak sygnatury lub regionów"}), 400
    from dieline_templates import DielineTemplate, save_template
    tpl = DielineTemplate(
        name=(d.get("name") or "szablon").strip(), sig_key=sig_key,
        page_w_mm=float(d.get("page_w_mm") or 0), page_h_mm=float(d.get("page_h_mm") or 0),
        panels={f: tuple(v) for f, v in regions.items()}, orient=d.get("orient") or {})
    db = get_db(); ensure_dieline_templates_table(db)
    save_template(db, tpl, created_by=session["user_id"])
    log_audit("template_save", detail=f"{tpl.name} {sig_key[:30]} faces={len(regions)}")
    return jsonify({"ok": True, "sig_key": sig_key, "faces": list(regions)})
