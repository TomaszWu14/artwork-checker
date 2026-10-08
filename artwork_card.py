"""artwork_card.py — agregat „karta produktu artworkowego" per REF (read-only).

Krok 1 rolloutu (patrz docs/superpowers/specs/2026-09-03-karta-charakterystyki-artworku-design.md §8b).
Składa kartę z ISTNIEJĄCYCH źródeł — nie tworzy nowej tabeli:

  - dane wspólne + poziomy:  material_master (kolumny płaskie + levels_json)
  - pliki/rewizje per poziom: artwork_index (lookup_best, all_revisions)

Zwraca zwykły dict, gotowy dla szablonu artwork_card.html. Rekord jest per REF,
ale `levels` to lista arkuszy per poziom (§1a — jeden arkusz = jeden komplet danych,
pod OCR). Brak ciężkich importów — moduł ładuje się w CI bez ML.
"""
import json

import material_master as _mm
import artwork_index as _ai

# Kanoniczna kolejność poziomów (jak material_master.LEVELS); nieznane doklejamy na końcu.
_LEVEL_ORDER = ("sztuka", "op", "opz", "karton")


def normalize_ref(raw) -> str:
    """Reuse normalizacji REF z master daty (spójne dopasowanie ref_norm)."""
    return _mm.normalize_ref(raw)


def _levels_for(db, ref_code, levels_data: dict, revisions: list) -> list:
    """Lista arkuszy per poziom. Scal poziomy z material_master (wymiar/ean/qty)
    z najlepszym plikiem z artwork_index dla danego poziomu."""
    keys = [k for k in _LEVEL_ORDER if k in levels_data]
    keys += [k for k in levels_data if k not in _LEVEL_ORDER]
    # Gdy master nie zna poziomów, wyprowadź je z typów opakowań widocznych w plikach.
    if not keys:
        seen = []
        for r in revisions:
            pt = (r.get("packaging_type") or "").strip()
            if pt and pt not in seen:
                seen.append(pt)
        keys = seen

    out = []
    for key in keys:
        d = levels_data.get(key) or {}
        ean = str(d.get("ean") or "").strip()
        best = None
        try:
            best = _ai.lookup_best(db, ref_code, key)
        except Exception:
            best = None
        # Fallback EAN z pliku, gdy master nie ma kodu dla poziomu.
        if not ean and best:
            ean = str(best.get("ean") or "").strip()
        out.append({
            "level": key,
            "ean": ean or "NO-EAN",
            "dims": str(d.get("wymiar") or "").strip(),
            "qty_base": str(d.get("qty_base") or "").strip(),
            "artwork_ref": str(d.get("artwork_ref") or "").strip(),
            "file": best,   # dict artwork_index lub None (brak pliku dla poziomu)
        })
    return out


def build_card(db, ref) -> dict | None:
    """Złóż kartę produktu dla REF. None gdy REF nieznany (brak master daty i plików)."""
    ref = str(ref or "").strip()
    if not ref:
        return None

    material = _mm.get_material(db, ref)          # ensure_table w środku
    revisions = _ai.all_revisions(db, ref)        # ensure_table w środku; sort rank DESC
    if not material and not revisions:
        return None

    ref_code = ref
    common = {}
    levels_data = {}
    if material:
        ref_code = material.get("ref_code") or ref
        common = {
            "opis_pl": material.get("opis_pl") or "",
            "opis_en": material.get("opis_en") or "",
            "rodzina": material.get("rodzina") or "",
            "grupa": material.get("grupa") or "",
            "podgrupa": material.get("podgrupa") or "",
            "producer_code": material.get("producer_code") or "",
            "supplier_codes": material.get("supplier_codes") or "",
            "txt_short_pl": material.get("txt_short_pl") or "",
            "ean": material.get("ean") or "",
        }
        try:
            levels_data = json.loads(material.get("levels_json") or "{}") or {}
        except (ValueError, TypeError):
            levels_data = {}

    current_rev = revisions[0].get("revision", "") if revisions else ""

    return {
        "ref": ref_code,
        "common": common,
        "levels": _levels_for(db, ref_code, levels_data, revisions),
        "revisions": revisions,
        "current_rev": current_rev,
    }
