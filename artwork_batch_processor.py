"""
artwork_batch_processor.py — batch porównanie artworków (do 50 par).

auto_pair_artwork_files(filenames)  → lista par [{file_a, file_b, prefix}]
run_artwork_batch(pairs, ...)       → generator wyników
build_artwork_batch_zip(results)    → bytes ZIP ze wszystkimi raportami Word
"""

import os
import re
import time
import json
from typing import Generator, Optional

# Słowa kluczowe identyfikujące Master vs Supplier
_MASTER_KEYS   = {"master", "wzorzec", "acme", "original"}
_SUPPLIER_KEYS = {"supplier", "dostawca", "factory", "od_dostawcy", "fabryczny"}


def _strip_role(name: str) -> str:
    """Usuwa suffix roli z nazwy pliku (bez rozszerzenia)."""
    n = os.path.splitext(name)[0]
    n = re.sub(
        r'[_\-\s]*(master|wzorzec|acme|supplier|dostawca|factory|od_dostawcy|'
        r'fabryczny|original|ref|_a|_b)$',
        '', n, flags=re.IGNORECASE
    )
    return n.strip('_- ')


def _role(name: str) -> str:
    """Zwraca 'master' lub 'supplier' na podstawie nazwy."""
    n = re.sub(r'\.[a-z]{2,4}$', '', name.lower())
    for k in _MASTER_KEYS:
        if k in n:
            return "master"
    for k in _SUPPLIER_KEYS:
        if k in n:
            return "supplier"
    return "unknown"


def auto_pair_artwork_files(filenames: list) -> list:
    """
    Paruje pliki po wspólnym prefiksie.
    A-02_master.pdf ↔ A-02_supplier.pdf  →  prefix='A-02'
    Zwraca [{file_a (master), file_b (supplier), prefix, confidence}]
    """
    pdfs = [f for f in filenames if f.lower().endswith(".pdf")]
    masters   = {f: _strip_role(f) for f in pdfs if _role(f) == "master"}
    suppliers = {f: _strip_role(f) for f in pdfs if _role(f) == "supplier"}

    pairs = []
    used_s = set()

    for mf, mp in masters.items():
        best, best_score = None, 0
        for sf, sp in suppliers.items():
            if sf in used_s:
                continue
            # Similarity of stripped prefixes
            score = _prefix_score(mp, sp)
            if score > best_score:
                best, best_score = sf, score
        if best and best_score > 0.5:
            pairs.append({
                "file_a": mf,
                "file_b": best,
                "prefix": mp,
                "confidence": round(best_score, 2),
            })
            used_s.add(best)

    # Fallback: jeśli brak roli — paruj po PODOBIEŃSTWIE nazwy (greedy), a nie po
    # sąsiednim indeksie (to parowało przypadkowe, niezwiązane pliki).
    unmatched = [f for f in pdfs if _role(f) == "unknown"]
    used_u = set()
    for a in unmatched:
        if a in used_u:
            continue
        ap = _strip_role(a)
        best, best_score = None, 0
        for b in unmatched:
            if b == a or b in used_u:
                continue
            score = _prefix_score(ap, _strip_role(b))
            if score > best_score:
                best, best_score = b, score
        if best is not None:
            used_u.add(a); used_u.add(best)
            pairs.append({
                "file_a": a,
                "file_b": best,
                "prefix": ap,
                "confidence": round(max(best_score, 0.5), 2),
            })
    # Nieparzysta liczba plików bez roli → ostatni zostaje bez pary. Zgłoś to w
    # logu, zamiast po cichu pomijać (operator dostaje mniej porównań niż wgrał).
    if len(unmatched) % 2 == 1:
        try:
            import logging
            logging.getLogger("artwork_batch").warning(
                "auto_pair: nieparzysta liczba plików bez roli — bez pary został: %s",
                unmatched[-1],
            )
        except Exception:
            pass

    return pairs


def _prefix_score(a: str, b: str) -> float:
    ta = set(re.split(r'[_\-\s\.]+', a.lower())) - {""}
    tb = set(re.split(r'[_\-\s\.]+', b.lower())) - {""}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


def _secure_uploaded_name(filename: str) -> str:
    try:
        from werkzeug.utils import secure_filename
        return secure_filename(filename) or "file"
    except Exception:
        return os.path.basename(filename) or "file"


def _uploaded_artwork_path(upload_dir: str, uid: int, side: str, filename: str) -> str:
    raw_name = os.path.basename(filename or "")
    safe_name = _secure_uploaded_name(raw_name)
    candidates = [safe_name]
    if raw_name and raw_name != safe_name:
        candidates.append(raw_name)
    for name in candidates:
        path = os.path.join(upload_dir, f"{uid}_aw_{side}_{name}")
        if os.path.exists(path):
            return path
    return os.path.join(upload_dir, f"{uid}_aw_{side}_{safe_name}")


def _preextract_sequential_pages(pairs: list, upload_dir: str, uid: int) -> dict:
    """
    For sequential pairs (page_b is not None), extract each required page from the
    multi-page supplier PDF into a separate single-page temp file.
    Returns {(file_b, page_b): tmp_path} mapping. Caller must clean up tmp files.
    """
    from artwork_comparator import extract_pdf_page

    cache = {}  # (file_b, page_b) -> extracted tmp path
    for pair in pairs:
        pb = pair.get("page_b")
        if pb is None:
            continue
        fb = pair["file_b"]
        key = (fb, pb)
        if key in cache:
            continue
        src = _uploaded_artwork_path(upload_dir, uid, "b", fb)
        if not os.path.exists(src):
            continue
        safe_name = re.sub(r'[^\w]', '_', fb)
        tmp = os.path.join(upload_dir, f"_seq_{uid}_{safe_name}_p{pb}.pdf")
        try:
            extract_pdf_page(src, pb, tmp)
            cache[key] = tmp
        except Exception:
            pass  # fall back to page_b parameter in compare_artworks
    return cache


def run_artwork_batch(
    pairs: list,
    upload_dir: str,
    uid: int,
    use_ai: bool = True,
    progress_cb=None,
) -> Generator:
    """
    Przetwarza pary artworków jeden po drugim.
    Yields dict z wynikiem każdej pary.
    progress_cb(i, total, status) — opcjonalny callback postępu.
    """
    from artwork_comparator import compare_artworks
    from artwork_report_engine import build_report_from_comparison, export_to_docx

    total = len(pairs)

    # Pre-extract single pages for sequential mode — gives true 1:1 comparison
    # (compare_artworks gets isolated single-page PDFs instead of page_b offsets)
    _page_cache = _preextract_sequential_pages(pairs, upload_dir, uid)
    _page_tmp_files = list(_page_cache.values())

    try:
        for i, pair in enumerate(pairs):
            result = {
                "index":    i,
                "total":    total,
                "file_a":   pair["file_a"],
                "file_b":   pair["file_b"],
                "prefix":   pair.get("prefix", ""),
                "status":   "processing",
                "error":    None,
                "critical": 0,
                "warnings": 0,
                "ok_count": 0,
                "risk":     "ok",
                "cache_id": None,
                "report_obj": None,
                "docx_bytes": None,
                "elapsed_ms": 0,
                "group_id": pair.get("group_id"),
                "seq_idx":  pair.get("seq_idx"),
                "page_b":   pair.get("page_b"),
            }

            if progress_cb:
                progress_cb(i, total, "processing")

            t0 = time.time()
            path_a = _uploaded_artwork_path(upload_dir, uid, "a", pair["file_a"])
            path_b = _uploaded_artwork_path(upload_dir, uid, "b", pair["file_b"])

            for path, label in [(path_a, "A"), (path_b, "B")]:
                if not os.path.exists(path):
                    result["status"] = "error"
                    result["error"] = f"Brak pliku {label}: {os.path.basename(path)}"
                    break

            if result["status"] == "error":
                yield result
                continue

            # Use pre-extracted single-page file if available (sequential split mode)
            pb = pair.get("page_b")
            extracted = _page_cache.get((pair["file_b"], pb)) if pb is not None else None
            actual_path_b = extracted if extracted else path_b
            page_b_arg    = None if extracted else pb

            try:
                cmp = compare_artworks(path_a, actual_path_b, use_ai=use_ai,
                                       use_ai_sections=use_ai, max_pages=6,
                                       page_a=pair.get("page_a"),
                                       page_b=page_b_arg)
                report = build_report_from_comparison(cmp)

                result["critical"]   = cmp.critical_count
                result["warnings"]   = cmp.important_count
                result["ok_count"]   = cmp.ok_count
                result["risk"]       = (
                    "critical" if cmp.critical_count > 0 else
                    "warning"  if cmp.important_count > 0 else "ok"
                )
                result["status"]     = "done"
                result["report_obj"] = report
                result["cmp_data"]   = cmp.to_dict(include_images=False)
                try:
                    result["docx_bytes"] = export_to_docx(report)
                except Exception:
                    result["docx_bytes"] = None

                # Barcode deep check on supplier file
                try:
                    from barcode_validator import deep_validate_printed_data
                    _pd0 = cmp.page_diffs[0] if cmp.page_diffs else None
                    result["barcode_check"] = deep_validate_printed_data(
                        getattr(_pd0, "text_b", None) or "",
                        getattr(_pd0, "img_b_b64", None),
                    )
                except Exception as e:
                    result["barcode_check"] = {"error": str(e)[:200]}

            except Exception as e:
                import traceback as _tb
                result["status"] = "error"
                result["error"]  = str(e)[:200]
                result["traceback"] = _tb.format_exc()[-1000:]

            result["elapsed_ms"] = int((time.time() - t0) * 1000)
            if progress_cb:
                progress_cb(i + 1, total, result["status"])
            yield result
    finally:
        for tmp in _page_tmp_files:
            try:
                os.remove(tmp)
            except Exception:
                pass


def build_artwork_batch_zip(results: list) -> bytes:
    """Pakuje raporty Word wszystkich par do ZIP. Zwraca bytes."""
    import zipfile
    from io import BytesIO

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Summary JSON
        summary = []
        for r in results:
            summary.append({
                "file_a":   r.get("file_a"),
                "file_b":   r.get("file_b"),
                "status":   r.get("status"),
                "critical": r.get("critical", 0),
                "warnings": r.get("warnings", 0),
                "risk":     r.get("risk", "ok"),
                "error":    r.get("error"),
            })
        zf.writestr("_podsumowanie.json", json.dumps(summary, ensure_ascii=False, indent=2))

        for idx, r in enumerate(results, 1):
            docx = r.get("docx_bytes")
            if not docx:
                continue
            prefix = r.get("prefix") or r.get("file_a", "raport")
            # Prefiks indeksu — inaczej raporty o tym samym prefiksie (częste w trybie
            # sekwencyjnym/multi-page) nadpisują się w ZIP i giną.
            fname = f"{idx:03d}_" + re.sub(r'[^\w\-.]', '_', prefix) + ".docx"
            zf.writestr(fname, docx)

    return buf.getvalue()
