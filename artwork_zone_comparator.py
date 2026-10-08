"""
artwork_zone_comparator.py — porównanie manualnie zaznaczonych stref artworku.

Przepływ:
  1. Użytkownik zaznacza pary stref (zone_a ↔ zone_b) w UI
  2. compare_zone_pairs() wycina regiony, uruchamia OCR + pixel diff
  3. Zwraca listę wyników gotowych do wyświetlenia w raporcie
"""

import base64, difflib, io, json

try:
    from PIL import Image
    import numpy as np
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


# ── Pomocnicze ─────────────────────────────────────────────────────────────────

def _b64_to_img(b64: str):
    if not HAS_PIL or not b64:
        return None
    try:
        with Image.open(io.BytesIO(base64.b64decode(b64))) as _im:
            return _im.convert("RGB")   # convert() zwraca nowy obraz; oryginał zamknięty
    except Exception:
        return None


def _img_to_b64(img, max_width: int = 500, quality: int = 88) -> str:
    if not HAS_PIL or img is None:
        return ""
    w, h = img.size
    if w > max_width:
        img = img.resize((max_width, int(h * max_width / w)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _crop(img, zone: dict):
    """Wycina strefę z obrazu wg procentowych współrzędnych {x_pct,y_pct,w_pct,h_pct}."""
    if img is None or not zone:
        return None
    W, H = img.size
    x1 = max(0, int(zone.get("x_pct", 0)  / 100 * W))
    y1 = max(0, int(zone.get("y_pct", 0)  / 100 * H))
    x2 = min(W, int((zone.get("x_pct", 0) + zone.get("w_pct", 100)) / 100 * W))
    y2 = min(H, int((zone.get("y_pct", 0) + zone.get("h_pct", 100)) / 100 * H))
    if x2 <= x1 or y2 <= y1:
        return None
    return img.crop((x1, y1, x2, y2))


def _ocr(crop) -> str:
    """OCR na wyciętym regionie — Tesseract z polskim+angielskim."""
    if crop is None:
        return ""
    try:
        import pytesseract
        w, h = crop.size
        if w == 0 or h == 0:   # BUGFIX: pusty crop → resize(0,0) wywala PIL
            return ""
        scale = max(1, 1800 // max(w, h, 1))
        if scale > 1:
            crop = crop.resize((w * scale, h * scale), Image.LANCZOS)
        return pytesseract.image_to_string(crop, lang="pol+eng", config="--psm 3").strip()
    except Exception:
        return ""


def _pixel_similarity(crop_a, crop_b):
    """Pixel similarity 0–100% po wyrównaniu rozmiaru.

    Zwraca None przy braku danych/błędzie — caller NIE może traktować braku
    pomiaru jak realnej różnicy wizualnej (0.0 < 92 dawało fałszywą niezgodność).
    """
    if not HAS_PIL or crop_a is None or crop_b is None:
        return None
    try:
        W = max(crop_a.width, crop_b.width)
        H = max(crop_a.height, crop_b.height)
        a = np.array(crop_a.resize((W, H), Image.LANCZOS).convert("L"), dtype=float)
        b = np.array(crop_b.resize((W, H), Image.LANCZOS).convert("L"), dtype=float)
        return round((1.0 - np.abs(a - b).mean() / 255.0) * 100, 1)
    except Exception:
        return None


def _text_diff(text_a: str, text_b: str) -> list:
    """Porównuje dwa teksty OCR linijka po linijce."""
    la = [l.strip() for l in text_a.splitlines() if l.strip()]
    lb = [l.strip() for l in text_b.splitlines() if l.strip()]
    rows = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, la, lb).get_opcodes():
        if op == "equal":
            for k in range(i2 - i1):
                rows.append({"a": la[i1+k], "b": lb[j1+k], "status": "equal"})
        else:
            for k in range(max(i2 - i1, j2 - j1)):
                a = la[i1+k] if (i1+k) < i2 else ""
                b = lb[j1+k] if (j1+k) < j2 else ""
                st = "changed" if a and b else ("only_a" if a else "only_b")
                rows.append({"a": a, "b": b, "status": st})
    return rows


# ── Główna funkcja ─────────────────────────────────────────────────────────────

def compare_zone_pairs(img_a_b64: str, img_b_b64: str, zones: list) -> list:
    """
    Porównuje manualnie zaznaczone pary stref.

    zones: list of {
        name: str,
        type: 'text' | 'graphic' | 'both',
        zone_a: {x_pct, y_pct, w_pct, h_pct},
        zone_b: {x_pct, y_pct, w_pct, h_pct},
    }

    Zwraca list of {
        name, type,
        crop_a_b64, crop_b_b64,
        text_diffs, pixel_similarity,
        status, status_class,
    }
    """
    img_a = _b64_to_img(img_a_b64)
    img_b = _b64_to_img(img_b_b64)

    results = []
    for zone in zones:
        za    = zone.get("zone_a") or {}
        zb    = zone.get("zone_b") or {}
        ztype = zone.get("type", "both")
        name  = zone.get("name", "Region")

        crop_a = _crop(img_a, za)
        crop_b = _crop(img_b, zb)

        # OCR
        text_a = _ocr(crop_a) if ztype in ("text", "both") else ""
        text_b = _ocr(crop_b) if ztype in ("text", "both") else ""
        diffs  = _text_diff(text_a, text_b) if (text_a or text_b) else []

        # Pixel similarity
        sim = _pixel_similarity(crop_a, crop_b) if ztype in ("graphic", "both") else None

        # Status
        has_text_diff = any(d["status"] != "equal" for d in diffs)
        if sim is not None and sim < 92:
            status, css = "Roznica wizualna", "status-niezgodnosc"
        elif has_text_diff:
            status, css = "Roznica tekstowa", "status-uwaga"
        else:
            status, css = "Zgodny", "status-ok"

        results.append({
            "name":             name,
            "type":             ztype,
            "crop_a_b64":       _img_to_b64(crop_a),
            "crop_b_b64":       _img_to_b64(crop_b),
            "text_a":           text_a,
            "text_b":           text_b,
            "text_diffs":       diffs,
            "pixel_similarity": sim,
            "status":           status,
            "status_class":     css,
        })

    return results


# ── Szablony stref ─────────────────────────────────────────────────────────────

def save_zone_template(name: str, zones: list, file_a: str = "", file_b: str = "") -> str:
    """Zapisuje szablon stref do bazy settings. Zwraca klucz."""
    import time
    from db import get_db
    db  = get_db()
    key = f"ztpl_{int(time.time() * 1000)}"
    try:
        db.execute(
            "INSERT INTO settings(category,key,value) VALUES(?,?,?)",
            ("artwork_zones", key, json.dumps({
                "name": name, "zones": zones,
                "file_a": file_a, "file_b": file_b,
            }, ensure_ascii=False))
        )
        db.commit()
    finally:
        db.close()
    return key


def list_zone_templates() -> list:
    """Zwraca listę zapisanych szablonów stref."""
    try:
        from db import get_db
        db   = get_db()
        try:
            rows = db.execute(
                "SELECT key,value FROM settings WHERE category='artwork_zones' ORDER BY id DESC"
            ).fetchall()
        finally:
            db.close()
        result = []
        for row in rows:
            try:
                t = json.loads(row["value"])
                t["id"] = row["key"]
                result.append(t)
            except Exception:
                continue
        return result
    except Exception:
        return []


def delete_zone_template(template_id: str) -> bool:
    try:
        from db import get_db
        db = get_db()
        try:
            db.execute("DELETE FROM settings WHERE category='artwork_zones' AND key=?", (template_id,))
            db.commit()
        finally:
            db.close()
        return True
    except Exception:
        return False
