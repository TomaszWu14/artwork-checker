"""
artwork_comparator.py — profesjonalne porównywanie artworków opakowań medycznych.

Silnik wykonuje:
  1. Renderowanie PDF/obrazu → PIL (PyMuPDF, 150 DPI podgląd / 220 DPI OCR)
  2. Pixel diff z kolorowymi regionami (czerwony/żółty/niebieski wg intensywności)
  3. Ekstrakcja tekstu: PyMuPDF → OCR fallback (Tesseract) dla stron graficznych
  4. Region-based OCR: color masking dla żółtych badge'y (gauge igły)
  5. Structured field diff: EAN, REF, Rewizja, Data, Gauge, Kolor, Format, Osoby
  6. Side-by-side overlay z zaznaczonymi regionami na obu wersjach
  7. Opcjonalna analiza AI (Claude)
"""

import base64
import difflib
import functools
import hashlib
import io
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("artwork_comparator")

# Per-request AI model selection. Under a threaded WSGI server multiple requests
# run concurrently in the same process; a plain module-level str would let
# request A's set_ai_model() clobber request B's in-flight AI call. ContextVar
# provides each request its own copy while reading the module default as fallback.
from contextlib import contextmanager
from contextvars import ContextVar as _ContextVar
_DEFAULT_AI_MODEL = "claude-sonnet-4-6"
_AI_MODEL_VAR: _ContextVar[str] = _ContextVar("artwork_ai_model", default=_DEFAULT_AI_MODEL)
# Public name kept for callers that read _AI_MODEL directly (e.g. logging).
_AI_MODEL = _DEFAULT_AI_MODEL

AVAILABLE_MODELS = [
    ("claude-haiku-4-5-20251001", "Haiku 4.5 — szybki, tani"),
    ("claude-sonnet-4-6",         "Sonnet 4.6 — domyślny (balans)"),
    ("claude-opus-4-7",           "Opus 4.7 — najdokładniejszy"),
]

def set_ai_model(model: str):
    _AI_MODEL_VAR.set(model)


def _current_ai_model() -> str:
    return _AI_MODEL_VAR.get()


def _risk_model() -> str:
    """Model do raportu RYZYKA (_ai_analyze). Scoring ryzyka nie wymaga
    najmocniejszego modelu, więc domyślnie szybki Haiku (≈3–5× szybciej niż
    Sonnet/Opus). Override: ARTWORK_RISK_MODEL (np. analizy-grade na życzenie)."""
    return (os.environ.get("ARTWORK_RISK_MODEL", "").strip()
            or "claude-haiku-4-5-20251001")


# OCR (verbatim text read of each crop) is the per-field bottleneck and runs once
# per crop. It's a simple transcription task, so it uses a fast model (Haiku) by
# default — independent of the analysis model above, which stays on the configured
# (more capable) model for risk scoring / section parsing. Override with
# ARTWORK_OCR_MODEL if a deployment wants the OCR step on a different model.
_DEFAULT_OCR_MODEL = "claude-haiku-4-5-20251001"


def _current_ocr_model() -> str:
    return (os.environ.get("ARTWORK_OCR_MODEL", "").strip() or _DEFAULT_OCR_MODEL)

try:
    import fitz
    HAS_FITZ = True
except ImportError:
    HAS_FITZ = False

try:
    from PIL import Image, ImageChops, ImageDraw
    import numpy as np
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    from scipy import ndimage
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

try:
    import pytesseract as _tess_probe
    _tess_probe.get_tesseract_version()
    _TESSERACT_AVAILABLE = True
except Exception:
    _TESSERACT_AVAILABLE = False

RENDER_DPI    = 150   # ↑ z 96 — lepsza jakość przy zoomie w split-screen
# OCR_DPI ↑ 150→300: small medical-label print (<5 pt) needs ~300 DPI for reliable
# OCR. Neural engines downscale to ≤2048 px internally, Tesseract benefits directly.
# Tunable via ARTWORK_OCR_DPI; set back to 150 on memory-constrained deployments.
OCR_DPI       = int(os.environ.get("ARTWORK_OCR_DPI", "300"))
BADGE_DPI     = 200
THUMB_W       = 700   # rozmiar miniaturki w raportach (małe pliki)
HIRES_W       = 1200  # rozmiar hires w split-screen viewerze (dobry zoom)
DIFF_THRESH   = int(os.environ.get("ARTWORK_DIFF_THRESH", "22"))   # pixel difference threshold (raised from 15 to suppress mild rendering noise)
OCR_MIN_CHARS = 5
MIN_REGION_PX = int(os.environ.get("ARTWORK_MIN_REGION_PX", "100"))  # min connected-component area px² — filters 5px shift strips (~75px²) but keeps char diffs (~150px²)
STRIP_RATIO   = 8    # reject regions whose bounding box is more elongated than this (thin layout-shift strips)
_TESSERACT_TIMEOUT = int(os.environ.get("TESSERACT_TIMEOUT", "30"))

def _env_flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in ("0", "false", "no", "off", "")

# Full-page image registration before pixel diff. Without it a 1-2 mm print shift
# lights up the whole page as "different" (the #1 source of false positives).
_ALIGN_ENABLED    = _env_flag("ARTWORK_ALIGN")
# Lift the fixed DIFF_THRESH by the measured per-page noise floor so anti-alias /
# print noise never registers as a region.
_ADAPTIVE_THRESH  = _env_flag("ARTWORK_ADAPTIVE_THRESH")
# Run the slow local OCR models (Qwen/Paddle/EasyOCR/Surya/DocTR). When off, the
# cascade is API engines (Claude/Mistral) → Tesseract only, avoiding the chain of
# 30 s timeouts when no API key is configured. Default on.
_OCR_LOCAL_ENGINES = _env_flag("ARTWORK_OCR_LOCAL", "0")


# ─── PERFORMANCE PROFILER ─────────────────────────────────────────────────────

class _PerfProfiler:
    """Lightweight, thread-safe stopwatch for one artwork comparison.

    Collected only when profiling is requested (ARTWORK_PROFILE=1 or the
    `profile_perf` flag on the comparison). Accumulates per-stage wall-clock
    time, per-field time (profile mode), per-page pixel-diff time, plus metadata,
    and serialises to the dict surfaced in the report's "Wydajność" section.

    A per-comparison instance (not a module global) — up to 3 comparisons run
    concurrently under _HEAVY_SEM, so a shared global would mix their timings.
    """

    # Human-readable Polish labels for known stage keys (template fallback: raw key).
    STAGE_LABELS = {
        "render_pages":   "Renderowanie stron (PDF→obraz)",
        "pdf_text":       "Ekstrakcja tekstu z PDF",
        "ocr_fullpage":   "OCR pełnych stron",
        "field_compare":  "Porównanie pól szablonu",
        "pixel_diff":     "Diff pikselowy + wyrównanie",
        "barcode":        "Walidacja kodów kreskowych",
        "icons":          "Porównanie ikon / piktogramów",
        "colorimetry":    "Kolorymetria",
        "ai_sections":    "Analiza sekcji AI",
        "ai_report":      "Raport ryzyka AI",
        "size_table":     "Analiza tabeli rozmiarów",
        "zones":          "Analiza strefowa OCR",
    }

    def __init__(self):
        self._lock = threading.Lock()
        self._t_start = time.perf_counter()
        self.stages: dict = {}
        self.fields: dict = {}
        self.pages: list = []
        self.meta: dict = {}

    @contextmanager
    def stage(self, name: str):
        _s = time.perf_counter()
        try:
            yield
        finally:
            self.add(name, (time.perf_counter() - _s) * 1000.0)

    def add(self, name: str, ms: float) -> None:
        with self._lock:
            self.stages[name] = self.stages.get(name, 0.0) + ms

    def field(self, name: str, ms: float) -> None:
        with self._lock:
            # Same field name twice → keep the larger (shouldn't happen, but safe).
            self.fields[name] = max(self.fields.get(name, 0.0), ms)

    def page(self, info: dict) -> None:
        with self._lock:
            self.pages.append(info)

    def set_meta(self, **kw) -> None:
        with self._lock:
            self.meta.update(kw)

    def to_dict(self) -> dict:
        total = (time.perf_counter() - self._t_start) * 1000.0
        with self._lock:
            stages = sorted(
                ({"key": k,
                  "label": self.STAGE_LABELS.get(k, k),
                  "ms": round(v, 1),
                  "pct": round(v / total * 100.0, 1) if total > 0 else 0.0}
                 for k, v in self.stages.items()),
                key=lambda x: x["ms"], reverse=True,
            )
            fields = sorted(
                ({"name": k, "ms": round(v, 1),
                  "pct": round(v / total * 100.0, 1) if total > 0 else 0.0}
                 for k, v in self.fields.items()),
                key=lambda x: x["ms"], reverse=True,
            )
            pages = list(self.pages)
            meta = dict(self.meta)
        return {
            "enabled":  True,
            "total_ms": round(total, 1),
            "stages":   stages,
            "fields":   fields,
            "pages":    pages,
            "meta":     meta,
        }


class _NullProfiler:
    """No-op profiler used when profiling is disabled — zero overhead, same API."""
    @contextmanager
    def stage(self, name: str):
        yield
    def add(self, *a, **k):    pass
    def field(self, *a, **k):  pass
    def page(self, *a, **k):   pass
    def set_meta(self, *a, **k): pass
    def to_dict(self) -> dict: return {"enabled": False}


# ─── LIVE PROGRESS (for the compare progress bar) ─────────────────────────────
# Per-comparison progress keyed by a client-supplied progress_id so the frontend
# can poll the real current stage instead of a scripted timeline. Backed by a tiny
# JSON file on shared disk so the progress POLL (which may land on a different
# gunicorn worker than the one running the comparison) still sees it.
_PROGRESS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "progress")
_PROGRESS_TTL = 900   # seconds — stale files are ignored / swept


def _progress_path(progress_id) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "", str(progress_id))[:64]
    return os.path.join(_PROGRESS_DIR, f"{safe}.json")


def set_progress(progress_id, pct, label) -> None:
    if not progress_id:
        return
    try:
        os.makedirs(_PROGRESS_DIR, exist_ok=True)
        payload = {"pct": int(max(0, min(100, pct))), "label": str(label), "ts": time.time()}
        tmp = _progress_path(progress_id) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, _progress_path(progress_id))   # atomic
        # Opportunistic sweep of stale files (cheap, bounded).
        if int(payload["ts"]) % 30 == 0:
            _sweep_progress()
    except Exception:
        pass


def get_progress(progress_id):
    if not progress_id:
        return None
    try:
        with open(_progress_path(progress_id)) as f:
            v = json.load(f)
        if time.time() - v.get("ts", 0) > _PROGRESS_TTL:
            return None
        return v
    except Exception:
        return None


def clear_progress(progress_id) -> None:
    if not progress_id:
        return
    try:
        os.remove(_progress_path(progress_id))
    except Exception:
        pass


def _sweep_progress() -> None:
    try:
        now = time.time()
        for fn in os.listdir(_PROGRESS_DIR):
            fp = os.path.join(_PROGRESS_DIR, fn)
            try:
                if now - os.path.getmtime(fp) > _PROGRESS_TTL:
                    os.remove(fp)
            except Exception:
                pass
    except Exception:
        pass


_OSD_WARNED = False
def _warn_osd_once(err) -> None:
    """Loguje JEDEN raz ostrzeżenie, gdy OSD (auto-orientacja) jest niedostępne —
    najczęściej brak osd.traineddata. Bez tego strony obrócone o 90/180° nie są
    automatycznie prostowane, więc deployment powinien o tym wiedzieć."""
    global _OSD_WARNED
    if _OSD_WARNED:
        return
    _m = str(err).lower()
    if any(t in _m for t in ("osd", "tessdata", "traineddata", "failed loading")):
        _OSD_WARNED = True
        logger.warning(
            "OSD auto-orientacja niedostępna (prawdopodobnie brak osd.traineddata) — "
            "strony obrócone o 90/180° nie będą automatycznie prostowane. "
            "Zainstaluj pakiet z osd.traineddata. Szczegóły: %s", err
        )


# ─── DATACLASSES ──────────────────────────────────────────────────────────────

@dataclass
class PageDiff:
    page_num:        int
    pixel_diff_pct:  float
    diff_regions:    list
    text_diffs:      list
    field_diffs:     list
    img_a_b64:       Optional[str]   # miniaturka (THUMB_W) — do raportów
    img_b_b64:       Optional[str]
    img_a_annot_b64: Optional[str]
    img_b_annot_b64: Optional[str]
    img_diff_b64:    Optional[str]
    img_a_hires_b64:       Optional[str] = None   # hires (HIRES_W) — do split-screen
    img_b_hires_b64:       Optional[str] = None
    img_a_annot_hires_b64: Optional[str] = None
    img_b_annot_hires_b64: Optional[str] = None
    has_critical:    bool = False
    has_important:   bool = False
    summary:         str  = ""
    size_mismatch:   bool = False
    section_diffs:   list = field(default_factory=list)


@dataclass
class ArtworkCompareResult:
    file_a:           str
    file_b:           str
    pages_a:          int
    pages_b:          int
    dims_a:           str = ""
    dims_b:           str = ""
    page_diffs:       list = field(default_factory=list)
    total_pixel_diff: float = 0.0
    identical:        bool  = False
    critical_count:   int   = 0
    important_count:  int   = 0
    info_count:       int   = 0
    ok_count:         int   = 0
    risk_level:       str   = "ok"
    summary:          str   = ""
    field_report:     list  = field(default_factory=list)
    barcode_report:   dict  = field(default_factory=dict)
    icon_comparison:  list  = field(default_factory=list)
    ai_report:        Optional[dict] = None
    ocr_failed:       bool  = False
    # Profile / mapped fields mode
    profile_active:     bool = False
    profile_name:       str  = ""
    mapped_field_count: int  = 0
    # File sizes (bytes)
    file_size_a:        int  = 0
    file_size_b:        int  = 0
    # Colorimetry: dominant colors per file
    colorimetry:        dict = field(default_factory=dict)
    # Detailed performance breakdown (only populated when profiling requested)
    performance:        dict = field(default_factory=dict)

    def to_dict(self, include_images: bool = False) -> dict:
        def pd(p):
            d = {
                "page_num": p.page_num, "pixel_diff_pct": round(p.pixel_diff_pct, 2),
                "diff_regions": p.diff_regions, "text_diffs": p.text_diffs,
                "field_diffs": p.field_diffs,
                "has_critical": p.has_critical, "has_important": p.has_important,
                "summary": p.summary, "size_mismatch": p.size_mismatch,
                "section_diffs": p.section_diffs,
                "img_a_b64": None, "img_b_b64": None,
                "img_a_annot_b64": None, "img_b_annot_b64": None, "img_diff_b64": None,
            }
            if include_images:
                d.update({"img_a_b64": p.img_a_b64, "img_b_b64": p.img_b_b64,
                           "img_a_annot_b64": p.img_a_annot_b64,
                           "img_b_annot_b64": p.img_b_annot_b64,
                           "img_diff_b64": p.img_diff_b64})
            return d
        return {
            "file_a": self.file_a, "file_b": self.file_b,
            "pages_a": self.pages_a, "pages_b": self.pages_b,
            "dims_a": self.dims_a, "dims_b": self.dims_b,
            "total_pixel_diff": round(self.total_pixel_diff, 2),
            "identical": self.identical,
            "ocr_failed": self.ocr_failed,
            "critical_count": self.critical_count,
            "important_count": self.important_count,
            "info_count": self.info_count, "ok_count": self.ok_count,
            "risk_level": self.risk_level, "summary": self.summary,
            "field_report": self.field_report,
            "barcode_report": self.barcode_report,
            "icon_comparison": self.icon_comparison,
            "page_diffs": [pd(p) for p in self.page_diffs],
            "ai_report": self.ai_report,
            "profile_active": self.profile_active,
            "profile_name": self.profile_name,
            "mapped_field_count": self.mapped_field_count,
            "file_size_a": self.file_size_a,
            "file_size_b": self.file_size_b,
            "colorimetry": self.colorimetry,
            "performance": self.performance,
        }

    def page_images(self, page_idx: int) -> dict:
        if page_idx >= len(self.page_diffs):
            return {}
        p = self.page_diffs[page_idx]
        return {
            "page_num": p.page_num,
            "img_a_b64":       p.img_a_hires_b64       or p.img_a_b64,
            "img_b_b64":       p.img_b_hires_b64       or p.img_b_b64,
            "img_a_annot_b64": p.img_a_annot_hires_b64 or p.img_a_annot_b64,
            "img_b_annot_b64": p.img_b_annot_hires_b64 or p.img_b_annot_b64,
            "img_diff_b64": p.img_diff_b64,
        }


# ─── ŁADOWANIE STRON ──────────────────────────────────────────────────────────

# Per-process page cache: (path, mtime, dpi, page_idx) → PIL Image copy
# Capped at 64 entries to limit memory usage (~20-50 MB total).
_PAGE_CACHE: dict = {}
_PAGE_CACHE_LOCK = threading.Lock()
_PAGE_CACHE_MAX = 64


def _page_cache_key(path: str, dpi: int, page_idx) -> tuple:
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0
    return (path, mtime, dpi, page_idx)


def _page_cache_get(key: tuple):
    with _PAGE_CACHE_LOCK:
        entry = _PAGE_CACHE.get(key)
        return entry.copy() if entry is not None else None


def _page_cache_put(key: tuple, img):
    with _PAGE_CACHE_LOCK:
        if len(_PAGE_CACHE) >= _PAGE_CACHE_MAX:
            # Evict oldest entry (insertion order in Python 3.7+) i zamknij obraz,
            # żeby nie zostawiać niezwolnionych buforów PIL.
            oldest = next(iter(_PAGE_CACHE))
            _old_img = _PAGE_CACHE.pop(oldest)
            try:
                _old_img.close()
            except Exception:
                pass
        _PAGE_CACHE[key] = img.copy()


def _pdf_to_images(path: str, dpi: int, max_pages: int = 50) -> list:
    if not HAS_FITZ:
        raise RuntimeError("Zainstaluj pymupdf: pip install pymupdf")
    doc = fitz.open(path)
    try:
        mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        imgs = []
        for pno, page in enumerate(doc):
            if pno >= max_pages:   # limit stron — chroni przed OOM na wielkich PDF
                break
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            pix = None             # zwolnij bufor C pixmapy od razu
            # Downscale if either dimension exceeds cap (mirror _load_pages) —
            # chroni przed OOM na wielkoformatowych arkuszach.
            if img.width > _MAX_IMG_PX or img.height > _MAX_IMG_PX:
                scale = _MAX_IMG_PX / max(img.width, img.height)
                img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
            imgs.append(img)
        return imgs
    finally:
        doc.close()


_MAX_IMG_PX = 3500  # cap longest side to avoid OOM on large-format packaging sheets

def _fitz_page_needs_rotation(page) -> int:
    """Return 0, 90, 180 or 270 — the extra rotation needed to make the page
    right-side-up, beyond what PyMuPDF already applied via /Rotate.

    Strategy: read the page's character bounding boxes.  In a properly
    rendered page, the majority of characters have their origin in the
    TOP half of the page (y_center < page_height/2 for the first characters
    encountered reading order).  If the dominant text mass is in the bottom
    half, the page is upside-down (180°).  This works without any OCR.
    """
    try:
        blocks = page.get_text("rawdict", flags=fitz.TEXT_PRESERVE_WHITESPACE).get("blocks", [])
        ys = []
        h = page.rect.height
        for b in blocks:
            for ln in b.get("lines", []):
                for sp in ln.get("spans", []):
                    if (sp.get("text", "").strip()):
                        ys.append(sp["origin"][1])   # y of text baseline
                        if len(ys) >= 60:
                            break
                if len(ys) >= 60:
                    break
            if len(ys) >= 60:
                break
        if len(ys) < 4:
            return 0
        # PDF coordinate origin is bottom-left; y increases upward.
        # PyMuPDF converts to top-left (y increases downward).
        # If most baselines are in the lower 40% of the page (y > 0.6*h),
        # the content is rendered upside-down.
        bottom_count = sum(1 for y in ys if y > h * 0.6)
        if bottom_count / len(ys) >= 0.65:
            return 180
        return 0
    except Exception:
        return 0


def _load_pages(path: str, dpi: int, page_idx: int = None) -> list:
    """Load all pages or a single page (0-based page_idx) from a PDF.

    Results are cached per (path, mtime, dpi, page_idx) so repeated calls
    within the same process (e.g. multiple comparison runs for the same master)
    avoid re-rendering expensive PyMuPDF operations.
    """
    if path.lower().endswith(".pdf"):
        if not HAS_FITZ:
            raise RuntimeError("Zainstaluj pymupdf: pip install pymupdf")
        doc = fitz.open(path)
        try:
            mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
            imgs = []
            indices = [page_idx] if (page_idx is not None and 0 <= page_idx < len(doc)) else range(len(doc))
            for i in indices:
                key = _page_cache_key(path, dpi, i)
                cached = _page_cache_get(key)
                if cached is not None:
                    imgs.append(cached)
                    continue
                pix = doc[i].get_pixmap(matrix=mat, alpha=False)
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                # Downscale if either dimension exceeds cap
                if img.width > _MAX_IMG_PX or img.height > _MAX_IMG_PX:
                    scale = _MAX_IMG_PX / max(img.width, img.height)
                    img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
                _page_cache_put(key, img)
                imgs.append(img)
            return imgs
        finally:
            doc.close()
    # Image files: cache by path+mtime only. dpi is irrelevant for raster files
    # (Image.open ignores it), so ignore it in the key — otherwise the same image
    # opened at 3 DPIs would triple decode/memory. Use a fixed sentinel dpi.
    key = _page_cache_key(path, 0, 0)
    cached = _page_cache_get(key)
    if cached is not None:
        return [cached]
    raw = Image.open(path)
    try:
        img = raw.convert("RGB")
    finally:
        raw.close()
    if img.width > _MAX_IMG_PX or img.height > _MAX_IMG_PX:
        scale = _MAX_IMG_PX / max(img.width, img.height)
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    _page_cache_put(key, img)
    return [img]


def _get_dims_mm(path: str, page_idx: int = None) -> tuple:
    if not HAS_FITZ or not path.lower().endswith(".pdf"):
        return (0, 0)
    doc = fitz.open(path)
    try:
        if len(doc) == 0:
            return (0, 0)
        idx = page_idx if (page_idx is not None and 0 <= page_idx < len(doc)) else 0
        p = doc[idx]
        w, h = p.rect.width / 72 * 25.4, p.rect.height / 72 * 25.4
        return (round(w), round(h))
    finally:
        doc.close()


# ─── OCR ──────────────────────────────────────────────────────────────────────

def _extract_text_from_pdf(path: str) -> list:
    if not HAS_FITZ:
        return []
    doc = fitz.open(path)
    try:
        return [page.get_text("text") or "" for page in doc]
    finally:
        doc.close()


def _orient_for_ocr(img, min_confidence: float = 1.5) -> "Image.Image":
    """Detect text orientation via Tesseract OSD and rotate to upright.

    Only rotates when OSD reports high enough confidence — icon-only crops or
    very short text returns unreliable angles which used to garble the display.
    """
    try:
        import pytesseract
        w, h = img.size
        if w == 0 or h == 0:
            return img
        scale = max(1, 300 // min(w, h, 300))
        probe = img.resize((w * scale, h * scale), Image.LANCZOS) if scale > 1 else img
        osd = pytesseract.image_to_osd(probe, config="--psm 0 -c min_characters_to_try=5")
        m_angle = re.search(r"Rotate:\s*(\d+)", osd)
        m_conf  = re.search(r"Orientation confidence:\s*([\d.]+)", osd)
        if not m_angle:
            return img
        if m_conf and float(m_conf.group(1)) < min_confidence:
            return img   # Untrustworthy reading — better leave as-is than spin it wrong
        angle = int(m_angle.group(1))
        if angle == 0:
            return img
        # PIL rotate is counter-clockwise; OSD angle is clockwise
        rotated = img.rotate(-angle, expand=True)
        logger.debug("OSD auto-rotate: %d° (conf=%s)",
                     angle, m_conf.group(1) if m_conf else "?")
        return rotated
    except Exception as _osd_err:
        _warn_osd_once(_osd_err)
        return img


_PL_EN_STOPWORDS = {
    # Polish high-frequency
    "i", "w", "z", "ze", "na", "do", "od", "po", "za", "u", "o", "lub", "oraz",
    "jak", "to", "co", "się", "sie", "nie", "tak", "lub", "dla", "przed", "tylko",
    "może", "moze", "być", "byc", "jest", "są", "sa", "ich", "tym", "tej", "te",
    "ten", "ta", "by", "iż", "iz", "że", "ze", "który", "ktory", "która", "ktora",
    "więcej", "wiecej", "mniej", "każdy", "kazdy", "wszystkie", "wszystkich",
    "produkt", "produkty", "przed", "użycia", "uzycia", "przechowywać", "acme",
    # English high-frequency
    "the", "and", "or", "for", "in", "of", "with", "on", "to", "from", "by",
    "this", "that", "all", "any", "more", "less", "each", "if", "when",
    "before", "after", "use", "store", "open", "non", "product", "made",
    # Ukrainian high-frequency (Cyrillic)
    "і", "та", "не", "він", "вона", "вони", "ми", "ви", "ти", "це", "для",
    "від", "до", "на", "по", "за", "що", "як", "але", "або", "може", "бути",
    "є", "були", "буде", "які", "який", "яка", "яке", "при", "про", "без",
    "його", "її", "їх", "нас", "вас", "між", "через",
    # Russian high-frequency (Cyrillic)
    "и", "в", "не", "он", "на", "с", "что", "а", "по", "это", "она",
    "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за", "бы",
    "до", "из", "от", "для", "при", "или", "они", "мы", "как", "все",
    "без", "между", "через", "если", "когда", "после", "перед",
    # German
    "und", "die", "der", "das", "ist", "von", "mit", "für", "nicht", "ein",
    "eine", "oder", "bei", "nach", "vor", "über", "unter", "des", "dem",
    "den", "eine", "lager", "bitte", "hinweis", "verwendung", "produkt",
    # French
    "et", "le", "la", "les", "de", "du", "des", "un", "une", "est", "pas",
    "avec", "pour", "sur", "dans", "par", "ou", "au", "aux", "ce", "qui",
    "que", "ne", "se", "son", "sa", "ses", "leur", "leurs", "conserver",
    # Spanish
    "el", "la", "los", "las", "de", "en", "con", "por", "para", "del",
    "una", "uno", "su", "sus", "son", "este", "esta", "estos", "estas",
    "sin", "producto", "antes", "uso", "conservar", "almacenar",
    # Italian
    "il", "lo", "gli", "le", "di", "da", "con", "per", "nel", "nella",
    "dei", "del", "una", "uno", "non", "sono", "questo", "questa",
    "prima", "dopo", "uso", "prodotto", "conservare",
    # Dutch
    "de", "het", "een", "van", "in", "is", "op", "met", "voor", "niet",
    "te", "dit", "dat", "zijn", "bij", "worden", "bewaren", "gebruik",
    # Estonian
    "ja", "on", "ei", "kui", "aga", "kuid", "kas", "see", "mis", "kes",
    "ning", "või", "ka", "nii", "siis", "veel", "seda", "pole", "alla",
    "hoida", "enne", "pärast", "kasuta", "ladustada", "toode",
    "laste", "eest", "kuiv", "kohas", "kuni", "suurus", "partii",
    "steriilne", "ühekordseks", "lateksivaba", "aegumiskuupäev",
    "äreulatuses", "kasutamiseks", "kasutusjuhend", "lugege",
    # Latvian
    "un", "ir", "nav", "kas", "lai", "bet", "vai", "arī", "tad", "kā",
    "par", "pie", "glabāt", "lietot", "produkts", "pirms",
    "bērniem", "sterilizēts", "vienreizlietojams", "izmantošana",
    # Lithuanian
    "ir", "ne", "tai", "bet", "ar", "dar", "jau", "kai", "nuo", "su",
    "iki", "per", "prie", "laikyti", "naudoti", "produktas",
    "vaikams", "sterilizuotas", "vienkartinis", "naudojimas",
    # Czech / Slovak
    "a", "je", "na", "se", "ve", "ze", "po", "od", "do", "pro", "při",
    "ale", "nebo", "jsou", "být", "tento", "tato", "toto", "před",
    "produkt", "skladovat", "použití", "sterilní", "jednorázový",
    # Romanian
    "și", "si", "de", "la", "în", "in", "cu", "pe", "nu", "este", "sunt",
    "pentru", "sau", "cel", "cea", "produs", "sterile", "ambalaj",
    # Hungarian
    "és", "es", "az", "van", "nem", "meg", "fel", "termék",
    "tárolja", "előtt", "után", "steril", "egyszer", "használatos",
    # Finnish / Swedish / Norwegian / Danish (Nordic)
    "ja", "ei", "on", "se", "tai", "jos", "kuin", "vain",
    "och", "att", "det", "den", "till", "som", "men", "inte",
    "förvara", "använda", "steril", "engångs",
    # Medical packaging vocabulary (language-neutral)
    "sterile", "steril", "sterility", "steriilne",
    "latex", "latexfrei", "lateksivaba",
    "single", "einmal", "jednorazowy",
    "expiry", "ablauf", "erhebt",
    "batch", "charge", "lot", "partia", "partii",
    "size", "sizes", "größe", "rozmiar", "suurus",
    "storage", "lagerung", "przechowywanie",
    "temperature", "temperatur", "temperatura",
    "humidity", "feuchtigkeit", "wilgotność",
}

# Matches Latin (incl. EU diacritics) AND Cyrillic.
# Explicit sub-ranges skip × (U+00D7) and ÷ (U+00F7) which fall inside À-ɏ.
_WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ɏѐ-ӿ]{1,12}")

# Used by vowel-density fallback scorer — covers Latin + Cyrillic vowels
_VOWELS = frozenset(
    "aeiouäöüåøæáéíóúàèìòùâêîôûãõýůñőűęąīūАаЕеЁёИиОоУуЫыЭэЮюЯяІіЇїЄє"
)


def _ocr_quality_score(text: str) -> int:
    """Score OCR text legibility (higher = more legible).
    0 = garbled / unusable. Used to pick the best rotation from 0/90/180/270.
    Handles Latin (Polish/English) and Cyrillic (Ukrainian/Russian) text.
    """
    if not text or len(text) < 3:
        return 0
    normal = sum(1 for c in text if c.isalnum() or c in ' .,/:;()-+=@%°#\n')
    ratio = normal / max(len(text), 1)
    if ratio < 0.45:
        return 0
    words = _WORD_RE.findall(text.lower())
    # Primary: stop-word count (covers PL/EN/UA/RU/DE/FR/ES/IT/NL/ET/LV/LT/…)
    sw_score = sum(1 for w in words if w in _PL_EN_STOPWORDS)
    if sw_score >= 2:
        return sw_score
    # Fallback: "natural word" count — language-agnostic.
    # A "natural" word has vowel ratio in the realistic band [0.20, 0.55]:
    # - < 0.20: all-consonant noise ("svz", "mmm")
    # - > 0.55: too vowel-heavy, typical of garbled OCR ("bujuueos"=62%, "ajou"=75%)
    # Every European language has per-word vowel ratios in [0.25, 0.55] on average.
    # Capped at 2 so it can never beat a genuine stop-word score of 3+.
    if not words:
        return 0
    natural = sum(
        1 for w in words
        if len(w) >= 3
        and 0.20 <= (sum(1 for c in w if c in _VOWELS) / len(w)) <= 0.55
    )
    if natural >= 4:
        return 2  # Enough plausible words — likely readable text in unknown language
    if natural >= 2:
        return 1
    return 0


def _ocr_quality_check(text: str) -> str:
    """Backward-compatible wrapper. Use _ocr_quality_score for rotation picking."""
    if not text or len(text) < 3:
        return text
    normal = sum(1 for c in text if c.isalnum() or c in ' .,/:;()-+=@%°#\n')
    ratio = normal / max(len(text), 1)
    if ratio < 0.45:
        return ""
    if len(text) >= 40 and _ocr_quality_score(text) < 2:
        return ""
    return text


def _ocr_rotation_sensitive(img) -> str:
    """OCR that is SENSITIVE to text orientation — used only for rotation detection.

    Skips Claude Vision and Mistral OCR because those engines are rotation-agnostic
    (they correctly read text at any angle), which makes all four rotation candidates
    get the same quality score and 0° always wins even when the image is upside-down.

    Uses only: PaddleOCR (angle-classifier) → Tesseract (classic, fails on rotated).
    Returns empty string when neither is available.
    """
    # PaddleOCR with cls=False — deliberately disable per-line angle correction
    # so that the whole-image rotation is what determines readability.
    reader = _get_paddleocr("latin") or _get_paddleocr("cyrillic")
    if reader:
        try:
            import numpy as np
            arr = np.array(img.convert("RGB"))
            result = reader.ocr(arr, cls=False)
            if result and result[0]:
                texts = [line[1][0] for line in result[0] if line and len(line) >= 2 and line[1][1] > 0.3]
                return " ".join(texts)
        except Exception:
            pass
    # Tesseract — rotation-sensitive; use minimal lang set for speed (orientation
    # scoring only needs to judge readability, not extract accurate text).
    try:
        import pytesseract
        _t0 = time.time()
        _result = pytesseract.image_to_string(img, lang="pol+eng+deu", config="--psm 6",
                                              timeout=_TESSERACT_TIMEOUT)
        _elapsed = time.time() - _t0
        if _elapsed > _TESSERACT_TIMEOUT:
            logger.warning("Tesseract OCR took %.1fs (timeout threshold: %ds)", _elapsed, _TESSERACT_TIMEOUT)
        return _result
    except Exception:
        pass
    return ""


def _ocr_with_orient_fallback(crop):
    """OCR with smart orientation.

    Packaging artwork often has text printed at 0°/90°/180°/270° on different
    panels. Uses rotation-sensitive engines (PaddleOCR / Tesseract) to score
    each rotation candidate; picks the best orientation; then runs the full
    OCR cascade (including Claude Vision) at that orientation for accurate text.

    Claude Vision and Mistral are skipped during rotation scoring because they
    correctly read text at any angle — causing all four rotations to tie and
    0° (the original, possibly upside-down image) to always win.

    Returns (oriented_crop, cleaned_text).
    """
    if not crop:
        return None, ""
    base = _orient_for_ocr(crop)

    # Check if a rotation-sensitive engine is available
    rotation_sensitive_available = (
        bool(_get_paddleocr("latin") or _get_paddleocr("cyrillic"))
        or _TESSERACT_AVAILABLE
    )

    candidates = []  # [(score, raw_len, img, text), ...]
    for ang in (0, 90, 180, 270):
        try:
            cand = base if ang == 0 else base.rotate(ang, expand=True)
        except Exception:
            continue
        if rotation_sensitive_available:
            # Score with rotation-sensitive OCR only
            try:
                t = _ocr_rotation_sensitive(cand).strip()
                t = " ".join(t.split())
            except Exception:
                t = ""
        else:
            # No rotation-sensitive engine available — use full cascade but
            # only for text length tie-breaking (all scores will be equal).
            try:
                t = _extract_text_ocr(cand, auto_orient=False).strip()
                t = " ".join(t.split())
            except Exception:
                t = ""
        score = _ocr_quality_score(t)
        candidates.append((score, len(t), cand, t))
        # Short-circuit when 0° is clearly correct (≥5 stop-words).
        if ang == 0 and score >= 5:
            break

    if not candidates:
        return base, ""

    # Best legibility wins; ties broken by raw text length
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    score, _, best_img, _ = candidates[0]
    if score == 0:
        # All rotations garbled — pick the rotation with the most raw characters
        # (avoids always picking 0° when a rotated panel produced more OCR output).
        candidates.sort(key=lambda c: c[1], reverse=True)
        _, _, best_img, _ = candidates[0]

    # Now extract final text at the chosen orientation using the full cascade
    # (including Claude Vision) for maximum accuracy.
    try:
        best_text = _extract_text_ocr(best_img, auto_orient=False).strip()
        best_text = " ".join(best_text.split())
    except Exception:
        best_text = ""
    return best_img, best_text


def _detect_page_rotation_angle(img) -> int:
    """Detect page text orientation via Tesseract OSD. Returns 0, 90, 180, or 270.
    Used to rotate display crops without changing the source image coordinate system.
    Downscales large pages aggressively — OSD doesn't need high resolution."""
    try:
        import pytesseract
        w, h = img.size
        # OSD works fine on ~800px max dim; downscale large pages for speed
        max_dim = max(w, h)
        if max_dim > 800:
            scale = 800 / max_dim
            probe = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        elif min(w, h) < 200:
            up = max(1, 300 // min(w, h))
            probe = img.resize((w * up, h * up), Image.LANCZOS)
        else:
            probe = img
        osd = pytesseract.image_to_osd(probe, config="--psm 0 -c min_characters_to_try=5")
        m = re.search(r"Rotate:\s*(\d+)", osd)
        return int(m.group(1)) if m else 0
    except Exception as _osd_err:
        _warn_osd_once(_osd_err)
        return 0


def _rotate_for_display(img, angle: int):
    """Apply rotation to PIL image for display purposes only."""
    if not img or not angle:
        return img
    try:
        return img.rotate(-angle, expand=True)
    except Exception:
        return img


_OCR_LANGS: str = ""


def _get_ocr_langs() -> str:
    """Return '+'-joined Tesseract lang string.

    Capped at 10 languages — loading all installed packs (often 24+) multiplies
    Tesseract inference time by 5–8×. Priority: Polish/English always first,
    then the most common EU medical-device label languages.
    """
    global _OCR_LANGS
    if _OCR_LANGS:
        return _OCR_LANGS
    # Order matters — Tesseract tries languages left-to-right. Cyrillic (ukr/rus/bul)
    # included because ACME medical packaging carries Ukrainian/Russian authorised-rep
    # text; without the pack Tesseract returns garbage that drives false diffs. Capped
    # (ARTWORK_OCR_MAX_LANGS, default 12) so the rarely-reached Tesseract fallback
    # doesn't load 24+ packs and run 5–8× slower.
    _TESSERACT_EU_EXTRAS = (
        "deu", "fra", "spa", "ita", "nld", "ukr", "rus", "ces", "hun", "por", "bul",
    )
    try:
        _cap = int(os.environ.get("ARTWORK_OCR_MAX_LANGS", "12"))
    except ValueError:
        _cap = 12
    try:
        import pytesseract
        available = set(pytesseract.get_languages())
        langs = ["pol", "eng"]
        for extra in _TESSERACT_EU_EXTRAS:
            if extra in available and extra not in langs:
                langs.append(extra)
        _OCR_LANGS = "+".join(langs[:max(2, _cap)])
    except Exception:
        _OCR_LANGS = "pol+eng"
    logger.info("OCR language packs: %s", _OCR_LANGS)
    return _OCR_LANGS


# ── OCR singletons ────────────────────────────────────────────────────────────
import threading as _threading

_paddleocr_latin    = None   # PaddleOCR — Latin script (Polish, English, …)
_paddleocr_cyrillic = None   # PaddleOCR — Cyrillic script (Russian, Ukrainian)
_paddleocr_lock     = _threading.Lock()

_easyocr_reader = None
_easyocr_lock   = _threading.Lock()

_doctr_reader = None   # DocTR — deep-learning OCR (between EasyOCR and Tesseract)
_doctr_lock   = _threading.Lock()

_got_ocr_model     = None   # GOT-OCR 2.0 — kept for reference, superseded by Qwen2.5-VL
_got_ocr_tokenizer = None
_got_ocr_lock      = _threading.Lock()

_qwen_model     = None   # Qwen2.5-VL-3B — best local VLM, superior 0/O 1/I disambiguation
_qwen_processor = None
_qwen_lock      = _threading.Lock()

_rapidocr_engine = None  # RapidOCR — PaddleOCR models in ONNX (self-contained, fast, CPU)
_rapidocr_lock   = _threading.Lock()


def _get_rapidocr():
    """Lazy RapidOCR singleton — PaddleOCR's recognition models converted to ONNX.

    Self-contained (ships its own models, no paddlepaddle dependency, ~50–80 MB),
    fast on CPU (~0.5–1 s/page) and a drop-in for the Paddle tier whose models were
    missing in production. Tries rapidocr-onnxruntime (1.x) then rapidocr (2.x).
    Caches a negative result (False) so a missing install warns only once."""
    global _rapidocr_engine
    if _rapidocr_engine is not None:
        return _rapidocr_engine or None
    with _rapidocr_lock:
        if _rapidocr_engine is not None:
            return _rapidocr_engine or None
        try:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError:
                from rapidocr import RapidOCR
            _rapidocr_engine = RapidOCR()
            logger.info("RapidOCR (ONNX) initialised")
        except Exception as exc:
            logger.warning("RapidOCR unavailable: %s", exc)
            _rapidocr_engine = False
    return _rapidocr_engine or None


def _rapidocr_readtext(img) -> str:
    """OCR via RapidOCR (ONNX). Returns space-joined recognized text or ''.
    Handles both result shapes: 1.x → (list[[box, text, score]], elapse);
    2.x → object exposing .txts."""
    engine = _get_rapidocr()
    if engine is None:
        return ""
    try:
        import numpy as np
        arr = np.array(img.convert("RGB"))
        out = engine(arr)
        result = out[0] if isinstance(out, tuple) else out
        if result is None:
            return ""
        if hasattr(result, "txts"):                      # rapidocr 2.x
            return " ".join(t for t in (result.txts or []) if t).strip()
        lines = []                                        # rapidocr 1.x list form
        for item in result:
            if isinstance(item, (list, tuple)) and len(item) >= 2 and item[1]:
                lines.append(str(item[1]))
        return " ".join(lines).strip()
    except Exception as exc:
        logger.debug("RapidOCR failed: %s", exc)
        return ""


def _get_qwen():
    """Lazy Qwen2.5-VL-3B-Instruct singleton (HuggingFace transformers, CPU).

    DISABLED BY DEFAULT: the 3B model in float32 needs ~12 GB RAM, which OOM-kills
    workers on an 8 GB host (each worker loads its own copy). The OCR cascade has
    Mistral/Paddle/Tesseract fallbacks, so the local VLM is a luxury, not a
    necessity. Set ARTWORK_ENABLE_LOCAL_VLM=1 only on a host with >=16 GB RAM.
    """
    global _qwen_model, _qwen_processor
    # Fast-path read outside lock — safe only for None/False sentinel check.
    # Writing _qwen_model must happen inside _qwen_lock to avoid a data race
    # where one thread clobbers another thread's loaded model.
    if _qwen_model is not None:
        return (_qwen_processor, _qwen_model) if _qwen_model is not False else None
    with _qwen_lock:
        if _qwen_model is not None:
            return (_qwen_processor, _qwen_model) if _qwen_model is not False else None
        if os.environ.get("DISABLE_QWEN"):
            _qwen_model = False
            return None
        if os.environ.get("ARTWORK_ENABLE_LOCAL_VLM", "").strip() not in ("1", "true", "yes"):
            _qwen_model = False
            return None
        try:
            import torch
            from transformers import Qwen2_5_VLForConditionalGeneration, AutoProcessor
            # Bandit B615: stała nazwa zaufanego modelu z HF Hub (nie z wejścia); opcjonalny model ładowany leniwie.
            _qwen_processor = AutoProcessor.from_pretrained(  # nosec B615
                "Qwen/Qwen2.5-VL-3B-Instruct", trust_remote_code=True)
            # Bandit B615: stała nazwa zaufanego modelu z HF Hub (nie z wejścia); opcjonalny model ładowany leniwie.
            _qwen_model = Qwen2_5_VLForConditionalGeneration.from_pretrained(  # nosec B615
                "Qwen/Qwen2.5-VL-3B-Instruct",
                trust_remote_code=True,
                torch_dtype=torch.float32,
                device_map="cpu",
                low_cpu_mem_usage=True,
            ).eval()
            logger.warning("Qwen2.5-VL model loaded — this uses ~12GB RAM; set DISABLE_QWEN=1 to skip")
            logger.info("Qwen2.5-VL-3B loaded")
            return (_qwen_processor, _qwen_model)
        except Exception as exc:
            logger.warning("Qwen2.5-VL unavailable: %s", exc)
            _qwen_model = False
            return None


def _qwen_readtext(img) -> str:
    """Run Qwen2.5-VL-3B-Instruct on a PIL image. Best local model for 0/O/1/I disambiguation."""
    pair = _get_qwen()
    if not pair:
        return ""
    processor, model = pair
    try:
        import torch
        messages = [{"role": "user", "content": [
            {"type": "image", "image": img},
            {"type": "text", "text":
                "Extract all text from this image exactly as printed. "
                "Pay special attention to numbers and codes — never confuse 0 with O or 1 with I. "
                "Return only the extracted text, no explanation."},
        ]}]
        try:
            from qwen_vl_utils import process_vision_info
            text_prompt = processor.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True)
            image_inputs, video_inputs = process_vision_info(messages)
            inputs = processor(
                text=[text_prompt], images=image_inputs,
                videos=video_inputs, return_tensors="pt")
        except ImportError:
            # qwen_vl_utils not installed — fall back to direct image encoding
            from io import BytesIO
            import base64 as _b64
            buf = BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=85)
            b64 = _b64.b64encode(buf.getvalue()).decode()
            inputs = processor(
                text=processor.apply_chat_template(
                    [{"role": "user", "content": [
                        {"type": "image", "image": f"data:image/jpeg;base64,{b64}"},
                        {"type": "text", "text": "Extract all text exactly as printed."},
                    ]}], tokenize=False, add_generation_prompt=True),
                images=[img.convert("RGB")],
                return_tensors="pt")
        with torch.no_grad():
            generated = model.generate(**inputs, max_new_tokens=512)
        trimmed = [out[len(inp):] for inp, out in zip(inputs.input_ids, generated)]
        result = processor.batch_decode(trimmed, skip_special_tokens=True)[0]
        return (result or "").strip()
    except Exception as exc:
        logger.debug("Qwen2.5-VL inference failed: %s", exc)
        return ""


def _strip_markdown(text: str) -> str:
    """Usuwa artefakty markdown, które modele OCR (Claude/Mistral) dodają mimo prośby
    o czysty tekst — generują fałszywe różnice A/B (np. jedna strona zwraca
    '[www.example.com](https://www.example.com)', druga zwykłe 'www.example.com'). Zachowuje
    pojedynczy '_' i '|', które bywają w kodach/jednostkach produktów."""
    if not text:
        return text
    # Linki/obrazy: [etykieta](url) → etykieta ; ![alt](url) → alt
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    # Znaczniki pogrubienia / kodu / przekreślenia
    text = re.sub(r"\*\*|__|~~|`|\*", "", text)
    # Hashe nagłówków na początku linii
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    # Pozostałe nawiasy kwadratowe owijające zwykły tekst
    text = text.replace("[", "").replace("]", "")
    return text


def _ftfy_fix(text: str) -> str:
    """Repair mojibake / mis-decoded Unicode from OCR (e.g. 'Â', 'Ã³' artifacts,
    broken diacritics, stray HTML entities) so the same printed content doesn't
    differ between A and B purely by encoding noise. No-op when ftfy is absent —
    cached per process so the import probe runs at most once."""
    if not text:
        return text
    global _HAS_FTFY, _FTFY
    if _HAS_FTFY is None:
        try:
            import ftfy as _f
            _FTFY, _HAS_FTFY = _f, True
        except Exception:
            _FTFY, _HAS_FTFY = None, False
    if not _HAS_FTFY:
        return text
    try:
        return _FTFY.fix_text(text)
    except Exception:
        return text


_HAS_FTFY = None
_FTFY = None


def _mistral_readtext(img) -> str:
    """Primary API-based OCR via Mistral OCR (mistral-ocr-latest).

    Requires MISTRAL_API_KEY env var. ~$0.001/page — fastest and most
    accurate option for printed medical packaging labels.
    Falls back silently so the cascade continues to GOT-OCR / PaddleOCR.
    """
    api_key = os.environ.get("MISTRAL_API_KEY", "")
    if not api_key:
        return ""
    try:
        import base64 as _b64, httpx as _hx
        from io import BytesIO
        w, h = img.size
        if max(w, h) > 2048:
            scale = 2048 / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=92)
        b64 = _b64.b64encode(buf.getvalue()).decode()
        resp = _hx.post(
            "https://api.mistral.ai/v1/ocr",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": "mistral-ocr-latest",
                  "document": {"type": "image_url",
                                "image_url": f"data:image/jpeg;base64,{b64}"}},
            timeout=30,
        )
        if resp.status_code != 200:
            logger.debug("Mistral OCR HTTP %s: %s", resp.status_code, resp.text[:200])
            return ""
        pages = resp.json().get("pages", [])
        text = "\n".join(p.get("markdown", "") for p in pages).strip()
        # Strip markdown (links, emphasis, headings) consistently with Claude so the
        # same content never differs only by formatting between A and B.
        text = _strip_markdown(text)
        text = re.sub(r"[ \t]{2,}", " ", text).strip()
        return text
    except Exception as exc:
        logger.debug("Mistral OCR failed: %s", exc)
        return ""


def _get_doctr_reader():
    """Lazy DocTR singleton — high-accuracy deep-learning OCR fallback."""
    global _doctr_reader
    if _doctr_reader is not None:
        return _doctr_reader or None
    with _doctr_lock:
        if _doctr_reader is not None:
            return _doctr_reader or None
        try:
            from doctr.models import ocr_predictor
            _doctr_reader = ocr_predictor(pretrained=True)
            logger.info("DocTR OCR reader initialised")
        except Exception as exc:
            logger.warning("DocTR unavailable: %s", exc)
            _doctr_reader = False
    return _doctr_reader or None


def _doctr_readtext(img) -> str:
    """Run DocTR OCR on a PIL image. Returns plain text."""
    reader = _get_doctr_reader()
    if not reader:
        return ""
    try:
        import numpy as np
        # DocTR predictor accepts numpy arrays (H,W,3) uint8 directly;
        # DocumentFile.from_images() only accepts file paths / bytes, NOT arrays.
        arr = np.array(img.convert("RGB"))
        result = reader([arr])
        lines = []
        for page in result.pages:
            for block in page.blocks:
                for line in block.lines:
                    lines.append(" ".join(w.value for w in line.words))
        return "\n".join(lines).strip()
    except Exception as exc:
        logger.debug("DocTR readtext failed: %s", exc)
        return ""


# ── Surya layout-aware OCR singleton ──────────────────────────────────────────

_surya_det_model      = None
_surya_det_processor  = None
_surya_rec_model      = None
_surya_rec_processor  = None
_surya_lock           = _threading.Lock()


def _get_surya():
    """Lazy Surya singleton — layout-aware OCR with reading-order detection."""
    global _surya_det_model, _surya_det_processor
    global _surya_rec_model, _surya_rec_processor
    if _surya_det_model is not None:
        return ((_surya_det_model, _surya_det_processor,
                 _surya_rec_model, _surya_rec_processor)
                if _surya_det_model else (None, None, None, None))
    with _surya_lock:
        if _surya_det_model is not None:
            return ((_surya_det_model, _surya_det_processor,
                     _surya_rec_model, _surya_rec_processor)
                    if _surya_det_model else (None, None, None, None))
        try:
            from surya.model.detection.segformer import (
                load_model as _sd_model, load_processor as _sd_proc)
            from surya.model.recognition.model import load_model as _sr_model
            from surya.model.recognition.processor import (
                load_processor as _sr_proc)
            _det_m               = _sd_model()   # load into temp var first
            _surya_det_processor = _sd_proc()
            _surya_rec_model     = _sr_model()
            _surya_rec_processor = _sr_proc()
            _surya_det_model     = _det_m        # sentinel set last — prevents TOCTOU race
            logger.info("Surya OCR models initialised")
        except Exception as exc:
            logger.warning("Surya unavailable: %s", exc)
            _surya_det_model = False
    return ((_surya_det_model, _surya_det_processor,
             _surya_rec_model, _surya_rec_processor)
            if _surya_det_model else (None, None, None, None))


def _surya_readtext(img) -> str:
    """Surya layout-aware OCR — detects reading order before recognition.
    Returns plain text lines joined by newlines."""
    det_model, det_processor, rec_model, rec_processor = _get_surya()
    if det_model is None:
        return ""
    try:
        from surya.ocr import run_ocr
        langs = [["pl", "en", "de", "fr", "es", "it", "pt", "nl",
                  "cs", "sk", "hu", "ro", "hr", "sl",
                  "et", "lv", "lt", "fi", "sv", "no", "da",
                  "uk", "ru", "bg"]]
        predictions = run_ocr(
            [img], langs,
            det_model, det_processor,
            rec_model, rec_processor,
        )
        if not predictions:
            return ""
        lines = [
            line.text for line in predictions[0].text_lines
            if line.text and line.text.strip()
        ]
        return "\n".join(lines).strip()
    except Exception as exc:
        logger.debug("Surya readtext failed: %s", exc)
        return ""


# ── DINOv2 visual similarity singleton ────────────────────────────────────────

_dinov2_model = None
_dinov2_processor = None
_dinov2_lock  = _threading.Lock()


def _get_dinov2():
    """Lazy DINOv2 ViT-S singleton for visual feature similarity."""
    global _dinov2_model, _dinov2_processor
    if _dinov2_model is not None:
        return (_dinov2_model, _dinov2_processor) if _dinov2_model else (None, None)
    with _dinov2_lock:
        if _dinov2_model is not None:
            return (_dinov2_model, _dinov2_processor) if _dinov2_model else (None, None)
        try:
            from transformers import AutoImageProcessor, AutoModel
            import torch
            # Bandit B615: stała nazwa zaufanego modelu z HF Hub (nie z wejścia); opcjonalny model ładowany leniwie.
            _dinov2_processor = AutoImageProcessor.from_pretrained("facebook/dinov2-small")  # nosec B615
            # Bandit B615: stała nazwa zaufanego modelu z HF Hub (nie z wejścia); opcjonalny model ładowany leniwie.
            _dinov2_model = AutoModel.from_pretrained("facebook/dinov2-small")  # nosec B615
            _dinov2_model.eval()
            logger.info("DINOv2 ViT-S model initialised (CPU)")
        except Exception as exc:
            logger.warning("DINOv2 unavailable: %s", exc)
            _dinov2_model = False
            _dinov2_processor = False
    return (_dinov2_model, _dinov2_processor) if _dinov2_model else (None, None)


def _dinov2_similarity(img_a, img_b) -> float:
    """Cosine similarity between DINOv2 CLS token embeddings. Returns 0–1."""
    model, processor = _get_dinov2()
    if model is None or processor is None:
        return -1.0
    try:
        import torch
        inputs = processor(images=[img_a, img_b], return_tensors="pt")
        with torch.no_grad():
            outputs = model(**inputs)
        emb = outputs.last_hidden_state[:, 0]   # CLS token
        emb = emb / emb.norm(dim=-1, keepdim=True)
        score = float((emb[0] * emb[1]).sum())
        return max(0.0, score)
    except Exception as exc:
        logger.debug("DINOv2 similarity failed: %s", exc)
        return -1.0


# ── Table Transformer singleton ───────────────────────────────────────────────

_table_transformer_model     = None
_table_transformer_processor = None
_table_transformer_lock      = _threading.Lock()


def _get_table_transformer():
    """Lazy Table Transformer singleton for size-table detection."""
    global _table_transformer_model, _table_transformer_processor
    if _table_transformer_model is not None:
        return ((_table_transformer_model, _table_transformer_processor)
                if _table_transformer_model else (None, None))
    with _table_transformer_lock:
        if _table_transformer_model is not None:
            return ((_table_transformer_model, _table_transformer_processor)
                    if _table_transformer_model else (None, None))
        try:
            from transformers import AutoImageProcessor, TableTransformerForObjectDetection
            # Bandit B615: stała nazwa zaufanego modelu z HF Hub (nie z wejścia); opcjonalny model ładowany leniwie.
            _table_transformer_processor = AutoImageProcessor.from_pretrained(  # nosec B615
                "microsoft/table-transformer-detection")
            # Bandit B615: stała nazwa zaufanego modelu z HF Hub (nie z wejścia); opcjonalny model ładowany leniwie.
            _table_transformer_model = TableTransformerForObjectDetection.from_pretrained(  # nosec B615
                "microsoft/table-transformer-detection")
            _table_transformer_model.eval()
            logger.info("Table Transformer model initialised")
        except Exception as exc:
            logger.warning("Table Transformer unavailable: %s", exc)
            _table_transformer_model = False
            _table_transformer_processor = None
    return ((_table_transformer_model, _table_transformer_processor)
            if _table_transformer_model else (None, None))


def _img2table_extract(crop_img) -> str:
    """Parse table structure from a crop image using img2table.

    Returns rows joined as 'cell1 | cell2 | ...' lines, or '' on failure.
    Preferred over raw OCR for size-table crops where cell boundaries matter.
    """
    try:
        import io as _io
        from img2table.document import Image as _Img2TableDoc
        from img2table.ocr import TesseractOCR as _Img2TableTess
        # img2table only accepts str/Path/BytesIO/bytes — not PIL Images
        _buf = _io.BytesIO()
        crop_img.convert("RGB").save(_buf, format="PNG")
        _buf.seek(0)
        doc = _Img2TableDoc(src=_buf)
        ocr = _Img2TableTess(lang="pol+eng+ukr+rus")
        tables = doc.extract_tables(
            ocr=ocr,
            borderless_tables=True,
            min_confidence=50,
        )
        if not tables:
            return ""
        parts = []
        for tbl in tables:
            df = tbl.df
            for _, row in df.iterrows():
                cells = [str(v).strip() for v in row.values
                         if v is not None and str(v).strip()]
                if cells:
                    parts.append(" | ".join(cells))
        return "\n".join(parts).strip()
    except ImportError:
        return ""
    except Exception as exc:
        logger.debug("img2table extract failed: %s", exc)
        return ""


def _table_cells_img2table(crop):
    """Zwraca komórki tabeli z wycinka jako [(row, col, value, (x1%,y1%,x2%,y2%)), …]
    przez img2table (struktura + bboxy). [] gdy img2table niedostępny/brak tabeli.
    Bboxy w % wycinka — mapują się na overlay pola, więc można zaznaczyć DOKŁADNĄ
    komórkę, która się różni."""
    if crop is None:
        return []
    try:
        import io as _io
        from img2table.document import Image as _Img2TableDoc
        from img2table.ocr import TesseractOCR as _Img2TableTess
        W, H = crop.size
        if not W or not H:
            return []
        _buf = _io.BytesIO()
        crop.convert("RGB").save(_buf, format="PNG")
        _buf.seek(0)
        doc = _Img2TableDoc(src=_buf)
        ocr = _Img2TableTess(lang=_get_ocr_langs())
        tables = doc.extract_tables(ocr=ocr, borderless_tables=True, min_confidence=50)
        if not tables:
            return []
        tbl = tables[0]   # najwyżej ocenioną/pierwszą tabelę
        out = []
        content = getattr(tbl, "content", None) or {}
        for r_idx, row_cells in content.items():
            for c_idx, cell in enumerate(row_cells or []):
                val = (getattr(cell, "value", None) or "")
                val = " ".join(str(val).split())
                bb = getattr(cell, "bbox", None)
                if bb is None:
                    continue
                x1, y1, x2, y2 = (getattr(bb, "x1", None), getattr(bb, "y1", None),
                                  getattr(bb, "x2", None), getattr(bb, "y2", None))
                if None in (x1, y1, x2, y2):
                    continue
                out.append((int(r_idx), int(c_idx), val,
                            (x1 / W * 100.0, y1 / H * 100.0, x2 / W * 100.0, y2 / H * 100.0)))
        return out
    except ImportError:
        return []
    except Exception as exc:
        logger.debug("img2table cells failed: %s", exc)
        return []


def _cell_values_differ(va, vb, tol: float = 0.0) -> bool:
    """Porównanie zawartości komórki: numerycznie gdy są liczby (z tolerancją),
    inaczej tekstowo (bez wielkości liter i nadmiarowych spacji)."""
    va = " ".join((va or "").split())
    vb = " ".join((vb or "").split())
    if va == vb:
        return False
    na = _extract_numbers_from_text(va)
    nb = _extract_numbers_from_text(vb)
    if na or nb:
        return _numeric_values_differ(na, nb, tol)
    return va.lower() != vb.lower()


def _compare_table_structured(crop_a, crop_b, tol: float = 0.0):
    """Porównanie tabeli KOMÓRKA-PO-KOMÓRCE (img2table). Wyrównuje siatki A/B po
    (wiersz, kolumna), porównuje wartości i zwraca DOKŁADNE boxy różniących się
    komórek po obu stronach. Zwraca dict {changed, boxes_a, boxes_b, note, diffs}
    albo None, gdy struktury nie udało się odzyskać po którejś stronie (wtedy
    caller wraca do boksowania po wartościach)."""
    if not _env_flag("ARTWORK_TABLE_STRUCT", "0"):   # domyślnie OFF — img2table (Tesseract)
        return None                                  # rywalizuje o CPU i wpychał POLE w timeout
    # UWAGA: img2table służy TYLKO do precyzyjnych RAMEK (detekcja i tak działa bez
    # niego — z diffu tekstu/liczb). Na serwerze produkcyjnym jego Tesseract spowalniał
    # CAŁE pole ponad limit czasu (tabela = duży wycinek), przez co pole timeoutowało
    # i pokazywało critical-timeout zamiast policzyć różnicę. Dlatego domyślnie WYŁĄCZONY.
    # Ramki robi value-boxing (szybki, celowany) + weryfikacja pikselowa. img2table można
    # włączyć opcjonalnie (ARTWORK_TABLE_STRUCT=1) na mocniejszym serwerze.
    # Budżet czasu (ARTWORK_TABLE_STRUCT_TIMEOUT, domyślnie 18 s / 9 s) — gdy się nie
    # wyrobi, pomijamy (None). ADDYTYWNIE — nigdy nie zmienia werdyktu pola.
    try:
        _to = int(os.environ.get("ARTWORK_TABLE_STRUCT_TIMEOUT", "18"))
    except ValueError:
        _to = 18
    # Uwaga: NIE używamy 'with' — jego __exit__ robi shutdown(wait=True), co
    # blokowałoby do końca wolnych wątków mimo timeoutu. shutdown(wait=False)
    # zwraca od razu (wątek dokończy img2table w tle i zniknie).
    _tex = ThreadPoolExecutor(max_workers=2)
    try:
        _fa = _tex.submit(_table_cells_img2table, crop_a)
        _fb = _tex.submit(_table_cells_img2table, crop_b)
        cells_a = _fa.result(timeout=_to)
        cells_b = _fb.result(timeout=max(1, _to // 2))
    except Exception as _te:
        logger.debug("img2table structural timed out/failed: %s", _te)
        _tex.shutdown(wait=False)
        return None
    _tex.shutdown(wait=False)
    if not cells_a or not cells_b:
        return None
    map_a = {(r, c): (v, bb) for r, c, v, bb in cells_a}
    map_b = {(r, c): (v, bb) for r, c, v, bb in cells_b}
    boxes_a, boxes_b, diffs = [], [], []
    for key in sorted(set(map_a) | set(map_b)):
        va, ba = map_a.get(key, ("", None))
        vb, bb = map_b.get(key, ("", None))
        if _cell_values_differ(va, vb, tol):
            if ba:
                boxes_a.append(tuple(ba))
            if bb:
                boxes_b.append(tuple(bb))
            diffs.append(f"{va or '∅'}→{vb or '∅'}")
    return {
        "changed": bool(diffs),
        "boxes_a": _dedupe_pct_boxes(boxes_a),
        "boxes_b": _dedupe_pct_boxes(boxes_b),
        "note": (f"Tabela (komórki): {len(diffs)} różnic — " + ", ".join(diffs[:8])
                 + (" …" if len(diffs) > 8 else "")) if diffs else "Tabela (komórki): zgodne",
        "diffs": diffs,
    }


def _detect_tables_transformer(img) -> list:
    """Detect table regions using Table Transformer.

    Returns list of {x1_pct, y1_pct, x2_pct, y2_pct, score} sorted by score desc.
    Falls back to [] when the model is unavailable or no tables are found.
    """
    model, processor = _get_table_transformer()
    if model is None or processor is None:
        return []
    try:
        import torch
        W, H = img.size
        inputs = processor(images=img, return_tensors="pt")
        with torch.no_grad():
            outputs = model(**inputs)
        target_sizes = torch.tensor([[H, W]])
        results = processor.post_process_object_detection(
            outputs, threshold=0.7, target_sizes=target_sizes)[0]
        tables = []
        for score, box in zip(results["scores"], results["boxes"]):
            x1, y1, x2, y2 = box.tolist()
            tables.append({
                "x1_pct": round(x1 / W * 100, 2),
                "y1_pct": round(y1 / H * 100, 2),
                "x2_pct": round(x2 / W * 100, 2),
                "y2_pct": round(y2 / H * 100, 2),
                "score":  round(float(score), 3),
            })
        tables.sort(key=lambda t: t["score"], reverse=True)
        return tables
    except Exception as exc:
        logger.debug("Table Transformer detection failed: %s", exc)
        return []


# ── LightGlue + SuperPoint singletons ─────────────────────────────────────────

_lightglue_extractor = None
_lightglue_matcher   = None
_lightglue_lock      = _threading.Lock()


def _get_lightglue():
    """Lazy LightGlue + SuperPoint singleton for learned feature matching."""
    global _lightglue_extractor, _lightglue_matcher
    if _lightglue_extractor is not None:
        return ((_lightglue_extractor, _lightglue_matcher)
                if _lightglue_extractor else (None, None))
    with _lightglue_lock:
        if _lightglue_extractor is not None:
            return ((_lightglue_extractor, _lightglue_matcher)
                    if _lightglue_extractor else (None, None))
        try:
            import torch
            from lightglue import SuperPoint, LightGlue
            _lightglue_extractor = SuperPoint(max_num_keypoints=1024).eval()
            _lightglue_matcher   = LightGlue(features="superpoint").eval()
            logger.info("LightGlue + SuperPoint initialised (CPU)")
        except Exception as exc:
            logger.warning("LightGlue unavailable: %s", exc)
            _lightglue_extractor = False
            _lightglue_matcher   = None
    return ((_lightglue_extractor, _lightglue_matcher)
            if _lightglue_extractor else (None, None))


def warmup_models(verbose: bool = True) -> None:
    """Eagerly initialise the heavy CPU models on the alignment / visual-similarity
    hot path (DINOv2 + LightGlue) so the first real comparison doesn't pay the
    cold-start (observed ~10 s+ in production logs).

    Safe to call from a background daemon thread at startup: each loader is a
    cached singleton guarding its own try/except, so a failure here is non-fatal
    and simply leaves that model to lazy-load on demand as before. OCR fallback
    models (EasyOCR's 25-language pack, Paddle) are deliberately NOT warmed — they
    are slow to load and only used when the primary API OCR is unavailable.
    """
    for _name, _fn in (("DINOv2", _get_dinov2), ("LightGlue", _get_lightglue)):
        try:
            _s = time.time()
            _fn()
            if verbose:
                logger.info("warmup: %s gotowy w %.1fs", _name, time.time() - _s)
        except Exception as _e:
            logger.warning("warmup: %s nie wystartował: %s", _name, _e)


def _lightglue_match_locate(tmpl_gray, page_gray, ph: int, pw: int,
                              th: int, tw: int):
    """LightGlue + SuperPoint matching. Returns same format as _feature_match_locate."""
    extractor, matcher = _get_lightglue()
    if extractor is None:
        return None
    try:
        import torch
        import numpy as np
        from lightglue.utils import numpy_image_to_torch, rbd

        def _to_tensor(gray_arr):
            # LightGlue expects float32 [0,1] normalised
            return numpy_image_to_torch(gray_arr.astype(np.float32) / 255.0)

        with torch.no_grad():
            feats0 = extractor.extract(_to_tensor(tmpl_gray))
            feats1 = extractor.extract(_to_tensor(page_gray))
            matches01 = matcher({"image0": feats0, "image1": feats1})

        feats0, feats1, matches01 = [rbd(x) for x in [feats0, feats1, matches01]]
        matches = matches01["matches"]          # (N, 2)
        if matches.shape[0] < 8:
            return None

        kpts0 = feats0["keypoints"][matches[:, 0]].cpu().numpy()  # (N, 2)
        kpts1 = feats1["keypoints"][matches[:, 1]].cpu().numpy()

        import cv2
        src_pts = kpts0.reshape(-1, 1, 2).astype(np.float32)
        dst_pts = kpts1.reshape(-1, 1, 2).astype(np.float32)
        M, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)
        if M is None:
            return None
        inliers = int(mask.ravel().sum())
        if inliers < 8:
            return None

        corners = np.float32([[0, 0], [tw, 0], [tw, th], [0, th]]).reshape(-1, 1, 2)
        dst = cv2.perspectiveTransform(corners, M)
        xs, ys = dst[:, 0, 0], dst[:, 0, 1]
        x1 = int(max(0,  xs.min()))
        y1 = int(max(0,  ys.min()))
        x2 = int(min(pw, xs.max()))
        y2 = int(min(ph, ys.max()))
        if x2 <= x1 or y2 <= y1:
            return None
        area_ratio = ((x2 - x1) * (y2 - y1)) / max(tw * th, 1)
        if not (0.25 < area_ratio < 4.0):
            return None

        confidence = min(0.97, 0.55 + inliers * 0.04)
        logger.debug("LightGlue: %d inliers conf=%.2f", inliers, confidence)
        return ({"x1_pct": x1/pw*100, "y1_pct": y1/ph*100,
                 "x2_pct": x2/pw*100, "y2_pct": y2/ph*100},
                confidence)
    except Exception as exc:
        logger.debug("LightGlue matching error: %s", exc)
        return None


def _get_paddleocr(script: str = "latin"):
    """Lazy PaddleOCR singleton per script family (latin | cyrillic)."""
    global _paddleocr_latin, _paddleocr_cyrillic
    slot = "_paddleocr_latin" if script == "latin" else "_paddleocr_cyrillic"
    current = _paddleocr_latin if script == "latin" else _paddleocr_cyrillic
    if current is not None:
        return current or None
    with _paddleocr_lock:
        current = _paddleocr_latin if script == "latin" else _paddleocr_cyrillic
        if current is not None:
            return current or None
        try:
            from paddleocr import PaddleOCR
            reader = PaddleOCR(
                use_angle_cls=True,
                lang=script,
                use_gpu=False,
                show_log=False,
            )
            if script == "latin":
                _paddleocr_latin = reader
            else:
                _paddleocr_cyrillic = reader
            logger.info("PaddleOCR reader initialised (script=%s)", script)
            return reader
        except Exception as exc:
            logger.warning("PaddleOCR (%s) unavailable: %s", script, exc)
            if script == "latin":
                _paddleocr_latin = False
            else:
                _paddleocr_cyrillic = False
            return None


def _get_easyocr_reader():
    """Lazy EasyOCR singleton — fallback for mixed-language and difficult crops.

    EU medical packaging carries 20-30 languages. EasyOCR groups languages by
    script, so all Latin-script EU languages share one base model — adding them
    costs only a small language-model download, not a full new CNN model.
    """
    global _easyocr_reader
    if _easyocr_reader is not None:
        return _easyocr_reader or None
    with _easyocr_lock:
        if _easyocr_reader is not None:
            return _easyocr_reader or None
        # All Latin-script EU languages + Cyrillic (UK/RU/BG).
        # "bs" (Bosnian) was added in EasyOCR 1.6.2 — try full list first,
        # fall back progressively to a known-safe minimal set so that one
        # unsupported language code never disables the entire EasyOCR tier.
        _EASYOCR_LANGS_FULL = [
            "pl", "en", "de", "fr", "es", "it", "pt", "nl",
            "cs", "sk", "hu", "ro", "hr", "sl", "bs",
            "et", "lv", "lt",
            "fi", "sv", "no", "da",
            "uk", "ru", "bg",
        ]
        _EASYOCR_LANGS_SAFE = ["pl", "en", "de", "fr", "es", "it",
                                "cs", "ro", "uk", "ru"]
        try:
            import easyocr
            for _lang_set in (_EASYOCR_LANGS_FULL, _EASYOCR_LANGS_SAFE):
                try:
                    _easyocr_reader = easyocr.Reader(
                        _lang_set, gpu=False, verbose=False
                    )
                    logger.info("EasyOCR reader initialised (%d languages, CPU)",
                                len(_lang_set))
                    break
                except Exception as _inner:
                    logger.warning("EasyOCR init failed with %d langs (%s), retrying with safe set",
                                   len(_lang_set), _inner)
                    _easyocr_reader = None
            if _easyocr_reader is None:
                _easyocr_reader = False
        except Exception as exc:
            logger.warning("EasyOCR unavailable: %s", exc)
            _easyocr_reader = False
    return _easyocr_reader or None


def _paddle_readtext(img, use_cls: bool = True) -> str:
    """Run PaddleOCR on a PIL image. Tries latin then cyrillic script.
    use_cls=False disables the per-line angle classifier — use when the caller
    has already corrected orientation (e.g. user-defined rotation) and must not
    let PaddleOCR re-flip correctly-oriented lines."""
    import numpy as np
    arr = np.array(img.convert("RGB"))
    for script in ("latin", "cyrillic"):
        reader = _get_paddleocr(script)
        if not reader:
            continue
        try:
            result = reader.ocr(arr, cls=use_cls)
            if not result or not result[0]:
                continue
            texts = [line[1][0] for line in result[0]
                     if line and len(line) >= 2 and line[1][1] > 0.3]
            text = " ".join(texts).strip()
            if text:
                return text
        except Exception as exc:
            logger.debug("PaddleOCR (%s) readtext failed: %s", script, exc)
    return ""


def _align_crop_to_master(crop_master, crop_supplier) -> tuple:
    """Register supplier crop to master crop using SIFT + RANSAC homography.

    Handles labels printed at different scales (e.g. 500x600 mm vs 451x464 mm)
    by warping the supplier into the master's coordinate space. After alignment
    pixel-by-pixel comparison and SSIM become meaningful.

    Returns: (aligned_supplier_PIL, confidence_0_to_1)
    When alignment fails, falls back to a plain Lanczos resize to master's size.
    """
    if crop_master is None or crop_supplier is None:
        return crop_supplier, 0.0
    try:
        import cv2
        import numpy as np
        from PIL import Image
        lanczos = Image.LANCZOS if hasattr(Image, "LANCZOS") else Image.BICUBIC

        # Bound alignment cost: SIFT/LightGlue/ECC on full-resolution crops
        # (profile crops render at 360 DPI) can take tens of seconds per field and
        # blow the request timeout — the UI spinner then hangs forever. Estimate
        # registration on a downscaled copy; pix_sim/SSIM resize internally so
        # accuracy is unaffected, and the warp stays usable at the (downscaled)
        # master size. Disable with ARTWORK_ALIGN_MAX_PX=0.
        try:
            _align_cap = int(os.environ.get("ARTWORK_ALIGN_MAX_PX", "1400"))
        except ValueError:
            _align_cap = 1400
        if _align_cap > 0:
            _amax = max(crop_master.width, crop_master.height,
                        crop_supplier.width, crop_supplier.height)
            if _amax > _align_cap:
                _asc = _align_cap / _amax
                crop_master = crop_master.resize(
                    (max(1, int(crop_master.width * _asc)), max(1, int(crop_master.height * _asc))), lanczos)
                crop_supplier = crop_supplier.resize(
                    (max(1, int(crop_supplier.width * _asc)), max(1, int(crop_supplier.height * _asc))), lanczos)

        a_gray = np.array(crop_master.convert("L"))
        b_gray = np.array(crop_supplier.convert("L"))
        h, w = a_gray.shape

        # Tiny crops: SIFT needs keypoints — use phase correlation (FFT-based
        # translation) + ECC refinement instead of feature matching.
        if min(a_gray.shape) < 80 or min(b_gray.shape) < 80:
            b_rs = np.array(
                crop_supplier.resize(crop_master.size, lanczos).convert("L"),
                dtype=np.float32,
            )
            a_f = a_gray.astype(np.float32)
            try:
                (dx, dy), _ = cv2.phaseCorrelate(a_f, b_rs)
                M_shift = np.float32([[1, 0, dx], [0, 1, dy]])
                b_rgb = np.array(crop_supplier.resize(crop_master.size, lanczos).convert("RGB"))
                warped = cv2.warpAffine(
                    b_rgb, M_shift, (w, h),
                    borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255),
                )
                return Image.fromarray(warped), 0.4
            except Exception:
                return crop_supplier.resize(crop_master.size, lanczos), 0.1

        # ── Tier 0: LightGlue + SuperPoint ────────────────────────────────
        # Learned matcher — significantly more keypoints on text/medical packaging
        # than SIFT; works on clean backgrounds where SIFT finds nothing.
        extractor_lg, matcher_lg = _get_lightglue()
        if extractor_lg is not None:
            try:
                import torch
                from lightglue.utils import numpy_image_to_torch, rbd

                b_gray_rs = np.array(
                    crop_supplier.resize(crop_master.size, lanczos).convert("L")
                )
                a_t = numpy_image_to_torch(a_gray.astype(np.float32) / 255.0)
                b_t = numpy_image_to_torch(b_gray_rs.astype(np.float32) / 255.0)

                with torch.no_grad():
                    feats0 = extractor_lg.extract(a_t)  # master
                    feats1 = extractor_lg.extract(b_t)  # supplier (resized to master)
                    m01 = matcher_lg({"image0": feats0, "image1": feats1})

                feats0, feats1, m01 = [rbd(x) for x in [feats0, feats1, m01]]
                matches = m01["matches"]  # (N, 2) — indices into feats0 / feats1

                if matches.shape[0] >= 8:
                    kp_a = feats0["keypoints"][matches[:, 0]].cpu().numpy()
                    kp_b = feats1["keypoints"][matches[:, 1]].cpu().numpy()
                    src_pts_lg = kp_b.reshape(-1, 1, 2).astype(np.float32)
                    dst_pts_lg = kp_a.reshape(-1, 1, 2).astype(np.float32)

                    use_aff_lg = min(h, w) < 200
                    if use_aff_lg:
                        M_lg, mask_lg = cv2.estimateAffinePartial2D(
                            src_pts_lg, dst_pts_lg, method=cv2.RANSAC,
                            ransacReprojThreshold=4.0,
                        )
                    else:
                        M_lg, mask_lg = cv2.findHomography(
                            src_pts_lg, dst_pts_lg, cv2.RANSAC, 4.0,
                        )

                    if M_lg is not None:
                        inliers_lg = int(mask_lg.ravel().sum()) if mask_lg is not None else 0
                        if inliers_lg >= 8:
                            b_rgb_rs = np.array(
                                crop_supplier.resize(crop_master.size, lanczos).convert("RGB")
                            )
                            warp_kw = dict(
                                borderMode=cv2.BORDER_CONSTANT,
                                borderValue=(255, 255, 255),
                            )
                            warped_lg = (
                                cv2.warpAffine(b_rgb_rs, M_lg, (w, h), **warp_kw)
                                if use_aff_lg
                                else cv2.warpPerspective(b_rgb_rs, M_lg, (w, h), **warp_kw)
                            )
                            conf_lg = min(1.0, inliers_lg / 20.0)
                            # ECC sub-pixel refinement on LightGlue result
                            try:
                                warped_g_lg = cv2.cvtColor(warped_lg, cv2.COLOR_RGB2GRAY).astype(np.float32)
                                M_ecc_lg = np.eye(2, 3, dtype=np.float32)
                                criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 50, 1e-4)
                                _, M_ecc_lg = cv2.findTransformECC(
                                    a_gray.astype(np.float32), warped_g_lg,
                                    M_ecc_lg, cv2.MOTION_TRANSLATION, criteria,
                                )
                                warped_lg = cv2.warpAffine(warped_lg, M_ecc_lg, (w, h), **warp_kw)
                                conf_lg = min(1.0, conf_lg + 0.1)
                            except Exception:
                                pass
                            logger.debug(
                                "LightGlue crop alignment: %d inliers conf=%.2f",
                                inliers_lg, conf_lg,
                            )
                            return Image.fromarray(warped_lg), conf_lg
            except Exception as exc:
                logger.debug("LightGlue crop alignment failed: %s", exc)

        # ── Tier 1: SIFT (classic feature matching) ───────────────────────
        sift = cv2.SIFT_create()
        kp1, des1 = sift.detectAndCompute(a_gray, None)
        kp2, des2 = sift.detectAndCompute(b_gray, None)

        # Try AKAZE as fallback when SIFT finds too few features
        if des1 is None or des2 is None or len(kp1) < 8 or len(kp2) < 8:
            akaze = cv2.AKAZE_create()
            kp1, des1 = akaze.detectAndCompute(a_gray, None)
            kp2, des2 = akaze.detectAndCompute(b_gray, None)
            norm = cv2.NORM_HAMMING
            ratio_thr = 0.80
        else:
            norm = cv2.NORM_L2
            ratio_thr = 0.75

        if des1 is None or des2 is None or len(kp1) < 8 or len(kp2) < 8:
            return crop_supplier.resize(crop_master.size, lanczos), 0.1

        bf = cv2.BFMatcher(norm)
        raw = bf.knnMatch(des1, des2, k=2)
        good = [p[0] for p in raw if len(p) >= 2 and p[0].distance < ratio_thr * p[1].distance]
        if len(good) < 8:
            return crop_supplier.resize(crop_master.size, lanczos), 0.1

        src_pts = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
        dst_pts = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)

        # Small crops (< 200px): affine (4 DOF) is more stable than full
        # homography (8 DOF) when there are few inliers.
        use_affine = min(h, w) < 200
        if use_affine:
            M_aff, mask = cv2.estimateAffinePartial2D(
                src_pts, dst_pts, method=cv2.RANSAC, ransacReprojThreshold=5.0,
            )
            M = M_aff
            warp_fn = lambda src, mat, sz: cv2.warpAffine(
                src, mat, sz,
                borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255),
            )
        else:
            M, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)
            warp_fn = lambda src, mat, sz: cv2.warpPerspective(
                src, mat, sz,
                borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255),
            )

        if M is None:
            return crop_supplier.resize(crop_master.size, lanczos), 0.1
        inliers = int(mask.ravel().sum()) if mask is not None else 0
        if inliers < 6:
            return crop_supplier.resize(crop_master.size, lanczos), 0.1

        # Warp supplier RGB into master dimensions
        b_rgb = np.array(crop_supplier.convert("RGB"))
        warped = warp_fn(b_rgb, M, (w, h))
        confidence = min(1.0, inliers / 25.0)

        # ECC sub-pixel refinement — tightens alignment by ≈ 0.5–2 px using
        # intensity correlation, significantly reducing false pixel-diff boxes.
        try:
            warped_gray = cv2.cvtColor(warped, cv2.COLOR_RGB2GRAY).astype(np.float32)
            a_f = a_gray.astype(np.float32)
            criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 50, 1e-4)
            M_ecc = np.eye(2, 3, dtype=np.float32)
            _, M_ecc = cv2.findTransformECC(
                a_f, warped_gray, M_ecc, cv2.MOTION_TRANSLATION, criteria,
            )
            warped = cv2.warpAffine(
                warped, M_ecc, (w, h),
                borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255),
            )
            confidence = min(1.0, confidence + 0.1)
            logger.debug("ECC refinement applied")
        except Exception:
            pass  # ECC can fail on featureless crops; coarse alignment is still usable

        logger.debug("Crop aligned (%d inliers, conf=%.2f)", inliers, confidence)
        return Image.fromarray(warped), confidence

    except Exception as exc:
        logger.debug("_align_crop_to_master SIFT/ECC failed: %s — trying template matching", exc)

    # ── Tier 2: multi-scale template matching ─────────────────────────────
    # Fallback for uniform/low-texture crops where feature matching has too
    # few keypoints (e.g. solid-color EAN bar, CE mark on white background).
    # Tries supplier at scales 0.80–1.20 × and finds the best NCC position.
    try:
        import cv2
        import numpy as np
        from PIL import Image
        lanczos = Image.LANCZOS if hasattr(Image, "LANCZOS") else Image.BICUBIC

        a_rgb = np.array(crop_master.convert("RGB"))
        a_gray_tm = cv2.cvtColor(a_rgb, cv2.COLOR_RGB2GRAY)
        ha, wa = a_gray_tm.shape

        b_rgb_orig = np.array(crop_supplier.convert("RGB"))
        b_gray_orig = cv2.cvtColor(b_rgb_orig, cv2.COLOR_RGB2GRAY)

        best_val, best_scale, best_loc = -1.0, 1.0, (0, 0)
        for scale in np.linspace(0.80, 1.20, 9):
            h_s = max(10, int(b_gray_orig.shape[0] * scale))
            w_s = max(10, int(b_gray_orig.shape[1] * scale))
            b_s = cv2.resize(b_gray_orig, (w_s, h_s))
            if h_s > ha or w_s > wa:
                continue
            res = cv2.matchTemplate(a_gray_tm, b_s, cv2.TM_CCOEFF_NORMED)
            _, mv, _, ml = cv2.minMaxLoc(res)
            if mv > best_val:
                best_val, best_scale, best_loc = mv, scale, ml

        if best_val >= 0.50:
            h_s = int(b_gray_orig.shape[0] * best_scale)
            w_s = int(b_gray_orig.shape[1] * best_scale)
            b_scaled = cv2.resize(b_rgb_orig, (w_s, h_s))
            tx, ty = best_loc
            canvas = np.full((ha, wa, 3), 255, dtype=np.uint8)
            y2 = min(ha, ty + h_s)
            x2 = min(wa, tx + w_s)
            canvas[ty:y2, tx:x2] = b_scaled[:y2 - ty, :x2 - tx]
            conf_tm = float(best_val) * 0.75  # conservative — NCC can overfit
            logger.debug(
                "Template-match crop alignment: scale=%.2f NCC=%.3f conf=%.2f",
                best_scale, best_val, conf_tm,
            )
            return Image.fromarray(canvas), conf_tm
    except Exception as exc2:
        logger.debug("Template match fallback failed: %s", exc2)

    # ── Final fallback: plain resize ──────────────────────────────────────
    try:
        from PIL import Image
        lanczos = Image.LANCZOS if hasattr(Image, "LANCZOS") else Image.BICUBIC
        return crop_supplier.resize(crop_master.size, lanczos), 0.0
    except Exception:
        return crop_supplier, 0.0


def _ssim_diff_overlay(img_a, img_b, dissim_threshold: float = 0.35):
    """Build a difference overlay using SSIM (Structural Similarity).

    SSIM is robust against brightness/contrast changes and focuses on structural
    differences — the right metric for printed materials where ink density may
    vary slightly between prints but the content is what matters.

    Both images must be the same size — call _align_crop_to_master first.
    Returns: PIL image (master with red tint where dissimilar) or None.
    """
    if img_a is None or img_b is None:
        return None
    try:
        import numpy as np
        from PIL import Image
        from skimage.metrics import structural_similarity as ssim

        if img_a.size != img_b.size:
            lanczos = Image.LANCZOS if hasattr(Image, "LANCZOS") else Image.BICUBIC
            img_b = img_b.resize(img_a.size, lanczos)

        a_gray = np.array(img_a.convert("L"))
        b_gray = np.array(img_b.convert("L"))

        # Adaptive window: scales with crop size — small crops need small window,
        # large crops benefit from larger context (max 11).
        min_dim = min(a_gray.shape[0], a_gray.shape[1])
        win_size = max(3, min(11, min_dim // 8))
        if win_size % 2 == 0:
            win_size -= 1
        if win_size < 3:
            return _pixel_diff_overlay(img_a, img_b)

        _score, diff_map = ssim(a_gray, b_gray, full=True, data_range=255,
                                 win_size=win_size)
        dissim = np.clip(1.0 - diff_map, 0.0, 1.0)

        # Gradient component: Sobel edge difference highlights ink/content changes
        # while ignoring uniform brightness/contrast shifts (print density variation).
        # Combined with SSIM this gives fewer false positives on text crops.
        try:
            import cv2 as _cv2
            sx_a = _cv2.Sobel(a_gray, _cv2.CV_32F, 1, 0, ksize=3)
            sy_a = _cv2.Sobel(a_gray, _cv2.CV_32F, 0, 1, ksize=3)
            sx_b = _cv2.Sobel(b_gray, _cv2.CV_32F, 1, 0, ksize=3)
            sy_b = _cv2.Sobel(b_gray, _cv2.CV_32F, 0, 1, ksize=3)
            grad_a = np.sqrt(sx_a ** 2 + sy_a ** 2)
            grad_b = np.sqrt(sx_b ** 2 + sy_b ** 2)
            max_grad = max(float(grad_a.max()), float(grad_b.max()), 1.0)
            grad_diff = np.abs(grad_a - grad_b) / max_grad
            # Weight: 60% SSIM, 40% gradient — blended dissimilarity
            dissim = np.clip(0.6 * dissim + 0.4 * grad_diff, 0.0, 1.0)
        except Exception:
            pass  # Fall back to pure SSIM if cv2 not available

        mask = dissim > dissim_threshold
        if not mask.any():
            return None

        # Build overlay: blend A+B, then tint red on dissimilar regions
        a_rgb = np.array(img_a.convert("RGB")).astype(np.float32)
        b_rgb = np.array(img_b.convert("RGB")).astype(np.float32)
        blend = ((a_rgb + b_rgb) / 2).astype(np.int16)
        # Strength of tint scales with dissimilarity
        strength = (dissim * 255).astype(np.int16)
        blend[..., 0] = np.clip(blend[..., 0] + (strength * mask), 0, 255)
        blend[..., 1] = np.clip(blend[..., 1] - (strength // 2 * mask), 0, 255)
        blend[..., 2] = np.clip(blend[..., 2] - (strength // 2 * mask), 0, 255)
        return Image.fromarray(blend.astype(np.uint8))
    except Exception as exc:
        logger.debug("_ssim_diff_overlay failed: %s", exc)
        return _pixel_diff_overlay(img_a, img_b)


def _annotate_crop_with_boxes(img, boxes, color=(220, 38, 38), line_width=None):
    """Draw colored rectangles on a PIL image. boxes: list of (x1_pct,y1_pct,x2_pct,y2_pct).

    line_width=None → resolution-proportional thickness. The previous flat 1-px
    line rendered faint after the crop was downscaled for display; this scales
    the outline with the crop size (~25% bolder) so red boxes stay clearly
    visible regardless of the source PDF resolution."""
    if not boxes or img is None:
        return img
    try:
        from PIL import ImageDraw
        out = img.copy().convert("RGB")
        draw = ImageDraw.Draw(out)
        w, h = out.size
        if line_width is None:
            line_width = max(2, round(max(w, h) * 0.002))
        for box in boxes:
            x1_pct, y1_pct, x2_pct, y2_pct = box
            px1 = max(0, int(x1_pct / 100 * w))
            py1 = max(0, int(y1_pct / 100 * h))
            px2 = min(w - 1, int(x2_pct / 100 * w))
            py2 = min(h - 1, int(y2_pct / 100 * h))
            for i in range(line_width):
                draw.rectangle([px1 - i, py1 - i, px2 + i, py2 + i], outline=color)
        return out
    except Exception as exc:
        logger.debug("annotate_crop_with_boxes failed: %s", exc)
        return img


def _pixel_diff_overlay(img_a, img_b, threshold: int = 20):
    """Blend A and B 50/50; tint orange where per-channel diff > threshold.

    Returns a PIL image showing both images superimposed with difference areas
    highlighted in orange — the '3rd image' for visual comparison.
    """
    if img_a is None or img_b is None:
        return None
    try:
        import numpy as np
        from PIL import Image
        lanczos = Image.LANCZOS if hasattr(Image, "LANCZOS") else Image.BICUBIC
        w = max(img_a.width, img_b.width)
        h = max(img_a.height, img_b.height)
        a = img_a.convert("RGB").resize((w, h), lanczos)
        b = img_b.convert("RGB").resize((w, h), lanczos)
        arr_a = np.array(a, dtype=np.float32)
        arr_b = np.array(b, dtype=np.float32)
        diff = np.abs(arr_a - arr_b).max(axis=2)  # max channel diff per pixel
        blend = ((arr_a + arr_b) / 2).astype(np.uint8)
        mask = diff > threshold
        if not mask.any():
            return None  # No meaningful difference — skip overlay
        # Tint changed pixels orange: boost red, suppress green/blue
        blend_f = blend.astype(np.int16)
        blend_f[mask, 0] = np.clip(blend_f[mask, 0] + 110, 0, 255)
        blend_f[mask, 1] = np.clip(blend_f[mask, 1] - 50, 0, 255)
        blend_f[mask, 2] = np.clip(blend_f[mask, 2] - 50, 0, 255)
        return Image.fromarray(blend_f.astype(np.uint8))
    except Exception as exc:
        logger.debug("pixel_diff_overlay failed: %s", exc)
        return None


def _pixel_diff_boxes(img_a, img_b, thresh: int = 22,
                      min_area: int = 20, strip_ratio: int = 10,
                      max_coverage: float = 0.30, max_boxes: int = 8,
                      de_thresh: float = 7.0) -> tuple:
    """Locate visually changed regions between two crop images via pixel diff.

    Returns (boxes_a, boxes_b) where each box is (x1_pct, y1_pct, x2_pct, y2_pct).
    Both sides receive the same %-coords (same field region, similar layout).
    Used as a fallback when OCR word-level boxes are unavailable or empty.

    Safety guards:
    - max_coverage: if more than this fraction of pixels differ the images have
      completely different layouts; drawing boxes everywhere is useless → return []
    - max_boxes: keep only the largest N regions to avoid cluttering the image
    - 2-px dilation before labeling merges adjacent micro-regions into fewer boxes
    """
    if not HAS_PIL:
        return [], []
    try:
        lanczos = Image.LANCZOS if hasattr(Image, "LANCZOS") else Image.BICUBIC
        W = max(img_a.width, img_b.width)
        H = max(img_a.height, img_b.height)
        a_rgb = img_a.convert("RGB").resize((W, H), lanczos)
        b_rgb = img_b.convert("RGB").resize((W, H), lanczos)

        # CIEDE2000 ΔE diff — the most perceptually accurate colour metric.
        # Falls back to CIE76 (cv2 Euclidean Lab) then max-channel RGB.
        try:
            from skimage.color import deltaE_ciede2000, rgb2lab as _sk_rgb2lab
            a_arr = np.array(a_rgb, dtype=np.float32) / 255.0
            b_arr = np.array(b_rgb, dtype=np.float32) / 255.0
            a_lab = _sk_rgb2lab(a_arr)
            b_lab = _sk_rgb2lab(b_arr)
            delta_e = deltaE_ciede2000(a_lab, b_lab)
            # ΔE00 ≈ 1 is "just noticeable"; 7.0 eliminates PDF rendering noise
            # (ink density / JPEG compression produces ΔE 3-6 on identical text)
            binary = delta_e > de_thresh
        except Exception:
            try:
                import cv2 as _cv2
                a_lab = _cv2.cvtColor(np.array(a_rgb, dtype=np.uint8), _cv2.COLOR_RGB2Lab).astype(np.float32)
                b_lab = _cv2.cvtColor(np.array(b_rgb, dtype=np.uint8), _cv2.COLOR_RGB2Lab).astype(np.float32)
                delta_e = np.sqrt(np.sum((a_lab - b_lab) ** 2, axis=2))
                lab_thresh = max(6.0, thresh * 0.45)
                binary = delta_e > lab_thresh
            except Exception:
                a = np.array(a_rgb, dtype=np.int16)
                b = np.array(b_rgb, dtype=np.int16)
                binary = np.abs(a - b).max(axis=2) > thresh

        if not binary.any():
            return [], []

        # Guard: if most of the image is different, boxes would cover everything.
        # This happens when master/supplier have completely different layouts.
        coverage = float(binary.mean())
        if coverage > max_coverage:
            return [], []

        if not HAS_SCIPY:
            ys, xs = np.where(binary)
            n_diff = int(xs.size)
            # Minimum znaczącej różnicy (odpowiednik min_area ze ścieżki scipy).
            _min_px = max(min_area, int(W * H * 0.0003))
            if n_diff < _min_px:
                return [], []
            x1, y1 = int(xs.min()), int(ys.min())
            x2, y2 = int(xs.max()), int(ys.max())
            box_area = max(1, (x2 - x1 + 1) * (y2 - y1 + 1))
            # Gdy różniące piksele są rozproszone (mała gęstość w ramce obejmującej
            # dużą część kadru), nie da się ich zlokalizować — pomiń, zamiast rysować
            # box na całość (dwa piksele szumu w rogach dawały box na cały crop).
            if box_area / float(W * H) > max_coverage and n_diff / box_area < 0.05:
                return [], []
            box = (float(x1 / W * 100), float(y1 / H * 100),
                   float(x2 / W * 100), float(y2 / H * 100))
            return [box], [box]

        # Adaptive min_area: 0.03% of crop area — requires a meaningful cluster
        # of different pixels, not just rendering noise along text edges.
        effective_min_area = max(min_area, int(W * H * 0.0003))

        # Morphological opening removes isolated noise; 2 dilation iterations
        # merges adjacent pixels without creating huge blobs from edge noise.
        opened = ndimage.binary_opening(binary, iterations=1)
        dilated = ndimage.binary_dilation(opened, iterations=2)
        labeled, num = ndimage.label(dilated)
        PAD = 3
        candidates = []
        for rid in range(1, num + 1):
            rmask = (labeled == rid)
            orig_area = int(binary[rmask].sum())  # area in original (undilated) mask
            if orig_area < effective_min_area:
                continue
            ys, xs = np.where(rmask)
            y1 = max(0, int(ys.min()) - PAD)
            y2 = min(H - 1, int(ys.max()) + PAD)
            x1 = max(0, int(xs.min()) - PAD)
            x2 = min(W - 1, int(xs.max()) + PAD)
            bw = max(x2 - x1, 1)
            bh = max(y2 - y1, 1)
            if max(bw, bh) / min(bw, bh) > strip_ratio:
                continue
            candidates.append((orig_area, x1, y1, x2, y2))

        # Keep the largest N regions only
        candidates.sort(key=lambda c: c[0], reverse=True)
        boxes = [
            (float(x1 / W * 100), float(y1 / H * 100),
             float(x2 / W * 100), float(y2 / H * 100))
            for _, x1, y1, x2, y2 in candidates[:max_boxes]
        ]
        return boxes, boxes
    except Exception as exc:
        logger.debug("_pixel_diff_boxes failed: %s", exc)
        return [], []


def _dedupe_pct_boxes(boxes, tol: float = 1.0):
    """Drop near-duplicate %-coord boxes (within `tol` percent on every edge)."""
    uniq = []
    for b in boxes:
        if not any(all(abs(b[i] - u[i]) < tol for i in range(4)) for u in uniq):
            uniq.append(b)
    return uniq


def _distinctive_num(v) -> bool:
    """Czy liczba jest na tyle charakterystyczna, by ją bezpiecznie zaznaczyć boxem.
    Pomijamy gołe małe liczby całkowite (<10) — np. poziomy 2/6 powtarzają się w
    wielu komórkach i boksowanie ich zaznaczyłoby identyczne miejsca. Procenty
    (1,4 / 17,0 / -25,6) i wartości ≥10 są charakterystyczne."""
    try:
        return abs(v) >= 10 or v != int(v)
    except (TypeError, ValueError):
        return False


def _parse_one_num(s):
    s = re.sub(r"[^0-9,.\-]", "", str(s)).replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    try:
        return float(m.group()) if m else None
    except (ValueError, AttributeError):
        return None


# Linie LEGENDY tabeli ("Level 2 > 30 min", "> 480 min") — wykluczane z boksowania,
# żeby gołe liczby (poziomy 2/6) zaznaczały tylko komórki tabeli, nie legendę.
_LEGEND_BOX_RE = re.compile(r"level\s*\d+\s*>|>\s*\d+\s*min|\bmin\b", re.I)


def _locate_value_boxes(crop, values, max_repeat: int = 2) -> list:
    """Zlokalizuj na wycinku KONKRETNE wartości liczbowe (te, które się różnią)
    przez OCR słowny i zwróć ich boxy (%). Dopasowanie NUMERYCZNE ('17' == '17,0').

    Precyzja:
      • pomija linie LEGENDY ('Level 2 > 30 min') — gołe liczby boksują tylko komórki,
      • jeśli ta sama wartość pasuje do >max_repeat miejsc (np. niezmienione '6' w
        K/P/T), traktuje ją jako NIEJEDNOZNACZNĄ i pomija — zamiast nadboksować całą
        kolumnę. Wołane PER STRONĘ (wartości A na crop A, B na crop B), więc unikalna
        zmieniona liczba (np. '2' w masterze) zaznacza się dokładnie w swojej komórce.
    """
    if not crop or not values:
        return []
    targets = set()
    for v in values:
        f = _parse_one_num(v)
        if f is not None:
            targets.add(round(f, 3))
    if not targets:
        return []
    try:
        words = _paddle_readtext_with_boxes(crop)   # [(text, (x1,y1,x2,y2)%)]
    except Exception:
        return []
    per_val = {}   # target → [boxes]
    for text, box in words or []:
        if _LEGEND_BOX_RE.search(str(text)):
            continue
        for tok in re.findall(r"-?\d[\d.,]*", str(text)):
            f = _parse_one_num(tok)
            if f is None:
                continue
            for t in targets:
                if abs(f - t) < 0.05:
                    per_val.setdefault(t, []).append(box)
                    break
    out = []
    for t, boxes in per_val.items():
        bb = _dedupe_pct_boxes(boxes)
        if len(bb) <= max_repeat:        # unikalne(-awe) → bezpiecznie boksuj
            out.extend(bb)
    return _dedupe_pct_boxes(out)


def _region_pixels_differ(img_a, img_b, box_pct, rgb_thresh: int = 45,
                          min_frac: float = 0.012) -> bool:
    """True when pixels inside box_pct (x1,y1,x2,y2 in %) genuinely differ between
    img_a and img_b (expected to be in the SAME aligned coordinate frame).

    Used to drop OCR-word diff boxes that are visually identical — i.e. the OCR
    engine misread the same printed text differently on the two crops (e.g. master
    read 'klasi' while both prints actually say 'klasy'). Returns True (keep the
    box) on any error, so verification never silently removes a real difference.
    """
    if not HAS_PIL or img_a is None or img_b is None:
        return True
    try:
        x1, y1, x2, y2 = box_pct

        def _crop(im):
            w, h = im.size
            l = max(0, int(x1 / 100.0 * w)); t = max(0, int(y1 / 100.0 * h))
            r = min(w, int(x2 / 100.0 * w)); bo = min(h, int(y2 / 100.0 * h))
            if r <= l or bo <= t:
                return None
            return im.convert("RGB").crop((l, t, r, bo))

        ca, cb = _crop(img_a), _crop(img_b)
        if ca is None or cb is None:
            return True
        W = max(ca.width, cb.width); H = max(ca.height, cb.height)
        if W < 2 or H < 2:
            return True
        lanczos = Image.LANCZOS if hasattr(Image, "LANCZOS") else Image.BICUBIC
        a = np.array(ca.resize((W, H), lanczos), dtype=np.int16)
        b = np.array(cb.resize((W, H), lanczos), dtype=np.int16)
        frac = float((np.abs(a - b).max(axis=2) > rgb_thresh).mean())
        return frac >= min_frac
    except Exception:
        return True


def _diff_text_boxes(img_a, img_b, text_a: str = "", text_b: str = "",
                     field_label: str = "") -> tuple:
    """OCR both crops with bounding boxes, find words present in one but not the
    other (or with a different token), return (boxes_a_pct, boxes_b_pct) where
    each box is (x1,y1,x2,y2) in % of crop size.

    text_a / text_b: clean OCR text already computed by _extract_text_ocr.
    When provided, the TOKEN DIFF is computed from these clean strings — this
    eliminates false positives caused by noise in per-word OCR (Tesseract reads
    the same word slightly differently on two images with different print
    quality).  Box LOCATIONS still come from per-word OCR on each image.

    Token bag uses SET semantics — flag a token only when it is entirely
    absent from one side. Count-based diff produced too many false positives
    on busy tables where OCR segmented identical text into a different
    number of words on each side. Size-label cases like '5' vs '5-6' still
    work because '5-6' tokenizes to ['5','6']."""
    if img_a is None or img_b is None:
        return [], []

    # Guard against one-sided OCR failure: when one side returned the OCR
    # sentinel ("N/D") or is empty, comparing it against the other side would
    # mark the entire other side as "different" — every token in vb becomes a
    # diff token → every per-word OCR line on supplier image gets boxed.  The
    # operator sees a falsely-alarmed all-red image when the actual issue was
    # that one OCR pass failed.  Skip box drawing in that case; the operator
    # still gets the textual N/D ↔ text comparison on the report.
    def _is_blank_ocr(t: str) -> bool:
        return (not t) or t.strip().upper() in ("N/D", "ND", "—", "-")
    if _is_blank_ocr(text_a) or _is_blank_ocr(text_b):
        if text_a or text_b:
            return [], []

    try:
        lines_a = _paddle_readtext_with_boxes(img_a)
        lines_b = _paddle_readtext_with_boxes(img_b)
    except Exception:
        lines_a, lines_b = [], []

    # If PaddleOCR/Tesseract returned nothing try a lightweight Tesseract pass
    # (word-level, PSM 11 — sparse text) as last resort for dense tables where
    # PaddleOCR sometimes misses individual numeric cells.
    def _try_tess_boxes(img):
        try:
            import pytesseract
            data = pytesseract.image_to_data(
                img, lang="eng+pol", config="--psm 11",
                output_type=pytesseract.Output.DICT)
            iw, ih = img.size
            out = []
            for i, txt in enumerate(data.get("text", [])):
                txt = (txt or "").strip()
                if not txt:
                    continue
                try:
                    conf = float(data["conf"][i])
                except (ValueError, TypeError):
                    conf = -1.0
                if conf < 40:
                    continue
                x, y, w, h = (data["left"][i], data["top"][i],
                              data["width"][i], data["height"][i])
                box = (x / iw * 100, y / ih * 100,
                       (x + w) / iw * 100, (y + h) / ih * 100)
                out.append((txt, box))
            return out
        except Exception:
            return []

    if not lines_a:
        lines_a = _try_tess_boxes(img_a)
    if not lines_b:
        lines_b = _try_tess_boxes(img_b)

    # Unicode-aware tokenizer: covers Cyrillic, extended Latin (German ß,
    # French é è ê, Spanish ñ, Czech č š ž …) and Polish.  Without \w with
    # re.UNICODE, non-Latin scripts silently produce empty tokens and the
    # diff path returns no boxes for Russian / Ukrainian / German artworks.
    # Pattern preserves hyphenated-number ranges as atomic tokens (e.g. "5-6",
    # "6-7", "9-10") so that a change like "6" → "5-6" is detected as a
    # distinct value, not silently merged via SET-diff with "6-7" on both sides.
    _TOKEN_RE = re.compile(r"\d+(?:-\d+)+|\w+", re.UNICODE)

    def tokenize(s):
        return [t for t in _TOKEN_RE.findall(s.lower())
                if t.isdigit() or len(t) >= 2]

    from collections import Counter

    # Token diff: use clean OCR when available to avoid OCR-noise false positives.
    # Noisy per-word reads (e.g. "COm" vs "com", ligature artefacts) produce
    # dozens of phantom diff tokens that box nearly everything on the supplier
    # image.  The clean single-pass OCR text (va/vb) has already gone through
    # the full fallback chain and is much more stable.
    if text_a or text_b:
        bag_a = Counter(tokenize(text_a))
        bag_b = Counter(tokenize(text_b))
    else:
        bag_a, bag_b = Counter(), Counter()
        for text, _ in lines_a: bag_a.update(tokenize(text))
        for text, _ in lines_b: bag_b.update(tokenize(text))

    # SET semantics for all tokens: flag a token only if it is entirely absent
    # from one side. Multiset/count diff was rejected because every box that
    # contains the token gets drawn red, not just the "extra" occurrence —
    # so a 1-count drift caused by OCR segmentation noise on a busy table
    # (e.g. "mg/ml" read 6 times on supplier vs 5 on master) marks all 6
    # occurrences as a difference even though the values match perfectly.
    # Size-label cases like "5" vs "5-6" still work: "5-6" tokenizes to
    # ["5","6"], so "6" is absent from master and is flagged on supplier.
    only_in_a = set(bag_a) - set(bag_b)
    only_in_b = set(bag_b) - set(bag_a)

    # Segmentation-noise filter: OCR on the two crops often joins/splits the
    # same word differently — master reads "Kunststoff-Pinzette" as ONE token,
    # supplier reads it as "Kunststoff"+"Pinzette" (or vice versa). Two artifact
    # patterns exist:
    #   JOIN: token T is the concatenation of two other-side tokens (T = a+b,
    #         both a and b are in the other bag) → T is a false diff.
    #   SPLIT: token T is a proper sub-string of some other-side token → T is
    #          a false diff (e.g. "plastik" ⊂ "plastikust" on other side).
    # Previous approach (''.join(bag.keys()) + substring check) was wrong: it
    # concatenated tokens without a separator, so e.g. bag = {'ref','cat'} gave
    # 'refcat' and the token 'efc' (a REAL diff) was silently suppressed because
    # 'efc' is a cross-boundary substring of 'refcat'. Fixed with explicit checks.
    _NUMERIC_RANGE_RE = re.compile(r"^\d+(?:-\d+)+$")

    def _seg_artifact(token: str, other_keys) -> bool:
        other = set(other_keys)
        # JOIN: token == concat of exactly two tokens from other side
        for n in range(1, len(token)):
            if token[:n] in other and token[n:] in other:
                return True
        # SPLIT: token is a proper substring of some longer other-side token.
        # Exclude numeric-range tokens (e.g. "6" ⊂ "5-6"): a size-range like
        # "5-6" is a distinct value, not a compound-word segmentation artefact.
        return any(token in k for k in other
                   if len(k) > len(token) and not _NUMERIC_RANGE_RE.match(k))

    only_in_a = {t for t in only_in_a if not _seg_artifact(t, bag_b.keys())}
    only_in_b = {t for t in only_in_b if not _seg_artifact(t, bag_a.keys())}

    # Fuzzy near-match suppression (cautious): an OCR pass often reads the same
    # word slightly differently on the two crops ("Conformity" vs "Conformlty",
    # "Clone" vs "Clonee"). Such a token is technically "only on one side" yet is
    # not a real difference — and because a whole paragraph used to get boxed,
    # one noisy token painted an identical block of text red. Drop alphabetic
    # tokens (len ≥ 4) that closely match (ratio ≥ 90) a token on the other side.
    # Digits and short codes are NEVER fuzzed: "374" vs "375" or "5" vs "6" must
    # remain a genuine difference.
    try:
        from rapidfuzz import fuzz as _rf_fuzz

        def _ocr_variant(token: str, other_keys) -> bool:
            if token.isdigit() or len(token) < 4:
                return False
            return any(_rf_fuzz.ratio(token, k) >= 90 for k in other_keys
                       if not k.isdigit() and abs(len(k) - len(token)) <= 2)

        only_in_a = {t for t in only_in_a if not _ocr_variant(t, bag_b.keys())}
        only_in_b = {t for t in only_in_b if not _ocr_variant(t, bag_a.keys())}
    except Exception as exc:
        logger.debug("fuzzy near-match suppression skipped: %s", exc)

    # Word-level boxing: box only the differing WORD, not the whole OCR line.
    # PaddleOCR returns line/paragraph-level boxes, so a single diff token used
    # to paint an entire (often identical) paragraph red — the main source of
    # false-positive boxes on dense tables. Locate the precise word via
    # Tesseract word-level OCR; fall back to the line box only when the word is
    # not found (e.g. Cyrillic crops Tesseract can't read in eng+pol).
    words_a = _try_tess_boxes(img_a)
    words_b = _try_tess_boxes(img_b)

    def _dedupe_boxes(boxes):
        uniq = []
        for b in boxes:
            if not any(all(abs(b[i] - u[i]) < 0.5 for i in range(4)) for u in uniq):
                uniq.append(b)
        return uniq

    def boxes_for_diffs(lines, words, diff_tokens):
        out, unlocated = [], []
        for tok in diff_tokens:
            tight = [box for text, box in words if tok in tokenize(text)]
            if tight:
                out.extend(tight)
            else:
                line_boxes = [box for text, box in lines if tok in tokenize(text)]
                if line_boxes:
                    out.extend(line_boxes)
                else:
                    unlocated.append(tok)   # detected as different but not found on image
        return _dedupe_boxes(out), unlocated

    boxes_a, unloc_a = boxes_for_diffs(lines_a, words_a, only_in_a)
    boxes_b, unloc_b = boxes_for_diffs(lines_b, words_b, only_in_b)

    # Diagnostic: surface what the token diff found and where word-localization is
    # weak (a token flagged as different but not locatable on the image → no box).
    if only_in_a or only_in_b:
        logger.info("[artwork-diff] pole=%r różnice-tokenów: tylko_wzorzec=%s "
                    "tylko_dostawca=%s",
                    field_label or "?", sorted(only_in_a), sorted(only_in_b))
    if unloc_a or unloc_b:
        logger.info("[artwork-diff]   ⚠ słaby punkt OCR-lokalizacji: tokeny "
                    "wykryte jako różne, ale NIE znalezione na obrazie (brak boxa) "
                    "→ wzorzec=%s dostawca=%s (zadziała pixel-diff)",
                    sorted(unloc_a), sorted(unloc_b))

    # Mirror boxes to the opposite side when one side found a location and the
    # other didn't. Both images show the same field region, so the changed value
    # is at approximately the same position on both sides. This lets the operator
    # see exactly where to look on master when supplier has "5-6" boxed (or vice
    # versa). Without mirroring, master would show no box at all — confusing.
    if boxes_b and not boxes_a:
        boxes_a = list(boxes_b)
    if boxes_a and not boxes_b:
        boxes_b = list(boxes_a)

    return boxes_a, boxes_b


def _paddle_readtext_with_boxes(img) -> list:
    """Return [(text, (x1_pct, y1_pct, x2_pct, y2_pct)), ...] for each detected
    line. Coords normalised to 0-100% of image size for client overlay.

    Tries PaddleOCR first; falls back to Tesseract image_to_data (word-level)
    when Paddle is unavailable. Tesseract is system-provided and doesn't
    require downloading models, so the fallback is safe on small VPS hosts."""
    iw, ih = img.size
    out = []

    # ── Tier 1: PaddleOCR (best accuracy, per-script) ────────────────────
    # Convert to numpy lazily — only if at least one Paddle reader exists.
    # On hosts without Paddle, this saves a ~20 MB allocation per call.
    arr = None
    for script in ("latin", "cyrillic"):
        reader = _get_paddleocr(script)
        if not reader:
            continue
        if arr is None:
            import numpy as np
            arr = np.array(img.convert("RGB"))
        try:
            result = reader.ocr(arr, cls=True)
            if not result or not result[0]:
                continue
            for line in result[0]:
                # BUGFIX: chroń przed pustym line[1]/pts i zerowym rozmiarem obrazu
                if (not line or len(line) < 2 or not line[1] or len(line[1]) < 2
                        or not isinstance(line[1][1], (int, float)) or line[1][1] <= 0.3):
                    continue
                pts = line[0]  # 4 corner points
                if not pts:
                    continue
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                if not xs or not ys:
                    continue
                x1, x2 = min(xs), max(xs)
                y1, y2 = min(ys), max(ys)
                _iw, _ih = max(iw, 1), max(ih, 1)
                out.append((
                    line[1][0],
                    (x1 / _iw * 100, y1 / _ih * 100,
                     x2 / _iw * 100, y2 / _ih * 100),
                ))
            # Do NOT early-return after the first script — collect results from
            # all scripts so mixed-script labels (e.g. Latin + Cyrillic) get
            # complete bounding-box coverage.
        except Exception as exc:
            logger.debug("PaddleOCR (%s) box-readtext failed: %s", script, exc)

    if out:
        return out

    # ── Tier 2: Tesseract image_to_data ──────────────────────────────────
    # Word-level only (level=5).  Tesseract emits container entries at
    # levels 1-4 (block / paragraph / line / textline) which can carry the
    # concatenated text of a whole line — using those produces row-wide
    # boxes covering many cells.  Word-level boxes are precise.
    try:
        import pytesseract
        data = pytesseract.image_to_data(
            img.convert("RGB") if img.mode not in ("RGB", "L") else img,
            lang=_get_ocr_langs(), config="--psm 6",
            output_type=pytesseract.Output.DICT,
        )
        levels = data.get("level", [])
        texts  = data.get("text", [])
        confs  = data.get("conf", [])
        lefts  = data.get("left", [])
        tops   = data.get("top", [])
        widths = data.get("width", [])
        heights= data.get("height", [])
        n = len(texts)
        for i in range(n):
            # Word level only — skip container entries.
            try:
                if int(levels[i]) != 5:
                    continue
            except (IndexError, TypeError, ValueError):
                continue
            text = (texts[i] or "").strip()
            try:
                conf = float(confs[i])
            except (TypeError, ValueError, IndexError):
                conf = -1.0
            if not text or conf < 30:
                continue
            x = lefts[i]; y = tops[i]
            w = widths[i]; h = heights[i]
            if w <= 0 or h <= 0:
                continue
            out.append((
                text,
                (x/iw*100, y/ih*100,
                 (x+w)/iw*100, (y+h)/ih*100),
            ))
    except Exception as exc:
        logger.debug("Tesseract box-readtext failed: %s", exc)

    return out


def _get_got_ocr():
    """Lazy GOT-OCR 2.0 singleton (stepfun-ai/GOT-OCR2_0 via HuggingFace).

    Gated behind ARTWORK_ENABLE_LOCAL_VLM like Qwen — multi-GB local model that
    must never load on a small-RAM host.
    """
    global _got_ocr_model, _got_ocr_tokenizer
    if os.environ.get("ARTWORK_ENABLE_LOCAL_VLM", "").strip() not in ("1", "true", "yes"):
        _got_ocr_model = False
        return None
    if _got_ocr_model is not None:
        return (_got_ocr_tokenizer, _got_ocr_model) if _got_ocr_model is not False else None
    with _got_ocr_lock:
        if _got_ocr_model is not None:
            return (_got_ocr_tokenizer, _got_ocr_model) if _got_ocr_model is not False else None
        try:
            import torch
            from transformers import AutoTokenizer, AutoModelForCausalLM
            # Bandit B615: stała nazwa zaufanego modelu z HF Hub (nie z wejścia); opcjonalny model ładowany leniwie.
            _got_ocr_tokenizer = AutoTokenizer.from_pretrained(  # nosec B615
                "stepfun-ai/GOT-OCR2_0", trust_remote_code=True)
            # Bandit B615: stała nazwa zaufanego modelu z HF Hub (nie z wejścia); opcjonalny model ładowany leniwie.
            _got_ocr_model = AutoModelForCausalLM.from_pretrained(  # nosec B615
                "stepfun-ai/GOT-OCR2_0",
                trust_remote_code=True,
                low_cpu_mem_usage=True,
                device_map="cpu",
                torch_dtype=torch.float32,
            ).eval()
            logger.info("GOT-OCR 2.0 loaded")
            return (_got_ocr_tokenizer, _got_ocr_model)
        except Exception as exc:
            logger.warning("GOT-OCR 2.0 unavailable: %s", exc)
            _got_ocr_model = False
            return None


def _got_readtext(img) -> str:
    """Run GOT-OCR 2.0 on a PIL image. Returns extracted text or empty string."""
    pair = _get_got_ocr()
    if not pair:
        return ""
    tokenizer, model = pair
    import tempfile, os as _os
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp = f.name
            img.save(tmp)
        result = model.chat(tokenizer, tmp, ocr_type="ocr")
        return (result or "").strip()
    except Exception as exc:
        logger.debug("GOT-OCR inference failed: %s", exc)
        return ""
    finally:
        if tmp:
            try:
                _os.unlink(tmp)
            except Exception:
                pass


def _preprocess_for_ocr(img: "Image.Image") -> "Image.Image":
    """Upscale small crops, boost contrast, and sharpen before any OCR engine.

    Keeps full colour — neural models (Claude, Mistral, Qwen) perform better on
    colour images than on greyscale or binarised ones.  Tesseract's own Sauvola
    step handles binarisation when it runs as the final fallback.
    """
    from PIL import ImageEnhance, ImageFilter
    img = img.convert("RGB")
    w, h = img.size
    # Downscale BARDZO dużych wycinków PRZED OCR — ogranicza koszt OCR (payload do
    # Claude Vision oraz czas Tesseracta) na wielkich polach tekstowych (np.
    # wielojęzyczny Opis_*), które inaczej wpychają pole w timeout. 2600 px zostawia
    # tekst w pełni czytelny. Wyłącz/zmień przez ARTWORK_OCR_MAX_PX.
    try:
        _ocr_cap = int(os.environ.get("ARTWORK_OCR_MAX_PX", "2600"))
    except ValueError:
        _ocr_cap = 2600
    if _ocr_cap and max(w, h) > _ocr_cap:
        _ds = _ocr_cap / float(max(w, h))
        img = img.resize((max(1, int(w * _ds)), max(1, int(h * _ds))), Image.LANCZOS)
        w, h = img.size
    # Upscale so the shortest side is at least 600 px and the longest at least
    # 1200 px — OCR accuracy drops sharply below these thresholds.
    scale = 1.0
    if min(w, h) < 600:
        scale = 600 / min(w, h)
    elif max(w, h) < 1200:
        scale = 1200 / max(w, h)
    if scale > 1.0:
        new_w, new_h = int(w * scale), int(h * scale)
        # Cap so the longest side never exceeds 2400 px — prevents runaway
        # memory use on tiny crops (e.g. 2×500 px → scale=300 → 600×150000 px).
        if max(new_w, new_h) > 2400:
            cap = 2400 / max(new_w, new_h)
            new_w, new_h = int(new_w * cap), int(new_h * cap)
        img = img.resize((new_w, new_h), Image.LANCZOS)
    # Mild contrast boost — compensates for low-contrast packaging print
    img = ImageEnhance.Contrast(img).enhance(1.35)
    # Unsharp mask — sharpens text edges without amplifying flat-area noise
    img = img.filter(ImageFilter.UnsharpMask(radius=1.0, percent=120, threshold=3))
    return img


def _claude_readtext(img, model: str = None) -> str:
    """OCR via Claude Vision — most accurate, understands context and layout.

    Sends the image to Claude and asks for verbatim text extraction.
    Requires ANTHROPIC_API_KEY. Used as primary OCR when available.
    `model` overrides the OCR model (e.g. an accurate model for verification).
    """
    api_key = _get_api_key_artwork()
    if not api_key:
        return ""
    _model = model or _current_ocr_model()
    try:
        import base64 as _b64, httpx as _hx
        from io import BytesIO
        w, h = img.size
        # Downscale only if above 2048 px — Claude handles higher resolution well
        if max(w, h) > 2048:
            scale = 2048 / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=92)
        b64 = _b64.b64encode(buf.getvalue()).decode()
        resp = _hx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                     "Content-Type": "application/json"},
            json={
                "model": _model,
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64",
                                                  "media_type": "image/jpeg", "data": b64}},
                    {"type": "text", "text":
                        "Extract ALL text from this medical packaging label image exactly as printed. "
                        "This may contain: EAN/GTIN barcodes, LOT numbers, expiry dates (EXP), "
                        "REF codes, revision numbers, gauge sizes, product names in multiple languages. "
                        "CRITICAL character rules — never substitute: "
                        "0 (zero) ≠ O (letter O), 1 (one) ≠ I (letter I) ≠ l (lowercase L), "
                        "5 ≠ S, 8 ≠ B, 6 ≠ G. "
                        "Preserve exact spacing, punctuation, and every digit. "
                        "Return only the extracted text, nothing else."},
                ]}],
            },
            timeout=30,
        )
        if resp.status_code != 200:
            # Log the body so 400s are diagnosable (bad model name, oversized
            # image, malformed request etc.) instead of silently failing.
            _body = ""
            try:
                _body = resp.text[:400]
            except Exception:
                pass
            logger.warning("Claude OCR HTTP %s — model=%s body=%s",
                           resp.status_code, _model, _body)
            return ""
        _resp_json = resp.json()
        try:
            from api_usage_tracker import record_usage
            record_usage(_model, _resp_json.get("usage", {}), call_type="ocr")
        except Exception:
            pass
        _content = (_resp_json.get("content") or [])
        if not _content:
            return ""
        txt = (_content[0].get("text") or "").strip()
        # Claude czasem dodaje markdown (**pogrubienie**, `kod`, ~~przekreślenie~~,
        # # nagłówki) mimo prośby o czysty tekst. Jedna strona dostaje **www**, druga
        # www → fałszywa różnica. Usuń znaczniki formatowania (treść zostaje).
        txt = _strip_markdown(txt)
        return txt.strip()
    except Exception as exc:
        logger.debug("Claude OCR failed: %s", exc)
        return ""


# In-process OCR cache: OCR of a crop is a pure function of its pixels +
# auto_orient, but the cascade is expensive (Claude Vision / Mistral API calls,
# heavy local models). Cache results per crop content hash so repeated OCR of
# the same crop — within a comparison and across re-runs — is instant and free.
# Per-process (gunicorn sync workers don't share memory); bounded LRU-ish size.
_OCR_TEXT_CACHE = {}
_OCR_TEXT_CACHE_MAX = 256
_OCR_TEXT_CACHE_LOCK = threading.Lock()


def _ocr_cache_key(img, auto_orient):
    try:
        return (hashlib.sha1(img.tobytes(), usedforsecurity=False).hexdigest(), img.size, img.mode, bool(auto_orient))
    except Exception:
        return None


_OCR_VERIFY_CACHE = {}
_OCR_VERIFY_CACHE_LOCK = threading.Lock()


def _ocr_verify_model() -> str:
    """Dokładny model do WERYFIKACJI różnic (domyślnie Sonnet). Gdy równy modelowi
    OCR albo pusty → weryfikacja wyłączona (zwraca '')."""
    vm = os.environ.get("ARTWORK_OCR_VERIFY_MODEL", "claude-sonnet-4-6").strip()
    return "" if (not vm or vm == _current_ocr_model()) else vm


def _ocr_verify_read(img):
    """Ponowny, dokładny odczyt OCR wycinka modelem weryfikującym (np. Sonnet).
    Używany do potwierdzenia różnicy wykrytej szybkim modelem (Haiku) na polu
    krytycznym — błąd OCR szybkiego modelu nie da wtedy fałszywej różnicy.
    Zwraca None gdy weryfikacja wyłączona/niedostępna (zachowaj pierwotny odczyt)."""
    vm = _ocr_verify_model()
    if not vm or img is None:
        return None
    key = _ocr_cache_key(img, False)
    ck = (("verify", vm) + key) if key is not None else None
    if ck is not None:
        with _OCR_VERIFY_CACHE_LOCK:
            if ck in _OCR_VERIFY_CACHE:
                return _OCR_VERIFY_CACHE[ck]
    try:
        txt = _claude_readtext(_preprocess_for_ocr(img), model=vm)
    except Exception as exc:
        logger.debug("OCR verify read failed: %s", exc)
        txt = ""
    txt = " ".join((txt or "").split())
    if ck is not None:
        with _OCR_VERIFY_CACHE_LOCK:
            _OCR_VERIFY_CACHE[ck] = txt
            if len(_OCR_VERIFY_CACHE) > 256:
                try:
                    del _OCR_VERIFY_CACHE[next(iter(_OCR_VERIFY_CACHE))]
                except Exception:
                    pass
    return txt


def _extract_text_ocr(img, auto_orient: bool = True) -> str:
    """Cached wrapper around the OCR cascade (see _extract_text_ocr_uncached)."""
    if img is None:
        return ""
    key = _ocr_cache_key(img, auto_orient)
    if key is not None:
        with _OCR_TEXT_CACHE_LOCK:
            if key in _OCR_TEXT_CACHE:
                return _OCR_TEXT_CACHE[key]
    result = _extract_text_ocr_uncached(img, auto_orient=auto_orient)
    # Single chokepoint for every OCR engine: repair encoding noise so it can't
    # drive false A/B diffs (no-op if ftfy isn't installed).
    result = _ftfy_fix(result)
    if key is not None:
        with _OCR_TEXT_CACHE_LOCK:
            _OCR_TEXT_CACHE[key] = result
            if len(_OCR_TEXT_CACHE) > _OCR_TEXT_CACHE_MAX:
                try:                   # drop oldest (dict is insertion-ordered)
                    del _OCR_TEXT_CACHE[next(iter(_OCR_TEXT_CACHE))]
                except Exception:
                    pass
    return result


def _ocr_tall_in_strips(img, auto_orient: bool = True) -> str:
    """OCR wysokiego, GĘSTEGO bloku tekstu (np. wielojęzyczny Opis_*) w poziomych
    PASACH. Jako jeden obraz taki blok traci detale — Claude Vision skaluje najdłuższy
    bok do ~1568 px, więc przy wysokości 5-6 tys. px szerokość spada do ~500 px i drobne
    linie znikają (OCR gubi tekst i „dopisuje" zmyślony). Każdy pas trafia do silnika
    osobno → wyższa efektywna rozdzielczość → kompletny odczyt. Lekki zakład, by nie
    ciąć wiersza; wynik sklejony w kolejności czytania."""
    if auto_orient:
        try:
            img = _orient_for_ocr(img)
        except Exception:
            pass
    w, h = img.size
    try:
        strip_h = int(os.environ.get("ARTWORK_OCR_STRIP_PX", "1400"))
    except ValueError:
        strip_h = 1400
    strip_h = max(600, strip_h)
    overlap = max(20, int(strip_h * 0.05))
    parts, y = [], 0
    while y < h:
        y2 = min(h, y + strip_h)
        top = max(0, y - overlap) if y > 0 else 0
        strip = img.crop((0, top, w, y2))
        try:
            txt = _extract_text_ocr_uncached(strip, auto_orient=False, _allow_strips=False)
        except Exception:
            txt = ""
        if txt and txt.strip():
            parts.append(txt.strip())
        y = y2
    return " ".join(parts)


def _extract_text_ocr_uncached(img, auto_orient: bool = True, _allow_strips: bool = True) -> str:
    """OCR cascade — 8 layers ordered by accuracy, with graceful fallback.

    Hierarchy:
      1. Claude Vision      — best accuracy, context-aware, 0/O 1/I safe
      2. Mistral OCR API    — fast API, excellent on printed labels
      3. Qwen2.5-VL-3B     — best local VLM, 96%+ DocVQA
      4. PaddleOCR          — fast, 85-90% on printed text, orientation-aware
      5. EasyOCR            — 80+ languages, difficult crops
      6. Surya              — layout-aware, reading-order detection
      7. DocTR              — deep-learning, complex multi-column layouts
      8. Tesseract          — universal fallback, always available
    """
    # Gęsty/wysoki blok tekstu jako JEDEN obraz traci detale (patrz _ocr_tall_in_strips)
    # → tniemy na pasy i OCR-ujemy każdy osobno. Tylko dla wyraźnie pionowych, dużych
    # wycinków; próg sterowalny ARTWORK_OCR_STRIP_MIN_H (domyślnie 2200 px).
    if _allow_strips and img is not None:
        try:
            _w, _h = img.size
            _min_h = int(os.environ.get("ARTWORK_OCR_STRIP_MIN_H", "2200"))
            if _h >= _min_h and _h >= _w * 1.6:
                return _ocr_tall_in_strips(img, auto_orient)
        except Exception:
            pass
    paddle_available = bool(_get_paddleocr("latin") or _get_paddleocr("cyrillic"))
    if auto_orient and not paddle_available:
        img = _orient_for_ocr(img)
    # Upscale, contrast-boost, and sharpen once — all engines benefit
    img = _preprocess_for_ocr(img)
    w, h = img.size

    # ── 1. Claude Vision — best accuracy, context-aware ───────────────────
    try:
        text = _claude_readtext(img)
        if text:
            return text
    except Exception as exc:
        logger.debug("Claude OCR failed: %s", exc)

    # ── 2. Mistral OCR API — fast, excellent on printed text ──────────────
    try:
        text = _mistral_readtext(img)
        if text:
            return text
    except Exception as exc:
        logger.debug("Mistral OCR failed: %s", exc)

    # ── Local model tiers (3-7) — accurate but slow; each can stall up to its
    #    own timeout when its model isn't installed. Skipped wholesale when
    #    ARTWORK_OCR_LOCAL=0 so the cascade is API → Tesseract only. ──────────
    if _OCR_LOCAL_ENGINES:
        # ── 3. Qwen2.5-VL-3B — best local model, 96%+ DocVQA, 0/O 1/I safe ──
        try:
            text = _qwen_readtext(img)
            if text:
                return text
        except Exception as exc:
            logger.debug("Qwen2.5-VL failed: %s", exc)

        # ── 4. RapidOCR (ONNX) — self-contained Paddle models, fast on CPU ────
        # Tried before PaddleOCR: ships its own models (no missing-model failures)
        # and avoids the paddlepaddle dependency. Paddle stays as a fallback.
        try:
            text = _rapidocr_readtext(img)
            if text:
                return text
        except Exception as exc:
            logger.debug("RapidOCR failed: %s", exc)

        # ── 4b. PaddleOCR — fast, handles skewed text and orientation ─────────
        try:
            text = _paddle_readtext(img, use_cls=auto_orient)
            if text:
                return text
        except Exception as exc:
            logger.debug("PaddleOCR failed: %s", exc)

        # ── 5. EasyOCR — 80+ languages, good for mixed-script EU packaging ────
        reader = _get_easyocr_reader()
        if reader:
            try:
                result = reader.readtext(img, detail=0, paragraph=False, batch_size=1)
                text = " ".join(str(r) for r in result).strip()
                if text:
                    return text
            except Exception as exc:
                logger.debug("EasyOCR failed: %s", exc)

        # ── 6. Surya — layout-aware, reading-order detection ──────────────────
        try:
            text = _surya_readtext(img)
            if text:
                return text
        except Exception as exc:
            logger.debug("Surya failed: %s", exc)

        # ── 7. DocTR — deep-learning OCR, handles complex multi-column layouts ─
        try:
            text = _doctr_readtext(img)
            if text:
                return text
        except Exception as exc:
            logger.debug("DocTR failed: %s", exc)

    # ── Fallback: Tesseract with Sauvola pre-binarization ────────────────
    try:
        import pytesseract
        try:
            from skimage.filters import threshold_sauvola
            _g = img.convert("L")
            _arr = np.array(_g)
            _win = max(25, min(_arr.shape[0], _arr.shape[1]) // 4)
            if _win % 2 == 0:
                _win += 1
            _thresh = threshold_sauvola(_arr, window_size=_win, k=0.2)
            _bin = Image.fromarray(((_arr > _thresh) * 255).astype(np.uint8))
            _t0 = time.time()
            _res = pytesseract.image_to_string(_bin, lang=_get_ocr_langs(), config="--psm 3",
                                               timeout=_TESSERACT_TIMEOUT) or ""
            _elapsed = time.time() - _t0
            if _elapsed > _TESSERACT_TIMEOUT:
                logger.warning("Tesseract OCR took %.1fs (timeout threshold: %ds)", _elapsed, _TESSERACT_TIMEOUT)
            return _res
        except Exception:
            _t0 = time.time()
            _res = pytesseract.image_to_string(img, lang=_get_ocr_langs(), config="--psm 3",
                                               timeout=_TESSERACT_TIMEOUT) or ""
            _elapsed = time.time() - _t0
            if _elapsed > _TESSERACT_TIMEOUT:
                logger.warning("Tesseract OCR took %.1fs (timeout threshold: %ds)", _elapsed, _TESSERACT_TIMEOUT)
            return _res
    except Exception as exc:
        logger.warning("OCR failed: %s", exc)
        return ""


# ── Perceptual similarity helpers ──────────────────────────────────────────────

def _phash_similarity(img_a, img_b) -> float:
    """Perceptual hash similarity: 1.0 = identical, 0.0 = completely different.
    Very fast (~1 ms) — used as a pre-filter before expensive SSIM/pixel diff."""
    try:
        import imagehash
        h1 = imagehash.phash(img_a)
        h2 = imagehash.phash(img_b)
        bits = len(h1.hash.flatten())
        if not bits:            # guard against empty hash → ZeroDivisionError
            return -1.0
        return 1.0 - (h1 - h2) / bits
    except Exception:
        return -1.0  # unavailable


def _ssim_score(img_a, img_b) -> float:
    """Structural Similarity Index (SSIM): 1.0 = identical, 0.0 = no similarity.
    Better than raw pixel diff — accounts for luminance, contrast and structure."""
    try:
        from skimage.metrics import structural_similarity as ssim
        a = np.array(img_a.convert("L"))
        b = np.array(img_b.convert("L"))
        if a.shape != b.shape:
            b = np.array(img_b.resize(img_a.size, Image.LANCZOS).convert("L"))
        min_dim = min(a.shape[0], a.shape[1])
        if min_dim < 7:
            return -1.0
        win_size = max(3, min(7, min_dim))
        if win_size % 2 == 0:
            win_size -= 1
        score, _ = ssim(a, b, full=True, win_size=win_size)
        return float(score)
    except Exception:
        return -1.0  # unavailable


def _ocr_yellow_badges(img) -> list:
    """Wykrywa żółte badge'y gauge igły przez color masking + OCR."""
    if not HAS_PIL or not HAS_SCIPY:
        return []
    try:
        arr = np.array(img)
        yellow = (arr[:, :, 0] > 180) & (arr[:, :, 1] > 120) & (arr[:, :, 2] < 90)
        labeled, num = ndimage.label(yellow)
        badges = []
        for rid in range(1, num + 1):
            mask = (labeled == rid)
            ys, xs = np.where(mask)
            if len(ys) < 300:
                continue
            y1, y2, x1, x2 = ys.min(), ys.max(), xs.min(), xs.max()
            if (x2 - x1) < 40 or (y2 - y1) < 40:
                continue
            crop = img.crop((max(0, x1-15), max(0, y1-15),
                              min(img.width, x2+15), min(img.height, y2+15)))
            text = _extract_text_ocr(crop, auto_orient=False)
            clean = " ".join(text.split())
            m_g  = re.search(r"(\d+)\s*[Gg]", clean)
            m_mm = re.findall(r"[\d,.]+\s*mm", clean, re.IGNORECASE)
            if m_g or m_mm:
                badges.append({
                    "text": clean, "gauge": m_g.group(1) if m_g else None,
                    "mm": m_mm, "area": int(len(ys)),
                    "x_pct": float(round(x1 / max(img.width, 1) * 100)),
                    "y_pct": float(round(y1 / max(img.height, 1) * 100)),
                })
        return badges
    except Exception as e:
        logger.warning("OCR yellow badge extraction failed: %s", e)
        return []


# ─── PIXEL DIFF + KOLOROWE REGIONY ────────────────────────────────────────────

def _compute_pixel_diff(img_a, img_b):
    """
    Zwraca (diff_pct, img_a_annot, img_b_annot, img_diff_classic, regions).
    img_a_annot i img_b_annot mają zaznaczone regiony w 3 kolorach:
      Czerwony  — duże różnice (avg > 60)
      Żółty     — średnie różnice (avg 25-60)
      Niebieski — małe różnice (avg < 25)
    """
    # Wyrównaj rozmiary
    wa, ha = img_a.size
    wb, hb = img_b.size
    if wa == 0 or ha == 0 or wb == 0 or hb == 0:
        return 0.0, img_a, img_b, img_a, []

    img_a_cmp = img_a
    img_b_r   = None
    b_warped  = False   # True tylko gdy B zostało zwarpowane homografią do układu A

    # ── Image registration ────────────────────────────────────────────────
    # Warp B into A's coordinate space (SIFT/AKAZE/LightGlue + ECC) before diffing.
    # Eliminates false regions caused by sub-mm print/render shifts. The perceptual
    # hash skips this work when the hashes already match (no shift to correct).
    if _ALIGN_ENABLED:
        ph = _phash_similarity(img_a, img_b)   # 1.0 = identical hash, -1.0 = lib missing
        if ph < 0.999:
            try:
                aligned_b, _conf = _align_crop_to_master(img_a, img_b)
                if aligned_b is not None and aligned_b.size == img_a.size:
                    img_b_r = aligned_b
                    b_warped = True
            except Exception as _ae:
                logger.debug("Full-page alignment failed: %s — falling back to resize", _ae)

    if img_b_r is not None:
        # Alignment may downscale (cost cap) → aligned size can differ from A.
        # Diff at the aligned resolution; region coords are percentage-based so
        # annotations still map correctly onto the original images.
        W, H = img_b_r.size
        if img_a_cmp.size != (W, H):
            img_a_cmp = img_a.resize((W, H), Image.LANCZOS)
    elif (wa, ha) != (wb, hb):
        W, H = max(wa, wb), max(ha, hb)
        img_a_cmp = img_a.resize((W, H), Image.LANCZOS) if img_a.size != (W, H) else img_a
        img_b_r   = img_b.resize((W, H), Image.LANCZOS)
    else:
        W, H = wa, ha
        img_b_r   = img_b

    diff_arr  = np.array(ImageChops.difference(img_a_cmp, img_b_r))
    diff_gray = np.max(diff_arr, axis=2).astype(np.uint8)

    # ── Adaptive threshold ────────────────────────────────────────────────
    # Lift the fixed floor by the page's own noise level (median + MAD of the
    # non-zero diff). Keeps real edits, drops anti-alias / print-grain noise.
    thresh = DIFF_THRESH
    if _ADAPTIVE_THRESH:
        nz = diff_gray[diff_gray > 0]
        if nz.size:
            med   = float(np.median(nz))
            mad   = float(np.median(np.abs(nz - med)))
            noise = med + 1.4826 * mad
            thresh = int(max(DIFF_THRESH, min(noise, DIFF_THRESH * 2)))

    binary    = diff_gray > thresh
    diff_pct  = float(binary.sum() / binary.size * 100)

    regions = []
    if HAS_SCIPY:
        labeled, num_reg = ndimage.label(binary)
        for rid in range(1, num_reg + 1):
            rmask = (labeled == rid)
            area  = rmask.sum()
            if area < MIN_REGION_PX:
                continue
            ys, xs = np.where(rmask)
            y1, y2 = int(ys.min()), int(ys.max())
            x1, x2 = int(xs.min()), int(xs.max())
            # +1: bbox is inclusive of x2/y2 (matches the [y1:y2+1, x1:x2+1] slice below)
            bw, bh = max(x2 - x1 + 1, 1), max(y2 - y1 + 1, 1)
            # Reject thin layout-shift strips (e.g. text shifted 5px → elongated bbox)
            if max(bw, bh) / min(bw, bh) > STRIP_RATIO:
                continue
            avg_diff = float(diff_gray[y1:y2+1, x1:x2+1].mean())
            sev = "high" if avg_diff > 60 else "medium" if avg_diff > 25 else "low"
            regions.append({
                "x_pct": float(round(x1 / W * 100, 1)), "y_pct": float(round(y1 / H * 100, 1)),
                "w_pct": float(round(bw / W * 100, 1)), "h_pct": float(round(bh / H * 100, 1)),
                "severity": sev, "avg_diff": float(round(avg_diff, 1)), "area": int(area),
            })
    else:
        # Bez scipy nie ma etykietowania spójnych komponentów. Zamiast nie pokazać
        # ŻADNYCH regionów (mapa różnic była pusta mimo realnej różnicy), degradujemy
        # do JEDNEGO boxa obejmującego wszystkie różniące piksele — symetrycznie do
        # ścieżki bez-scipy w _pixel_diff_boxes.
        ys, xs = np.where(binary)
        if xs.size >= MIN_REGION_PX:
            y1, y2 = int(ys.min()), int(ys.max())
            x1, x2 = int(xs.min()), int(xs.max())
            bw, bh = max(x2 - x1 + 1, 1), max(y2 - y1 + 1, 1)
            box_area = bw * bh
            # Pomiń, gdy różnice są rozproszone po dużej części kadru (jeden box
            # zamalowałby prawie całość) — ten sam guard co w _pixel_diff_boxes.
            if not (box_area / float(W * H) > 0.5 and xs.size / float(box_area) < 0.05):
                avg_diff = float(diff_gray[y1:y2+1, x1:x2+1].mean())
                sev = "high" if avg_diff > 60 else "medium" if avg_diff > 25 else "low"
                regions.append({
                    "x_pct": float(round(x1 / W * 100, 1)), "y_pct": float(round(y1 / H * 100, 1)),
                    "w_pct": float(round(bw / W * 100, 1)), "h_pct": float(round(bh / H * 100, 1)),
                    "severity": sev, "avg_diff": float(round(avg_diff, 1)), "area": int(xs.size),
                })

    COLOR_MAP = {
        "high":   ((220, 38, 38, 110), (220, 38, 38, 230)),
        "medium": ((234, 179, 8, 90),  (234, 179, 8, 210)),
        "low":    ((59, 130, 246, 60), (59, 130, 246, 180)),
    }

    # img_a_cmp is either img_a (when sizes match) or a resized copy.
    # Box coordinates are in W×H space so annotate img_a using img_a_cmp.
    img_a_annot = img_a_cmp.copy().convert("RGBA")
    img_diff    = img_a_cmp.copy().convert("RGBA")
    # B-side annotation target + W×H→target scale. When B was homography-warped
    # into A's frame, its region coords live in the W×H aligned space and a
    # uniform scale CANNOT map them back onto the pristine B (a perspective warp
    # is non-linear) — boxes would drift. So annotate the aligned B directly
    # (sx=sy=1): the preview shows B registered to A, consistent with the A panel
    # and with boxes landing precisely. Without a warp we keep the pristine B and
    # the exact uniform sx/sy mapping (plain resize / identical size).
    if b_warped and img_b_r is not None:
        img_b_annot = img_b_r.copy().convert("RGBA")
        sx = sy = 1.0
    else:
        img_b_annot = img_b.copy().convert("RGBA")
        sx = img_b.width  / W
        sy = img_b.height / H
    draw_a = ImageDraw.Draw(img_a_annot, "RGBA")
    draw_b = ImageDraw.Draw(img_b_annot, "RGBA")
    draw_d = ImageDraw.Draw(img_diff,    "RGBA")

    for reg in regions:
        x1 = int(reg["x_pct"] / 100 * W)
        y1 = int(reg["y_pct"] / 100 * H)
        x2 = int((reg["x_pct"] + reg["w_pct"]) / 100 * W)
        y2 = int((reg["y_pct"] + reg["h_pct"]) / 100 * H)
        fill, outline = COLOR_MAP[reg["severity"]]
        draw_a.rectangle([x1, y1, x2, y2], fill=fill, outline=outline, width=2)
        draw_d.rectangle([x1, y1, x2, y2], fill=(220, 38, 38, 90), outline=(220, 38, 38, 220), width=2)
        bx1, bx2 = int(x1*sx), int(x2*sx)
        by1, by2 = int(y1*sy), int(y2*sy)
        draw_b.rectangle([bx1, by1, bx2, by2], fill=fill, outline=outline, width=2)

    return (diff_pct,
            img_a_annot.convert("RGB"), img_b_annot.convert("RGB"),
            img_diff.convert("RGB"), regions)


# ─── STRUCTURED FIELD DIFF ────────────────────────────────────────────────────

def _first(text: str, pat: str, default: str = "—") -> str:
    m = re.findall(pat, text, re.IGNORECASE)
    if not m:
        return default
    first = m[0]
    if isinstance(first, tuple):
        # Multiple capture groups — join non-empty parts
        joined = " × ".join(p for p in first if p)
        return joined.strip() if joined else default
    return first.strip() if first else default


def _ai_extract_manufacturer_blocks(ocr_a: str, ocr_b: str) -> dict:
    """
    Uses Claude to extract manufacturer, EU REP, and distributor blocks from both OCR texts.
    Returns dict with keys: manufacturer_a, manufacturer_b, eu_rep_a, eu_rep_b, distributor_a, distributor_b
    Falls back to empty strings on error or missing API key.
    """
    import urllib.request as _ur
    api_key = _get_api_key_artwork()
    if not api_key:
        return {}
    if len(ocr_a.strip()) < 20 and len(ocr_b.strip()) < 20:
        return {}

    prompt = (
        "Masz dwa teksty OCR z etykiet wyrobów medycznych (Plik A = wzorzec, Plik B = dostawca).\n"
        "Wyciągnij z każdego:\n"
        "1. manufacturer — nazwa i pełny adres producenta (Manufactured by / wytwórca)\n"
        "2. eu_rep — nazwa i adres EU REP / przedstawiciela UE\n"
        "3. distributor — nazwa i adres dystrybutora / importera (np. ACME)\n\n"
        "Dla każdego pola zwróć PEŁNY tekst (wieloliniowy) dokładnie jak w OCR.\n"
        'Jeśli pole nie występuje zwróć "".\n\n'
        f"=== PLIK A (wzorzec) ===\n{ocr_a[:3000]}\n\n"
        f"=== PLIK B (dostawca) ===\n{ocr_b[:3000]}\n\n"
        "Zwróć WYŁĄCZNIE JSON:\n"
        '{"manufacturer_a":"","manufacturer_b":"","eu_rep_a":"","eu_rep_b":"","distributor_a":"","distributor_b":""}'
    )

    payload = json.dumps({
        "model": _current_ai_model(),
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = _ur.Request(
        "https://api.anthropic.com/v1/messages", data=payload,
        headers={"Content-Type": "application/json", "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"}, method="POST")
    try:
        # Bandit B310: URL stały w kodzie (https://api.anthropic.com), schemat nie pochodzi z wejścia.
        with _ur.urlopen(req, timeout=20) as resp:  # nosec B310
            data = json.loads(resp.read())
        try:
            from api_usage_tracker import record_usage
            record_usage(_current_ai_model(), data.get("usage", {}), call_type="artwork_manufacturer")
        except Exception:
            pass
        _content = data.get("content") or []
        if not _content:
            return {}
        text = (_content[0].get("text") or "").strip()
        text = re.sub(r"^```(?:json)?\s*", "", text).rstrip("`").strip()
        m = re.search(r"\{[\s\S]+\}", text)
        if not m:
            return {}
        try:
            return json.loads(m.group())
        except (json.JSONDecodeError, ValueError):
            return {}
    except Exception as _exc:
        logger.warning("Manufacturer AI detection failed: %s", _exc)
        return {}


def _detect_color_spec(text: str) -> str:
    pantone_pats = [
        r"PANTONE\s+[\w\-]+(?:\s+[CUMV](?:\s|$)|\s+C\b)?",
        r"\bP\s+\d{3,4}\s+[A-Z]+\b",   # shorthand "P 485 C", "P 1805 U"
    ]
    pantones = []
    for pat in pantone_pats:
        pantones.extend(re.findall(pat, text, re.IGNORECASE))
    if pantones:
        return " + ".join(dict.fromkeys(p.strip() for p in pantones))
    cmyk = re.search(r"(\d{1,3})[/\s]+(\d{1,3})[/\s]+(\d{1,3})[/\s]+(\d{1,3})", text)
    if cmyk:
        return f"CMYK {cmyk.group(1)}/{cmyk.group(2)}/{cmyk.group(3)}/{cmyk.group(4)}"
    return "—"


def _extract_lot(text: str) -> str:
    """Wyciąga numer LOT/Batch z tekstu opakowania medycznego."""
    patterns = [
        r"(?:LOT|Lot|BATCH|Batch|SERIA|Partia|Nr\s*(?:serii|LOT))[:\s#]+([A-Z0-9][A-Z0-9\-]{2,20})",
        r"\bLOT[:\s]([A-Z0-9\-]{3,20})\b",
        r"\b(?:BATCH|SERIE|SERIE NO)[.:\s]+([A-Z0-9\-]{3,20})\b",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return "—"


def _extract_exp(text: str) -> str:
    """Wyciąga datę ważności (EXP/USE BY) z tekstu opakowania."""
    patterns = [
        r"(?:EXP|Exp\.?|USE\s*BY|EXPIRY|Ważny\s*do|Ważność|Data\s*ważności)[:\s]+(\d{2}[./\-]\d{2,4}(?:[./\-]\d{2,4})?)",
        r"(?:EXP|Exp\.?|USE\s*BY)[:\s]+(\d{4}[./\-]\d{2})",
        r"\b(?:EXP|BB)[:\s](\d{2}/\d{4})\b",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return "—"


def _extract_ref(text: str) -> str:
    """Wyciąga numer REF (katalogowy) produktu medycznego."""
    patterns = [
        r"(?:REF|Ref\.?|Cat\.?\s*No\.?|Catalog|Katalog|Nr\s*(?:kat\.?|ref\.?))[:\s#]+([A-Z0-9][-A-Z0-9\.]{2,20})",
        r"\bREF[:\s]([A-Z0-9][-A-Z0-9\.]{2,20})\b",
        r"(?:GTIN|EAN|UPC)[:\s]+(\d{8,14})",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return "—"


_FILENAME_NOISE = re.compile(
    r"(?:_v\d+|_r\d+|_rev\d*|_master|_factory|_artwork|_art|_print|_final|_draft|"
    r"_approved|_proof|_supplier|_client|_new|_old|_ok|_ver\d*|_d\d{4,}|\d{8,}|"
    r"\.(pdf|ai|eps|tif|tiff|png|jpg))$",
    re.IGNORECASE
)


def _product_name_from_filename(path: str) -> str:
    """Wyciąga nazwę produktu z nazwy pliku, usuwając typowe sufiksy."""
    name = os.path.basename(path or "")
    name = os.path.splitext(name)[0]
    # Iteracyjnie usuwaj szum z końca
    for _ in range(6):
        prev = name
        name = _FILENAME_NOISE.sub("", name)
        if name == prev:
            break
    name = name.replace("_", " ").strip()
    return name[:80] if len(name) > 4 else "—"


def _extract_product_name(text: str) -> str:
    """Wyciąga nazwę produktu z tekstu opakowania (fallback gdy brak ścieżki pliku)."""
    # Unikaj linii wyglądających jak Pantone/CMYK, numery, kody
    skip = re.compile(r"(?:PANTONE|CMYK|P\s+\d{3,4}|\bCE\b|MDR|\bISO\b|\bEN\b|^\d)", re.IGNORECASE)
    lines = [l.strip() for l in text.splitlines() if len(l.strip()) > 8]
    for line in lines[:30]:
        if len(line) > 10 and line.isupper() and not skip.search(line) and not re.match(r"^\d+", line):
            return line[:80]
    for line in lines[:15]:
        if len(line) > 10 and not re.match(r"^[\d.]+$", line) and not skip.search(line):
            return line[:80]
    return "—"


def _extract_ean(text: str) -> str:
    """Wyciąga 13-cyfrowy EAN z tekstu — obsługuje spacje między cyframi (np. '5 907996 822690').
    Kompaktuje spacje tylko w obrębie linii, żeby nie sklejać cyfr z różnych wierszy."""
    result = "—"
    for line in text.splitlines():
        # Zero-width lookaround usuwa WSZYSTKIE pojedyncze spacje między cyframi w
        # jednym przebiegu (także w pełni rozstrzelone "5 9 0 7 9 9 6 8 2 2 6 9 0"),
        # czego dwa przebiegi (\d) (\d) nie domykały.
        c = re.sub(r"(?<=\d)\s+(?=\d)", "", line)
        m = re.search(r"(?<!\d)(\d{13})(?!\d)", c)
        if m:
            return m.group(1)
    return result


def _extract_gauge(text: str) -> str:
    """Wyciąga gauge/rozmiar wyrobu medycznego — ignoruje masę (g/kg) i objętość (ml)."""
    # Priorytet: jawny rozmiar (XS/S/M/L/XL/XXL) z jednostką gauge/Fr/Ch lub mm
    size_code = re.search(r"\b(XS|S|M|L|XL|XXL|XXXL)\b", text)
    # Gauge/French/Charriere
    gauge_m = re.search(
        r"(?<!\w)(\d{1,2}(?:[.,]\d+)?)\s*(Fr(?:ench)?|Ch(?:arriere)?|(?-i:G)(?!\.?[Ww])\b|gauge)(?!\s*[wW])",
        text, re.IGNORECASE
    )
    # mm — tylko w kontekście wymiarów (OD/ID/długość) nie masy
    mm_m = re.search(
        r"(?:OD|ID|długość|length|Ø|diam)[:\s]*(\d{1,3}(?:[.,]\d+)?)\s*mm",
        text, re.IGNORECASE
    )
    parts = []
    if size_code:
        parts.append(size_code.group(1))
    if gauge_m:
        parts.append(f"{gauge_m.group(1)} {gauge_m.group(2)}")
    if mm_m and not gauge_m:
        parts.append(f"{mm_m.group(1)} mm")
    return " / ".join(parts) if parts else "—"


def _build_field_report(full_a: str, full_b: str,
                         badges_a: list, badges_b: list,
                         dims_a: tuple, dims_b: tuple,
                         use_ai: bool = False,
                         file_a: str = "", file_b: str = "") -> list:
    def gauge_str(badges):
        if not badges:
            return "—"
        b = max(badges, key=lambda x: x["area"])
        parts = []
        if b.get("gauge"):
            parts.append(b["gauge"])
        if b.get("mm"):
            parts.append(" × ".join(b["mm"]))
        return " / ".join(parts) if parts else b.get("text", "—")[:40]

    rows = []

    # Pola krytyczne — "N/D vs N/D" to nie "Zgodny", tylko "Nie zweryfikowano" (ważne ostrzeżenie)
    _CRITICAL_MUST_HAVE = {"EAN / GTIN", "REF / Nr katalogowy", "Rozmiar / Gauge",
                            "Oznaczenie CE / MDR", "Sterylność / Jednorazowy"}

    def row(field, va, vb, sev, note=""):
        va = (str(va).strip() if va is not None else None) or "N/D"
        vb = (str(vb).strip() if vb is not None else None) or "N/D"
        if va == "—":
            va = "N/D"
        if vb == "—":
            vb = "N/D"
        both_nd = va == "N/D" and vb == "N/D"
        changed = va != vb and not both_nd
        # Confidence: 0.0 = both sides N/D (OCR failed), 0.5 = one side, 1.0 = both extracted
        ocr_confidence = 0.0 if both_nd else (1.0 if (va != "N/D" and vb != "N/D") else 0.5)
        # Dla pól krytycznych brak weryfikacji obu stron = ważne ostrzeżenie
        if both_nd and sev == "critical" and field in _CRITICAL_MUST_HAVE:
            changed = True
            va = "Nie zweryfikowano (OCR)"
            vb = "Nie zweryfikowano (OCR)"
            sev = "important"
            note = (note + " | " if note else "") + "Brak danych OCR dla obu stron — zweryfikuj ręcznie"
        rows.append({"field": field, "val_a": va, "val_b": vb,
                     "severity": sev, "changed": changed, "note": note,
                     "ocr_confidence": ocr_confidence})

    # Format dokumentu — PIERWSZA POZYCJA (wpływa na jakość pixel diff)
    row("Format dokumentu",
        f"{dims_a[0]}×{dims_a[1]} mm" if dims_a[0] else "N/D",
        f"{dims_b[0]}×{dims_b[1]} mm" if dims_b[0] else "N/D",
        "critical", note="Zmiana formatu arkusza — pixel diff pominięty dla różnych formatów")

    # Nazwa produktu — priorytet: z nazwy pliku
    name_a = _product_name_from_filename(file_a) if file_a else "—"
    name_b = _product_name_from_filename(file_b) if file_b else "—"
    if name_a == "—":
        name_a = _extract_product_name(full_a)
    if name_b == "—":
        name_b = _extract_product_name(full_b)
    row("Nazwa produktu", name_a, name_b, "important",
        note="Nazwa wyrobu z nazwy pliku lub pierwszej linii tekstu")

    # Rewizja dokumentu
    row("Rewizja / Wersja",
        _first(full_a, r"(?:Rev\.?|Wer\.?|Ver\.?|Version|Rewizja)[:\s]*([\d]+(?:\.\d+)*)"),
        _first(full_b, r"(?:Rev\.?|Wer\.?|Ver\.?|Version|Rewizja)[:\s]*([\d]+(?:\.\d+)*)"),
        "critical")

    # EAN barcode (13 cyfr) — obsługa spacji
    row("EAN / GTIN", _extract_ean(full_a), _extract_ean(full_b), "critical")

    # REF — bez fallbacku na EAN
    row("REF / Nr katalogowy",
        _extract_ref(full_a),
        _extract_ref(full_b), "critical")

    # LOT / EXP
    row("LOT / Nr serii",
        _extract_lot(full_a),
        _extract_lot(full_b), "critical",
        note="Numer serii/LOT — krytyczne dla identyfikacji partii")

    row("EXP / Data ważności",
        _extract_exp(full_a),
        _extract_exp(full_b), "critical",
        note="Data ważności produktu medycznego")

    # Gauge/rozmiar (dla wyrobów medycznych)
    ga = gauge_str(badges_a) if badges_a else _extract_gauge(full_a)
    gb = gauge_str(badges_b) if badges_b else _extract_gauge(full_b)
    row("Rozmiar / Gauge", ga, gb, "critical",
        note="Rozmiar wyrobu medycznego (gauge, Fr, CH, mm)")

    # Wymiary produktu (fizyczne)
    row("Wymiary produktu",
        _first(full_a, r"(\d{1,4})\s*[*×x]\s*(\d{1,4})\s*(?:[*×x]\s*(\d{1,4}))?\s*mm"),
        _first(full_b, r"(\d{1,4})\s*[*×x]\s*(\d{1,4})\s*(?:[*×x]\s*(\d{1,4}))?\s*mm"),
        "important", note="Wymiary fizyczne produktu w mm")

    # Specyfikacja koloru
    row("Specyfikacja koloru",
        _detect_color_spec(full_a),
        _detect_color_spec(full_b),
        "important", note="CMYK / Pantone — specyfikacja kolorów druku")

    # Sterylność / Single Use — "N/D" gdy brak (etykieta przed EAN)
    st_a = _first(full_a, r"(STERILE|STERYLNY|JAŁOWY|SINGLE\s+USE|JEDNORAZOWY|DO\s+NOT\s+REUSE)")
    st_b = _first(full_b, r"(STERILE|STERYLNY|JAŁOWY|SINGLE\s+USE|JEDNORAZOWY|DO\s+NOT\s+REUSE)")
    row("Sterylność / Jednorazowy",
        st_a if st_a and st_a != "—" else "N/D",
        st_b if st_b and st_b != "—" else "N/D",
        "critical", note="Wymagana informacja MDR dla wyrobów sterylnych")

    # Oznaczenie CE/MDR — "N/D" gdy brak
    ce_a = _first(full_a, r"(CE\s*\d{4}|MDR\s*\d{4}/\d+|CE\s+2|0318|0197)")
    ce_b = _first(full_b, r"(CE\s*\d{4}|MDR\s*\d{4}/\d+|CE\s+2|0318|0197)")
    row("Oznaczenie CE / MDR",
        ce_a if ce_a and ce_a != "—" else "N/D",
        ce_b if ce_b and ce_b != "—" else "N/D",
        "critical", note="Numer jednostki notyfikowanej — wymagane CE MDR")

    # Materiał
    mat_a = _first(full_a, r"(?:材质|[Mm]aterial|Materiał)[：:\s]*([^\n,;]{3,40})")
    mat_b = _first(full_b, r"(?:材质|[Mm]aterial|Materiał)[：:\s]*([^\n,;]{3,40})")
    if mat_a != "—" or mat_b != "—":
        row("Materiał", mat_a, mat_b, "important")

    # Data projektu
    dt_a = _first(full_a, r"(?:日期|[Dd]ate|Data\s+projektu)[：:\s]*([\d]{2,4}[./\-][\d]{1,2}[./\-][\d]{2,4}|[\d]{4}[./\-][\d]{2}[./\-][\d]{2})")
    dt_b = _first(full_b, r"(?:日期|[Dd]ate|Data\s+projektu)[：:\s]*([\d]{2,4}[./\-][\d]{1,2}[./\-][\d]{2,4}|[\d]{4}[./\-][\d]{2}[./\-][\d]{2})")
    if dt_a != "—" or dt_b != "—":
        row("Data projektu", dt_a, dt_b, "important")

    # Producent / EU REP / Dystrybutor — AI gdy dostępne, regex jako fallback
    if use_ai:
        _mfr = _ai_extract_manufacturer_blocks(full_a, full_b)
    else:
        _mfr = {}

    mfr_a = _mfr.get("manufacturer_a") or _first(full_a, r"(?:Manufactured\s+by|Producent|Manufacturer)[:\s]+([^\n]{5,60})")
    mfr_b = _mfr.get("manufacturer_b") or _first(full_b, r"(?:Manufactured\s+by|Producent|Manufacturer)[:\s]+([^\n]{5,60})")
    row("Producent / Manufacturer", mfr_a, mfr_b, "critical",
        note="Zmiana producenta lub adresu fabryki — weryfikuj MDR")

    eu_rep_a = _mfr.get("eu_rep_a") or _first(full_a, r"(?:EU\s*REP|Przedstawiciel\s+UE)[:\s]*([^\n]{5,80})")
    eu_rep_b = _mfr.get("eu_rep_b") or _first(full_b, r"(?:EU\s*REP|Przedstawiciel\s+UE)[:\s]*([^\n]{5,80})")
    row("EU REP", eu_rep_a, eu_rep_b, "critical",
        note="Zmiana przedstawiciela UE — wymagane MDR")

    dist_a = _mfr.get("distributor_a") or "—"
    dist_b = _mfr.get("distributor_b") or "—"
    if dist_a != "—" or dist_b != "—":
        row("Dystrybutor / Importer", dist_a, dist_b, "important")

    # G.W. / N.W.
    gw_a = _first(full_a, r"G\.?W\.?[:\s]*([\d,\.]+\s*(?:KGS?|kg))")
    gw_b = _first(full_b, r"G\.?W\.?[:\s]*([\d,\.]+\s*(?:KGS?|kg))")
    row("G.W. / N.W.", gw_a, gw_b, "important")

    return rows


# ─── MULTI-FILE EXTRACTION ────────────────────────────────────────────────────

def extract_artwork_fields(pdf_path: str) -> dict:
    """Wyciąga pola strukturalne z jednego PDF artworku (na potrzeby porównania multi-plik)."""
    texts = _extract_text_from_pdf(pdf_path) if pdf_path.lower().endswith(".pdf") else []

    if HAS_PIL:
        pages_ocr = _load_pages(pdf_path, OCR_DPI)
        for i, t in enumerate(texts):
            if len(t.strip()) < OCR_MIN_CHARS and i < len(pages_ocr):
                texts[i] = _extract_text_ocr(pages_ocr[i])
        if not texts:
            texts = [_extract_text_ocr(p) for p in pages_ocr]

    full = "\n".join(texts)
    dims = _get_dims_mm(pdf_path)

    all_pantones: list = []
    for t in texts:
        found = re.findall(r"PANTONE\s+[\w\-]+(?:\s+[CUMV](?:\s|$)|\s+C\b)?", t, re.IGNORECASE)
        all_pantones.extend(p.strip() for p in found)
    colors = " + ".join(dict.fromkeys(all_pantones)) if all_pantones else _detect_color_spec(full)

    return {
        "dims":      f"{dims[0]}×{dims[1]} mm" if dims[0] else "—",
        "pages":     len(texts),
        "revision":  _first(full, r"(?:Rev(?:ision)?\.?|Wer\.?|Ver(?:sion)?\.?|Rewizja|r)[:\s]*([vV]?\d+[\._]\d+(?:[\._]\d+)*|[vV]\d+(?:\.\d+)*|\d+\.\d+(?:\.\d+)*)"),
        "ean":       _first(full, r"\b(\d{13})\b"),
        "ref":       _extract_ref(full),
        "lot":       _extract_lot(full),
        "exp":       _extract_exp(full),
        "colors":    colors,
        "sterility": _first(full, r"(STERILE|STERYLNY|JAŁOWY|SINGLE\s+USE|JEDNORAZOWY)", "—"),
        "ce":        _first(full, r"(CE\s*\d{4}|0318|0197)", "—"),
    }


def compare_multi_artworks(pdf_paths: list) -> dict:
    """Porównuje N artworków — buduje macierz rozbieżności pól."""
    from collections import Counter

    FIELD_DEFS = [
        ("revision",  "Rewizja",              "critical"),
        ("ean",       "EAN / GTIN",            "critical"),
        ("ref",       "REF / Nr katalogowy",   "critical"),
        ("lot",       "LOT / Nr serii",        "important"),
        ("exp",       "EXP / Data ważności",   "important"),
        ("colors",    "Kolory (Pantone/CMYK)", "important"),
        ("dims",      "Format dokumentu",      "info"),
        ("sterility", "Sterylność",            "important"),
        ("ce",        "CE / MDR",              "important"),
    ]

    files = [os.path.basename(p) for p in pdf_paths]
    extracted = []
    for p in pdf_paths:
        try:
            extracted.append(extract_artwork_fields(p))
        except Exception as e:
            extracted.append({"error": str(e)[:200]})

    matrix = []
    mismatch_count = 0
    ok_count = 0

    for key, label, severity in FIELD_DEFS:
        values = [e.get(key, "—") for e in extracted]
        non_dash = [v for v in values if v != "—"]

        if not non_dash or len(set(non_dash)) <= 1:
            mismatch = False
            consensus = non_dash[0] if non_dash else "—"
            mismatch_indices: list = []
            ok_count += 1
        else:
            if non_dash:
                consensus = Counter(non_dash).most_common(1)[0][0]
            else:
                consensus = "—"
            mismatch_indices = [i for i, v in enumerate(values) if v != "—" and v != consensus]
            mismatch = True
            mismatch_count += 1

        matrix.append({
            "key":             key,
            "label":           label,
            "severity":        severity,
            "values":          values,
            "consensus":       consensus,
            "mismatch":        mismatch,
            "mismatch_indices": mismatch_indices,
        })

    return {
        "files":          files,
        "fields":         matrix,
        "mismatch_count": mismatch_count,
        "ok_count":       ok_count,
    }


# ─── TEXT DIFF ────────────────────────────────────────────────────────────────

TEXT_DIFF_CATEGORIES = {
    "critical": ["batch", "lot", "exp", "expiry", "ważność", "ref", "gtin",
                 "sterile", "jałowy", "latex free", "caution", "warning",
                 "ostrzeżenie", "do not reuse", "single use", "jednorazowy"],
    "important": ["name", "nazwa", "product", "producent", "manufacturer",
                  "ce ", "mdr", "size", "rozmiar", "gauge", "quantity", "ilość",
                  "storage", "przechowywać", "temperature"],
    "info": ["address", "adres", "phone", "tel", "www", "email", "made in"],
}


def _cat_diff(text: str) -> tuple:
    t = text.lower()
    for cat, kws in TEXT_DIFF_CATEGORIES.items():
        if any(k in t for k in kws):
            sev = "error" if cat == "critical" else "warning" if cat == "important" else "info"
            return cat, sev
    return "other", "info"


# ─── SEKCJE ARTWORKU ──────────────────────────────────────────────────────────
# Każda sekcja ma listę wzorców regex. Linia trafia do pierwszej pasującej sekcji.
# Linie z nielatyńskimi znakami (chiński, arabski itp.) → "dane_dostawcy".
# Linie bez dopasowania → "tlumaczenia" (domyślna sekcja).

SECTION_DEFS = [
    {
        "key": "kody_kreskowe",
        "label": "Kody kreskowe (GS1 / UDI)",
        "severity": "critical",
        "patterns": [
            r"\(0[12]\)\d+",
            r"\(17\)\d{6}",
            r"\(10\)[A-Z0-9]",
            r"(?:GTIN|GS1-128|UDI|DataMatrix)\b",
        ],
    },
    {
        "key": "dane_zmienne",
        "label": "Dane zmienne (LOT / EXP / daty)",
        "severity": "critical",
        "patterns": [
            r"(?:LOT|BATCH|SERIA|Nr\s*serii)\s*[:\s#]",
            r"(?:EXP|USE\s*BY|DATA\s*WAŻNOŚCI|WAŻNY\s*DO|EXPIRY)\s*[:\s]",
            r"(?:MFG|MANUFACTURED|DATA\s*PROD(?:UKCJI)?)\s*[:\s]",
            r"\b\d{2}[./]\d{2}[./]\d{4}\b",
            r"\b\d{4}-\d{2}-\d{2}\b",
        ],
    },
    {
        "key": "dane_produktu",
        "label": "Dane produktu (REF / EAN / rozmiar)",
        "severity": "critical",
        "patterns": [
            r"(?:REF|Cat\.?\s*No\.?)\s*[:\s#]",
            r"(?:EAN|GTIN)\s*[:\s]",
            r"\b\d{13}\b",
            r"(?:rozmiar|size|gauge)\s*[:\s]",
            r"\b\d+(?:[,\.]\d+)?\s*(?:G\b|Fr\b|CH\b)",
            r"\b\d+\s*[xX×]\s*\d+\s*(?:mm|cm)\b",
        ],
    },
    {
        "key": "symbole_mdr",
        "label": "Symbole i oznaczenia MDR",
        "severity": "critical",
        "patterns": [
            r"(?:CE\s*\d{4}|MDR\s*20\d{2}/\d+|0318|0197)",
            r"(?:STERILE|STERYLNY|JAŁOWY)\b",
            r"\bEO\b",
            r"(?:SINGLE\s*USE|DO\s*NOT\s*REUSE|JEDNORAZOWY)",
            r"(?:CAUTION|WARNING|OSTRZEŻENIE)\b",
            r"(?:LATEX\s*FREE|BEZ\s*LATEKSU)",
            r"\bIFU\b",
            r"\bRx\s*ONLY\b",
        ],
    },
    {
        "key": "dane_producenta",
        "label": "Dane producenta / dystrybutora",
        "severity": "important",
        "patterns": [
            r"(?:ACME|Manufactured\s*by|Producent|Manufacturer)\b",
            r"(?:CH\s*REP|UA\s*REP|AU\s*REP|Authorized\s*Rep)",
            r"(?:ul\.|str\.|aleja|al\.)\s+\w",
            r"\b\d{2}-\d{3}\b",
            r"(?:www\.|http|\.[a-z]{2,3}/|\.com|\.pl|\.eu)\b",
            r"sp\.\s*z\s*o\.o\.",
            r"GmbH|Ltd\.|Inc\.|S\.A\.",
        ],
    },
    {
        "key": "dane_fabryczne",
        "label": "Dane fabryczne / rewizja",
        "severity": "critical",
        "patterns": [
            r"(?:Rev\.|Wer\.|Version|Rewizja)\s*[:\s]*[\d]",
            r"(?:Opracow(?:ał|ała)|Zatwierdzil|Zatwierdz(?:ił|iła))\s*[:\s]",
            r"(?:Approved\s*by|Drawn\s*by|Checked\s*by)\s*[:\s]",
            r"(?:Data\s*zatwierdzenia|Date\s*of\s*approval)\s*[:\s]",
        ],
    },
    {
        "key": "wymiary_kolory",
        "label": "Wymiary i specyfikacja kolorów",
        "severity": "important",
        "patterns": [
            r"PANTONE\s+[\w\-]+(?:\s+[CUMV]\b)?",
            r"(?:CMYK|RGB)\s*[\d/,]+",
            r"(?:Format|Rozmiar\s*etykiety|Label\s*size)\s*[:\s]",
            r"(?:G\.W\.|N\.W\.|Gross\s*[Ww]eight|Net\s*[Ww]eight)\s*[:\s]",
            r"(?:Typ\s*opakowania|Package\s*type|Packaging\s*type)\s*[:\s]",
            r"\b\d+\s*[×xX]\s*\d+\s*mm\b",
            r"(?:karton\s*transportowy|transport\s*carton)",
        ],
    },
]

# Wzorzec do wykrywania niełacińskich znaków (chiński, arabski, japoński itp.)
_NON_LATIN_RE = re.compile(
    r'[一-鿿぀-ゟ゠-ヿ؀-ۿ'
    r'Ѐ-ӿͰ-Ͽ가-힯]'
)


def _classify_line(line: str) -> str:
    """Klasyfikuje linię tekstu do jednej z sekcji artworku."""
    if _NON_LATIN_RE.search(line):
        return "dane_dostawcy"
    for sec in SECTION_DEFS:
        for pat in sec["patterns"]:
            if re.search(pat, line, re.IGNORECASE):
                return sec["key"]
    return "tlumaczenia"


def _split_into_sections(text: str) -> dict:
    """Dzieli tekst na sekcje na podstawie klasyfikacji każdej linii."""
    sections: dict = {s["key"]: [] for s in SECTION_DEFS}
    sections["tlumaczenia"] = []
    sections["dane_dostawcy"] = []
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) < 3:
            continue
        sections[_classify_line(line)].append(line)
    return sections


def _compare_sections(text_a: str, text_b: str) -> list:
    """
    Porównuje dwa teksty sekcja do sekcji.
    Zwraca listę sekcji z parami linii (content_a, content_b, status).
    status: 'equal' | 'changed' | 'only_master' | 'only_factory'
    """
    secs_a = _split_into_sections(text_a)
    secs_b = _split_into_sections(text_b)

    all_keys = [s["key"] for s in SECTION_DEFS] + ["tlumaczenia", "dane_dostawcy"]
    sec_meta = {s["key"]: s for s in SECTION_DEFS}
    sec_meta["tlumaczenia"]   = {"label": "Tłumaczenia", "severity": "important"}
    sec_meta["dane_dostawcy"] = {"label": "Dane dostawcy (znaki niełacińskie)", "severity": "info"}

    result = []
    for key in all_keys:
        la = secs_a.get(key, [])
        lb = secs_b.get(key, [])
        if not la and not lb:
            continue

        rows = []
        diff_count = 0
        for i in range(max(len(la), len(lb))):
            a = la[i] if i < len(la) else ""
            b = lb[i] if i < len(lb) else ""
            if a == b:
                status = "equal"
            elif not a:
                status = "only_factory"
                diff_count += 1
            elif not b:
                status = "only_master"
                diff_count += 1
            else:
                status = "changed"
                diff_count += 1
            _, sev = _cat_diff(a + " " + b)
            rows.append({"content_a": a, "content_b": b, "status": status, "severity": sev})

        severities = [r["severity"] for r in rows if r["status"] != "equal"]
        sec_sev = "error" if "error" in severities else "warning" if "warning" in severities else "info"

        result.append({
            "section_key":   key,
            "section_label": sec_meta[key]["label"],
            "severity":      sec_sev,
            "rows":          rows,
            "diff_count":    diff_count,
            "total_count":   len(rows),
        })
    return result


def _compare_texts(text_a: str, text_b: str) -> list:
    # Collapse ALL internal whitespace (str.split() treats NBSP/thin spaces as
    # whitespace too) symmetrically on both sides — otherwise justified multilingual
    # OCR where one side has a NBSP and the other a plain space is a phantom diff.
    la = [" ".join(l.split()) for l in text_a.splitlines()]
    lb = [" ".join(l.split()) for l in text_b.splitlines()]
    la = [l for l in la if len(l) >= 4]
    lb = [l for l in lb if len(l) >= 4]
    if not la and not lb:
        return []
    diffs = []
    n = max(len(la), len(lb))
    for i in range(n):
        a = la[i] if i < len(la) else ""
        b = lb[i] if i < len(lb) else ""
        if a == b:
            continue
        cat, sev = _cat_diff(a + " " + b)
        diffs.append({
            "type": "replace",
            "content_a": a,
            "content_b": b,
            "category": cat,
            "severity": sev,
        })
    return diffs


# ─── AI SECTION DECOMPOSITION ─────────────────────────────────────────────────

_DECOMPOSE_SYSTEM = """Jesteś ekspertem ds. etykiet wyrobów medycznych ISO 15223 / MDR.
Klasyfikujesz linie tekstu OCR z etykiety opakowania do sekcji semantycznych.
Każdą linię/frazę przypisz do sekcji — nie pomijaj żadnej linii tekstu, nawet krótkich.
Zwracasz WYŁĄCZNIE JSON — bez markdown, bez komentarzy."""

# Klucz → (etykieta w raporcie, poziom ważności)
_DECOMPOSE_SECTIONS = {
    "tłumaczenia":      ("Tłumaczenia nazwy i opisu produktu (wszystkie języki)", "important"),
    "instrukcje_ifu":   ("Instrukcje użycia / IFU (wszystkie języki)",           "important"),
    "dane_produktu":    ("Dane produktu (REF / EAN / rozmiar)",                   "critical"),
    "dane_zmienne":     ("Dane zmienne (LOT / EXP / daty)",                       "critical"),
    "symbole_mdr":      ("Symbole i oznaczenia MDR",                              "critical"),
    "dane_producenta":  ("Dane producenta / dystrybutora",                        "important"),
    "dane_fabryczne":   ("Dane fabryczne / rewizja",                              "critical"),
    "wymiary_kolory":   ("Wymiary i specyfikacja kolorów",                        "important"),
    "kody_kreskowe":    ("Kody kreskowe (GS1 / UDI)",                       "critical"),
    "normy_ostrzezenia":("Normy, ostrzeżenia, warunki przechowywania",     "important"),
    "inne":             ("Inne",                                           "info"),
}


def _ai_decompose_label(ocr_text: str) -> dict:
    """
    Wysyła tekst OCR etykiety do Claude i dostaje z powrotem słownik sekcji.
    Działa niezależnie od układu/obrotu etykiety — klasyfikuje po treści.
    Zwraca {} przy braku klucza API lub błędzie (fallback do regexów).
    """
    import urllib.request as _ur
    api_key = _get_api_key_artwork()
    if not api_key or len(ocr_text.strip()) < 20:
        return {}

    sections_desc = "\n".join(
        f'  "{k}": {label}'
        for k, (label, _) in _DECOMPOSE_SECTIONS.items()
    )
    empty_json = json.dumps({k: [] for k in _DECOMPOSE_SECTIONS}, ensure_ascii=False)

    prompt = (
        "Tekst OCR z etykiety wyrobu medycznego (może być odwrócona/rotowana, układ niestandarod.).\n"
        "ZADANIE: Przypisz KAŻDĄ linię/frazę do JEDNEJ sekcji. Nie pomijaj żadnej linii.\n\n"
        f"SEKCJE:\n{sections_desc}\n\n"
        "ZASADY:\n"
        "- tłumaczenia: nazwa produktu w PL/EN/DE/FR/ES/PT/IT/RO/HU/CZ/SK/BG/RU/UA/AR/HE i inne\n"
        "  — KAŻDA wersja językowa to osobna pozycja w liście\n"
        "- instrukcje_ifu: 'Do jednorazowego użytku', 'Nie używać jeśli...', 'Przechowywać w...',\n"
        "  temperatura, wilgotność, wskazania do stosowania — we WSZYSTKICH językach\n"
        "- normy_ostrzezenia: EN ISO, EN 455, CAUTION, WARNING, numery norm, klasy AQL, temperatury\n"
        "- dane_zmienne: LOT, EXP, daty — uwzględnij placeholdery (□□□, XXXXXX, puste ramki)\n"
        "- kody_kreskowe: numery GS1 z AI (01)(10)(17) lub 13-cyfrowe EAN\n"
        "- WSZYSTKO trafia do jakiejś sekcji — nic nie pomijaj\n\n"
        f"TEKST OCR:\n{ocr_text[:6000]}\n\n"
        f"Zwróć WYŁĄCZNIE JSON:\n{empty_json}"
    )

    payload = json.dumps({
        "model": _current_ai_model(),
        "max_tokens": 3000,
        "system": _DECOMPOSE_SYSTEM,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = _ur.Request(
        "https://api.anthropic.com/v1/messages", data=payload,
        headers={"Content-Type": "application/json", "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"}, method="POST")
    try:
        # Bandit B310: URL stały w kodzie (https://api.anthropic.com), schemat nie pochodzi z wejścia.
        with _ur.urlopen(req, timeout=40) as resp:  # nosec B310
            data = json.loads(resp.read())
        try:
            from api_usage_tracker import record_usage
            record_usage(_current_ai_model(), data.get("usage", {}), call_type="artwork_decompose")
        except Exception:
            pass
        _content = data.get("content") or []
        if not _content:
            return {}
        text = (_content[0].get("text") or "").strip()
        text = re.sub(r"^```(?:json)?\s*", "", text).strip().rstrip("`").strip()
        if not text.startswith("{"):
            m = re.search(r"\{[\s\S]+\}", text)
            if m:
                text = m.group()
        result = json.loads(text)
        for k in _DECOMPOSE_SECTIONS:
            if k not in result:
                result[k] = []
        return result
    except Exception as _exc:
        logger.warning("Section AI decomposition failed: %s", _exc)
        return {}


def _match_section_items(la: list, lb: list) -> list:
    """
    Dopasowuje elementy dwóch list sekcji.
    Używa rapidfuzz (fuzzy) jeśli dostępny, inaczej difflib.
    Obsługuje zmienioną kolejność elementów.
    """
    if not la and not lb:
        return []

    try:
        from rapidfuzz import fuzz as _fuzz
        use_fuzzy = True
    except ImportError:
        use_fuzzy = False

    rows = []

    if not use_fuzzy:
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, la, lb).get_opcodes():
            if op == "equal":
                for k in range(i2 - i1):
                    rows.append({"content_a": la[i1+k], "content_b": lb[j1+k], "status": "equal"})
            else:
                for k in range(max(i2 - i1, j2 - j1)):
                    a = la[i1+k] if op != "insert" and (i1+k) < i2 else ""
                    b = lb[j1+k] if op != "delete" and (j1+k) < j2 else ""
                    st = "changed" if a and b else ("only_master" if a else "only_factory")
                    rows.append({"content_a": a, "content_b": b, "status": st})
        return rows

    # Fuzzy matching — radzi sobie ze zmienioną kolejnością
    used_b: set = set()
    for a in la:
        best_score, best_j, best_b = 0, -1, ""
        for j, b in enumerate(lb):
            if j in used_b:
                continue
            score = _fuzz.ratio(a.lower(), b.lower())
            if score > best_score:
                best_score, best_j, best_b = score, j, b
        if best_score >= 95:
            rows.append({"content_a": a, "content_b": best_b, "status": "equal"})
            used_b.add(best_j)
        elif best_score >= 70:
            rows.append({"content_a": a, "content_b": best_b, "status": "changed"})
            used_b.add(best_j)
        else:
            rows.append({"content_a": a, "content_b": "", "status": "only_master"})

    for j, b in enumerate(lb):
        if j not in used_b:
            rows.append({"content_a": "", "content_b": b, "status": "only_factory"})

    return rows


def _compare_sections_ai(text_a: str, text_b: str) -> list:
    """
    AI-based section comparison.
    Rozkłada oba teksty OCR przez Claude na sekcje semantyczne,
    potem porównuje sekcję po sekcji z fuzzy matchingiem.
    Fallback do regex _compare_sections gdy brak klucza API.
    """
    import concurrent.futures

    if not _get_api_key_artwork():
        return _compare_sections(text_a, text_b)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            fut_a = pool.submit(_ai_decompose_label, text_a)
            fut_b = pool.submit(_ai_decompose_label, text_b)
            secs_a = fut_a.result(timeout=45)
            secs_b = fut_b.result(timeout=45)
    except Exception:
        # Timeout/błąd API nie może wywalić całego porównania — wróć do regexu.
        return _compare_sections(text_a, text_b)

    if not secs_a and not secs_b:
        return _compare_sections(text_a, text_b)

    result = []
    for key, (label, _) in _DECOMPOSE_SECTIONS.items():
        la = [s.strip() for s in (secs_a.get(key) or []) if s.strip()]
        lb = [s.strip() for s in (secs_b.get(key) or []) if s.strip()]
        if not la and not lb:
            continue

        rows_raw = _match_section_items(la, lb)
        rows = []
        for r in rows_raw:
            _, sev = _cat_diff(r["content_a"] + " " + r["content_b"])
            rows.append({**r, "severity": sev})

        diff_count = sum(1 for r in rows if r["status"] != "equal")
        severities  = [r["severity"] for r in rows if r["status"] != "equal"]
        sec_sev     = "error" if "error" in severities else "warning" if "warning" in severities else "info"

        result.append({
            "section_key":   key,
            "section_label": label,
            "severity":      sec_sev,
            "rows":          rows,
            "diff_count":    diff_count,
            "total_count":   len(rows),
            "ai_decomposed": True,
        })

    return result


# ─── DETEKCJA I PORÓWNANIE REGIONÓW GRAFICZNYCH (IKON) ───────────────────────

# Rozmiar ikon w mm — mniejsze = szum, większe = bloki tekstowe lub zdjęcia produktu
_ICON_MIN_MM = 4
_ICON_MAX_MM = 50   # ↓ z 70 — wyklucza duże bloki tekstowe fałszywie wykryte jako ikony
# Minimalna gęstość ciemnych pikseli w bbox (filtruje puste obszary i rzadkie teksty)
_ICON_MIN_DENSITY = 0.10  # ↑ z 0.04 — wymaga gęstszej treści graficznej
# Tolerancja dopasowania pozycji ikon między A i B (% wymiaru strony)
_ICON_MATCH_TOL = 15.0


def _detect_icon_regions(img, dpi: int = RENDER_DPI) -> list:
    """
    Wykrywa dyskretne regiony graficzne (ikony, piktogramy) na obrazie etykiety.
    Zwraca listę dict z {'x_pct','y_pct','w_pct','h_pct','center_x','center_y'}.
    Wymaga PIL + numpy + scipy (dostępne w produkcji).
    """
    if not HAS_PIL or not HAS_SCIPY:
        return []
    try:
        arr = np.array(img.convert("L"))
        W, H = img.size
        scale = dpi / 25.4  # px/mm

        # Binaryzacja — piksele ciemniejsze niż 210/255
        binary = arr < 210

        # Dylatacja — łączy sąsiednie piksele tej samej ikony
        iters = max(2, int(scale * 1.5))  # ~1.5mm at render DPI
        dilated = ndimage.binary_dilation(binary, iterations=iters)

        labeled, num = ndimage.label(dilated)

        min_px = int(_ICON_MIN_MM * scale)
        max_px = int(_ICON_MAX_MM * scale)

        regions = []
        for rid in range(1, num + 1):
            mask = labeled == rid
            ys, xs = np.where(mask)
            y1, y2 = int(ys.min()), int(ys.max())
            x1, x2 = int(xs.min()), int(xs.max())
            w, h = x2 - x1, y2 - y1

            if w < min_px or h < min_px:
                continue
            if w > max_px or h > max_px:
                continue
            # Filtruj zbyt wydłużone (kody kreskowe, linie)
            ratio = max(w, h) / max(min(w, h), 1)
            if ratio > 8:
                continue
            # Gęstość ciemnych pikseli w oryginalnym (nie dilated) bbox
            density = float(binary[y1:y2+1, x1:x2+1].sum()) / max((w * h), 1)
            if density < _ICON_MIN_DENSITY:
                continue

            regions.append({
                "x_pct":    round(x1 / W * 100, 2),
                "y_pct":    round(y1 / H * 100, 2),
                "w_pct":    round(w  / W * 100, 2),
                "h_pct":    round(h  / H * 100, 2),
                "center_x": (x1 + x2) / 2 / W * 100,
                "center_y": (y1 + y2) / 2 / H * 100,
                "density":  round(density, 3),
            })

        # Sortuj: wiersz (co 3%) → kolumna
        regions.sort(key=lambda r: (int(r["center_y"] / 3), r["center_x"]))
        return regions
    except Exception:
        return []


def _crop_icon(img, region: dict, pad_px: int = 8) -> Optional["Image.Image"]:
    """Wycina region z obrazu z marginesem pad_px."""
    if img is None or region is None:
        return None
    W, H = img.size
    x1 = max(0, int(region["x_pct"] / 100 * W) - pad_px)
    y1 = max(0, int(region["y_pct"] / 100 * H) - pad_px)
    x2 = min(W, int((region["x_pct"] + region["w_pct"]) / 100 * W) + pad_px)
    y2 = min(H, int((region["y_pct"] + region["h_pct"]) / 100 * H) + pad_px)
    if x2 <= x1 or y2 <= y1:
        return None
    return img.crop((x1, y1, x2, y2))


def _icon_similarity(crop_a, crop_b) -> float:
    """Visual similarity 0–100% between two crops.

    Pipeline:
    1. Perceptual hash (fast pre-filter) — if nearly identical, return early.
    2. SSIM (structural similarity) — perceptually better than raw pixel diff.
    3. DINOv2 cosine similarity — semantic feature comparison for complex icons.
    4. Raw pixel diff — final fallback.
    """
    if crop_a is None or crop_b is None:
        return 0.0
    try:
        W = max(crop_a.width, crop_b.width)
        H = max(crop_a.height, crop_b.height)
        ra = crop_a.resize((W, H), Image.LANCZOS)
        rb = crop_b.resize((W, H), Image.LANCZOS)

        # 1. Perceptual hash pre-filter
        ph = _phash_similarity(ra, rb)
        if ph >= 0:
            if ph >= 0.97:   # nearly identical
                return 100.0
            if ph <= 0.40:   # very different — skip expensive models
                return round(ph * 100, 1)

        # 2. SSIM (structural similarity)
        ssim = _ssim_score(ra, rb)
        if ssim >= 0:
            # High confidence: use SSIM directly
            if ssim >= 0.90 or ssim <= 0.30:
                return round(float(ssim) * 100, 1)
            # Mid-range: blend with DINOv2 for more semantic accuracy
            dino = _dinov2_similarity(ra, rb)
            if dino >= 0:
                score = 0.5 * ssim + 0.5 * dino
                return round(float(score) * 100, 1)
            return round(float(ssim) * 100, 1)

        # 3. DINOv2 standalone (when scikit-image unavailable)
        dino = _dinov2_similarity(ra, rb)
        if dino >= 0:
            return round(float(dino) * 100, 1)

        # 4. Raw pixel diff fallback
        a = np.array(ra.convert("L")).astype(float)
        b = np.array(rb.convert("L")).astype(float)
        sim = 1.0 - np.abs(a - b).mean() / 255.0
        return round(float(sim) * 100, 1)
    except Exception:
        return 0.0


_ICON_CONTENT_MATCH_SIM = 65.0  # min similarity for content-based (layout-shifted) match; ↑ z 45 — redukuje fałszywe pary


def compare_icon_regions(img_a, img_b,
                          dpi: int = RENDER_DPI,
                          hires_a=None, hires_b=None) -> list:
    """
    Wykrywa ikony na obu etykietach, dopasowuje parami i porównuje.

    Przebieg w 2 krokach:
    1. Dopasowanie pozycyjne (tolerancja _ICON_MATCH_TOL %).
    2. Dla niespasowanych ikon — dopasowanie treściowe (pixel similarity):
       jeśli podobieństwo >= _ICON_CONTENT_MATCH_SIM, para otrzymuje status
       "Inny układ" zamiast "Brak w B".

    Zwraca listę dict z kluczami:
      crop_a_b64, crop_b_b64, crop_a_hires, crop_b_hires,
      similarity, status, status_class,
      x_pct, y_pct (pozycja A), pos_b_x, pos_b_y (pozycja B gdy inny układ),
      layout_shift (bool)
    """
    if not HAS_PIL or not HAS_SCIPY:
        return []

    regions_a = _detect_icon_regions(img_a, dpi)
    regions_b = _detect_icon_regions(img_b, dpi)

    if not regions_a and not regions_b:
        return []

    # Pre-crop wszystkich ikon (potrzebne do similarity w kroku 2)
    crops_a = [_crop_icon(img_a, r) for r in regions_a]
    crops_b = [_crop_icon(img_b, r) for r in regions_b]

    # Krok 1 — pozycyjne dopasowanie
    used_b: set = set()
    matched: dict = {}  # i_a → (j_b or None, match_type)

    for i, ra in enumerate(regions_a):
        best_dist, best_j = float("inf"), -1
        for j, rb in enumerate(regions_b):
            if j in used_b:
                continue
            dist = ((ra["center_x"] - rb["center_x"]) ** 2 +
                    (ra["center_y"] - rb["center_y"]) ** 2) ** 0.5
            if dist < best_dist:
                best_dist, best_j = dist, j
        if best_j >= 0 and best_dist <= _ICON_MATCH_TOL:
            matched[i] = (best_j, "position")
            used_b.add(best_j)
        else:
            matched[i] = (None, "none")

    # Krok 2 — treściowe dopasowanie dla niespasowanych ikon A
    unmatched_b = [j for j in range(len(regions_b)) if j not in used_b]

    for i in range(len(regions_a)):
        if matched[i][0] is not None:
            continue
        best_sim, best_j = 0.0, -1
        for j_b in unmatched_b:
            sim = _icon_similarity(crops_a[i], crops_b[j_b])
            if sim > best_sim:
                best_sim, best_j = sim, j_b
        if best_sim >= _ICON_CONTENT_MATCH_SIM and best_j >= 0:
            # Only create layout match if icons are not too far apart in position
            ra_pos = regions_a[i]
            rb_pos = regions_b[best_j]
            pos_delta_x = abs(ra_pos["center_x"] - rb_pos["center_x"])
            pos_delta_y = abs(ra_pos["center_y"] - rb_pos["center_y"])
            # Skip pairing if position delta is extreme AND similarity is not very high
            # (high similarity + large delta = likely background match, not real icon match)
            if pos_delta_x > 35 and pos_delta_y > 20 and best_sim < 85:
                pass  # leave as "none" → will be "Brak w B"
            else:
                matched[i] = (best_j, "layout")
                unmatched_b.remove(best_j)
                used_b.add(best_j)

    # Zbuduj wynik
    matched_b_set = {j for (j, _) in matched.values() if j is not None}
    result = []

    for i, ra in enumerate(regions_a):
        j_b, match_type = matched[i]
        rb     = regions_b[j_b] if j_b is not None else None
        crop_a = crops_a[i]
        crop_b = crops_b[j_b] if j_b is not None else None
        sim    = _icon_similarity(crop_a, crop_b)

        if rb is None:
            status, status_cls = "Brak w B", "status-niezgodnosc"
        elif sim >= 92:
            # Wizualnie identyczna ikona — OK nawet jeśli w innym miejscu
            status, status_cls = "OK",        "status-ok"
        elif match_type == "layout" and sim >= 75:
            # Podobna ikona ale wyraźnie przesunięta — zmiana układu
            status, status_cls = "Inny układ", "status-niezgodnosc"
        elif sim >= 70:
            status, status_cls = "Roznica",   "status-niezgodnosc"
        else:
            status, status_cls = "Blad",      "status-niezgodnosc"

        # Hires crop: użyj obrazu OCR (150 DPI) jeśli dostępny — więcej pikseli na ikonę
        hires_crop_a = _crop_icon(hires_a, ra) if hires_a else crop_a
        hires_crop_b = _crop_icon(hires_b, rb) if (hires_b and rb) else crop_b

        # Pixel-diff annotated hires crops for non-OK pairs
        crop_a_hires_annot = None
        crop_b_hires_annot = None
        if status not in ("OK", "Brak w B") and hires_crop_a and hires_crop_b:
            try:
                _, ann_a, ann_b, _, _ = _compute_pixel_diff(hires_crop_a, hires_crop_b)
                crop_a_hires_annot = _img_to_b64(ann_a, max_width=600, quality=95)
                crop_b_hires_annot = _img_to_b64(ann_b, max_width=600, quality=95)
            except Exception:
                pass

        result.append({
            "crop_a_b64":         _img_to_b64(crop_a, max_width=180, quality=88) if crop_a else None,
            "crop_b_b64":         _img_to_b64(crop_b, max_width=180, quality=88) if crop_b else None,
            "crop_a_hires":       _img_to_b64(hires_crop_a, max_width=600, quality=95) if hires_crop_a else None,
            "crop_b_hires":       _img_to_b64(hires_crop_b, max_width=600, quality=95) if hires_crop_b else None,
            "crop_a_hires_annot": crop_a_hires_annot,
            "crop_b_hires_annot": crop_b_hires_annot,
            "similarity":   sim,
            "status":       status,
            "status_class": status_cls,
            "x_pct":        ra["x_pct"],
            "y_pct":        ra["y_pct"],
            "pos_b_x":      rb["x_pct"] if rb else None,
            "pos_b_y":      rb["y_pct"] if rb else None,
            "layout_shift": match_type == "layout",
        })

    # Ikony tylko w B (brak w A)
    for j in range(len(regions_b)):
        if j not in matched_b_set:
            rb          = regions_b[j]
            crop_b      = crops_b[j]
            hires_crop_b = _crop_icon(hires_b, rb) if hires_b else crop_b
            result.append({
                "crop_a_b64":   None,
                "crop_b_b64":   _img_to_b64(crop_b, max_width=180, quality=88) if crop_b else None,
                "crop_a_hires": None,
                "crop_b_hires": _img_to_b64(hires_crop_b, max_width=600, quality=95) if hires_crop_b else None,
                "similarity":   0.0,
                "status":       "Brak w A",
                "status_class": "status-niezgodnosc",
                "x_pct":        rb["x_pct"],
                "y_pct":        rb["y_pct"],
                "pos_b_x":      rb["x_pct"],
                "pos_b_y":      rb["y_pct"],
                "layout_shift": False,
            })

    return result


def _img_to_b64(img, max_width: int = THUMB_W, quality: int = 88) -> str:
    if img is None:
        return ""
    w, h = img.size
    if w > max_width:
        img = img.resize((max_width, int(h * max_width / w)), Image.LANCZOS)
    # JPEG can't encode RGBA/P crops ("cannot write mode RGBA as JPEG") — flatten.
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    # optimize=True runs a second Huffman pass — worth it for small thumbnails
    # (better compression, cheap) but it noticeably slows encoding of the larger
    # hi-res variants that get encoded many times per report. Skip it there.
    img.save(buf, format="JPEG", quality=quality, optimize=(max_width <= THUMB_W))
    return base64.b64encode(buf.getvalue()).decode("utf-8")


# ─── AI ANALIZA (VISION) ──────────────────────────────────────────────────────

_AI_SYSTEM = """Jesteś ekspertem ds. walidacji artworków opakowań wyrobów medycznych (MDR) w firmie ACME.
Twoja analiza musi być WYCZERPUJĄCA i RYGORYSTYCZNA — pominięcie różnicy to błąd krytyczny.

Porównaj WSZYSTKIE elementy obu artworków:

═══ KRYTYCZNE (każda różnica = critical) ═══
1. REF / Nr katalogowy — odczytaj z obu, porównaj cyfrę po cyfrze
2. EAN/GTIN 13 cyfr — porównaj wszystkie cyfry łącznie z cyfrą kontrolną
3. Rozmiar / Gauge / Fr / CH — czy zmieniony?
   3a. TABELA ROZMIARÓW RĘKAWIC: odczytaj KAŻDY wiersz (XS/S/M/L/XL) i wartości palców.
       "5-6" vs "6" to CRITICAL. Tabela może być obrócona lub w rogu.
4. Oznaczenie CE + numer notyfikowanej (0318/0197/2777) — czy zmienione?
5. Symbol jednorazowego użytku (pętla/strzałki) — obecny w obu? na tej samej pozycji?
6. Sterylność (STERILE, EO, R, JAŁOWY) — zmieniona?
7. Ostrzeżenia CAUTION/WARNING — czy KAŻDE jest obecne w obu?
8. LOT / EXP placeholdery — obecne w obu (puste prostokąty to NORMALNE w artworkach)
9. Numer rewizji — zmieniony?

═══ WAŻNE (każda różnica = important) ═══
10. Nazwa produktu — każde słowo, skrót, numer
11. Wszystkie TŁUMACZENIA (PL/EN/DE/FR/ES/PT/IT/RO/HU/CZ/SK/BG/RU/UA/AR/HE i inne):
    — Porównaj nazwy produktu, wskazania, instrukcje w KAŻDYM języku
    — Różna kolejność języków = important
    — Brakujący język = important
12. Producent / adres fabryki — każde słowo, ulica, kraj
13. EU REP (przedstawiciel UE) — adres, kraj
14. AQL, normy (EN ISO 374, EN 455, EN 13795 itp.) — zmienione numery?
15. Warunki przechowywania (temperatura, wilgotność) — zmienione wartości?
16. Piktogramy / ikony (temperatura, wilgotność, AQL, jednorazowe, sterylność) —
    czy KAŻDA ikona jest obecna? Czy jakaś zniknęła lub dodano nową?
17. Kolory (Pantone/CMYK) — każdy kolor z obu stron

═══ WIZUALNE (każda różnica = info lub więcej) ═══
18. Układ graficzny — zmieniony?
19. Logo producenta — zmienione?
20. Format / rozmiar arkusza — zmieniony?
21. Grafiki sekwencji zakładania — ta sama kolejność kroków?
22. Tabela rozmiarów (wizualna) — inne wartości choćby o 1 cyfrę?

ZASADY:
- Placeholder LOT/EXP (puste prostokąty) są NORMALNE — nie flagujesz jako błąd
- Ale BRAK placeholdera = CRITICAL
- Nie tolerujesz "czegoś nie widać" — jeśli elementy są na obrazie, musisz je odczytać
- Każda "N/D" w danych automatycznych = weryfikuj WZROKOWO na obrazie
- Pozycja ikon zmieniona o >5% strony = important
- Brakująca ikona normatywna = critical

Odpowiedz WYŁĄCZNIE w JSON (bez markdown):
{
  "overall_risk": "ok|info|warning|error|critical",
  "risk_score": 0-100,
  "summary_pl": "2-3 zdania po polsku co się zmieniło i czy jest OK",
  "critical_findings": [
    {"field": "...", "val_a": "...", "val_b": "...", "description": "...", "action": "..."}
  ],
  "important_findings": [
    {"field": "...", "val_a": "...", "val_b": "...", "description": "..."}
  ],
  "info_findings": [
    {"field": "...", "description": "..."}
  ],
  "visual_changes": {
    "layout_changed": false,
    "colors_changed": false,
    "logo_changed": false,
    "format_changed": false,
    "notes": ""
  },
  "regulatory_checklist": {
    "ref_present": true, "ean_present": true, "ce_mark": true,
    "single_use_symbol": true, "sterility_mark": true,
    "lot_placeholder": true, "exp_placeholder": true,
    "manufacturer_info": true, "warnings_present": true
  },
  "approval_recommendation": "APPROVED|APPROVED_WITH_NOTES|NEEDS_REVISION|REJECTED",
  "approval_reason": "Uzasadnienie decyzji po polsku"
}"""


# ─── AUTOMATYCZNE STREFY ARTWORKU (bez szablonów) ─────────────────────────────

# Podział artworku na standardowe strefy procentowe.
# Każda strefa = (nazwa, y1%, y2%, severity, opis)
# Strefa "header" to zazwyczaj logo + EAN; "body" to tabela rozmiarów + ikony;
# "translations" to bloki językowe; "footer" to dane producenta.
_AUTO_ZONES = [
    ("Nagłówek / Logo + EAN",       0,   18,  "critical",  "Logo, EAN, REF, nazwa produktu"),
    ("Tabela rozmiarów / Gauge",    18,   42,  "critical",  "Rozmiary XS-XL, parametry techniczne"),
    ("Ikony / Infografiki",         42,   62,  "important", "Piktogramy, CE, symbole użytkowania"),
    ("Tłumaczenia — blok górny",    62,   78,  "important", "Języki EU: EN/DE/FR/ES/PT/IT"),
    ("Tłumaczenia — blok dolny",    78,   90,  "important", "Języki EU: PL/CZ/SK/HU/RO/BG/RU/UA"),
    ("Stopka / Dane producenta",    90,  100,  "info",      "Producent, importer, www"),
]


def _ocr_enhanced(img) -> str:
    """OCR z preprocessing: grayscale → kontrast → progowanie — lepsze dla małego tekstu."""
    try:
        from PIL import ImageEnhance, ImageFilter
        import pytesseract
        img = _orient_for_ocr(img)
        g = img.convert("L")
        g = ImageEnhance.Contrast(g).enhance(2.5)
        g = g.filter(ImageFilter.SHARPEN)
        # Scale up jeśli mały — Tesseract radzi sobie lepiej z > 30px wysokości liter
        w, h = g.size
        if h < 120:
            scale = max(2, min(120 // max(h, 1), 8))
            # Jeden współczynnik z limitami szer.=4000 / wys.=600 — zachowuje
            # proporcje (niezależne limity zniekształcały szerokie paski OCR).
            scale = min(scale, 4000 / w, 600 / h)
            if scale > 1:
                g = g.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        # Adaptive Sauvola binarization — outperforms fixed threshold for uneven
        # illumination and low-contrast packaging text.  Falls back to fixed threshold.
        try:
            from skimage.filters import threshold_sauvola
            arr = np.array(g)
            win = max(25, min(arr.shape[0], arr.shape[1]) // 4)
            if win % 2 == 0:
                win += 1
            thresh_map = threshold_sauvola(arr, window_size=win, k=0.2)
            g = Image.fromarray(((arr > thresh_map) * 255).astype(np.uint8))
        except Exception:
            g = g.point(lambda x: 255 if x > 140 else 0)
        # Call Tesseract directly on the binarized image — skipping _extract_text_ocr
        # avoids the RGB-conversion and contrast steps in _preprocess_for_ocr which
        # would undo the Sauvola binarization we just computed.
        import pytesseract
        _t0 = time.time()
        _res = pytesseract.image_to_string(g, lang=_get_ocr_langs(), config="--psm 3",
                                           timeout=_TESSERACT_TIMEOUT) or ""
        _elapsed = time.time() - _t0
        if _elapsed > _TESSERACT_TIMEOUT:
            logger.warning("Tesseract OCR took %.1fs (timeout threshold: %ds)", _elapsed, _TESSERACT_TIMEOUT)
        return _res
    except Exception:
        return _extract_text_ocr(img)


def _zone_ocr_compare(img_a, img_b, path_a: str = "", path_b: str = "") -> tuple:
    """
    Automatyczna analiza strefowa — bez szablonów, działa dla wszystkich produktów.
    Dzieli artworki na pionowe strefy, OCR każdą, porównuje tekst i pixelowo.

    Zwraca (rows, zone_regions):
      rows        — lista wpisów field_report z prefixem [Strefa]
      zone_regions — lista dict {y1_pct, y2_pct, severity, name} dla zmienionych stref
                     → używane do nałożenia kolorowych ramek na artwork
    """
    if not HAS_PIL or img_a is None or img_b is None:
        return [], []

    # Jeśli dostępne ścieżki PDF — renderuj strefę tabeli rozmiarów w 300 DPI dla lepszego OCR
    hires_a = hires_b = None
    if path_a and path_b:
        try:
            hires_a = _load_pages(path_a, _PROFILE_RENDER_DPI, page_idx=0)
            hires_a = hires_a[0] if hires_a else None
            hires_b = _load_pages(path_b, _PROFILE_RENDER_DPI, page_idx=0)
            hires_b = hires_b[0] if hires_b else None
        except Exception:
            hires_a = hires_b = None

    wa, ha = img_a.size
    wb, hb = img_b.size

    rows = []
    zone_regions = []

    for zone_name, y1_pct, y2_pct, severity, desc in _AUTO_ZONES:
        # Crop strefa z obu stron (220 DPI dla pixel diff)
        y1a = int(y1_pct / 100 * ha); y2a = int(y2_pct / 100 * ha)
        y1b = int(y1_pct / 100 * hb); y2b = int(y2_pct / 100 * hb)

        crop_a = img_a.crop((0, y1a, wa, y2a)) if y2a > y1a else None
        crop_b = img_b.crop((0, y1b, wb, y2b)) if y2b > y1b else None

        if not crop_a or not crop_b:
            continue

        # Pixel similarity dla strefy
        try:
            pix_sim = _icon_similarity(crop_a, crop_b)
        except Exception:
            pix_sim = 0.0

        # OCR — dla tabeli rozmiarów używaj 300 DPI jeśli dostępne
        is_size_zone = "rozmiar" in zone_name.lower() or "gauge" in zone_name.lower()
        if is_size_zone and hires_a and hires_b:
            wha, hha = hires_a.size; whb, hhb = hires_b.size
            hcrop_a = hires_a.crop((0, int(y1_pct/100*hha), wha, int(y2_pct/100*hha)))
            hcrop_b = hires_b.crop((0, int(y1_pct/100*hhb), whb, int(y2_pct/100*hhb)))
            va = " ".join(_ocr_enhanced(hcrop_a).split()) or "N/D"
            vb = " ".join(_ocr_enhanced(hcrop_b).split()) or "N/D"
        else:
            va = " ".join(_extract_text_ocr(crop_a).split()) or "N/D"
            vb = " ".join(_extract_text_ocr(crop_b).split()) or "N/D"

        both_nd = va == "N/D" and vb == "N/D"
        text_changed = (va != vb) and not both_nd
        pixel_changed = pix_sim < 88  # próg: wizualne różnice w strefie

        if both_nd and not pixel_changed:
            continue  # brak danych i brak różnic wizualnych — pomijamy

        changed = text_changed or pixel_changed

        note_parts = [desc]
        note_parts.append(f"Podobieństwo wizualne: {pix_sim:.0f}%")
        if pixel_changed and not text_changed:
            note_parts.append("⚠ Różnica wizualna — zmiana grafiki/kolorów/układu")
        if text_changed and not pixel_changed:
            note_parts.append("⚠ Zmiana tekstowa przy podobnym wyglądzie")
        if text_changed and pixel_changed:
            note_parts.append("⚠ Różnica tekstowa i wizualna")

        rows.append({
            "field":    f"[Strefa] {zone_name}",
            "val_a":    va[:500] if va != "N/D" else "N/D",
            "val_b":    vb[:500] if vb != "N/D" else "N/D",
            "severity": severity,
            "changed":  changed,
            "note":     " | ".join(note_parts),
        })

        if changed:
            zone_regions.append({
                "y1_pct": y1_pct, "y2_pct": y2_pct,
                "severity": severity, "name": zone_name,
                "pix_sim": pix_sim,
            })

    return rows, zone_regions


# ─── SYSTEM SZABLONÓW PÓL ARTWORKU ────────────────────────────────────────────

_PROFILE_RENDER_DPI = 360  # wysoka rozdzielczość dla cropów szablonowych
# 360 DPI ≈ 15.5 MP na A4 (37 MB RGB/strona) — kompromis między jakością
# a pamięcią. 400 DPI dawało ~46 MB/stronę i pod obciążeniem groziło OOM
# przy wielostronicowych artworkach × concurrent workers.


def _get_dominant_colors(img, n: int = 5) -> list:
    """Return top-N dominant colors as list of {hex, r, g, b, pct}."""
    if not HAS_PIL:
        return []
    try:
        from collections import Counter
        small = img.resize((120, 120), Image.LANCZOS).convert("RGB")
        quantized = small.quantize(colors=n, method=2).convert("RGB")
        pixels = list(quantized.getdata())
        total = len(pixels)
        counter = Counter(pixels)
        colors = []
        for (r, g, b), cnt in counter.most_common(n):
            colors.append({
                "hex": f"#{r:02x}{g:02x}{b:02x}",
                "r": r, "g": g, "b": b,
                "pct": round(cnt * 100 / total, 1),
            })
        return colors
    except Exception:
        return []


def _get_colorimetry(img) -> dict:
    """Return colorimetry dict: avg_rgb, brightness, dominant colors."""
    if not HAS_PIL:
        return {}
    try:
        rgb = img.convert("RGB")
        arr = np.array(rgb)
        avg = arr.mean(axis=(0, 1))
        brightness = float(avg.mean())
        return {
            "avg_rgb": [int(avg[0]), int(avg[1]), int(avg[2])],
            "avg_hex": "#{:02x}{:02x}{:02x}".format(int(avg[0]), int(avg[1]), int(avg[2])),
            "brightness": round(brightness, 1),
            "dominant": _get_dominant_colors(img, n=6),
        }
    except Exception:
        return {}


def _delta_e_rgb(rgb1, rgb2) -> float:
    """Perceptualna różnica barwy (CIEDE2000) między dwoma kolorami RGB (0-255)."""
    try:
        from skimage.color import deltaE_ciede2000, rgb2lab as _rgb2lab
        a = _rgb2lab(np.array([[[c / 255.0 for c in rgb1]]], dtype=float))
        b = _rgb2lab(np.array([[[c / 255.0 for c in rgb2]]], dtype=float))
        return float(deltaE_ciede2000(a, b)[0][0])
    except Exception:
        # Awaryjnie (brak skimage): euklides w sRGB — przybliżenie, ale lepsze niż nic.
        import math
        return math.sqrt(sum((float(x) - float(y)) ** 2 for x, y in zip(rgb1, rgb2)))


def _global_color_compare(col_a: dict, col_b: dict, de_thresh: float = None) -> dict:
    """Globalne porównanie kolorów CAŁEJ strony: paleta dominująca master vs dostawca
    + średnie/maks ΔE (CIEDE2000). Werdykt RÓŻNICA, gdy któryś ISTOTNY kolor (≥3%
    powierzchni) przesunął się o więcej niż próg (domyślnie ΔE>5; ARTWORK_COLOR_DE).
    Zwraca dict do raportu albo {} gdy brak danych."""
    if not col_a or not col_b:
        return {}
    if de_thresh is None:
        try:
            de_thresh = float(os.environ.get("ARTWORK_COLOR_DE", "5"))
        except ValueError:
            de_thresh = 5.0
    pal_a = col_a.get("dominant") or []
    pal_b = col_b.get("dominant") or []
    avg_a, avg_b = col_a.get("avg_rgb"), col_b.get("avg_rgb")
    avg_de = _delta_e_rgb(avg_a, avg_b) if (avg_a and avg_b) else 0.0
    pairs, max_de = [], 0.0
    for ca in pal_a:
        if ca.get("pct", 0) < 3:        # pomiń marginalne kolory (szum, antyaliasing)
            continue
        rgb_a = (ca["r"], ca["g"], ca["b"])
        best, best_de = None, 999.0
        for cb in pal_b:
            de = _delta_e_rgb(rgb_a, (cb["r"], cb["g"], cb["b"]))
            if de < best_de:
                best_de, best = de, cb
        pairs.append({
            "a_hex": ca["hex"], "a_pct": ca["pct"],
            "b_hex": best["hex"] if best else "",
            "delta_e": round(best_de, 1), "changed": best_de > de_thresh,
        })
        max_de = max(max_de, best_de)
    changed = (max_de > de_thresh) or (avg_de > de_thresh)
    n_changed = sum(1 for p in pairs if p["changed"])
    if changed:
        note = (f"Różnica koloru: {n_changed} dominując(ych) kolorów przesuniętych "
                f"(maks ΔE={max_de:.1f}, tło ΔE={avg_de:.1f}; próg {de_thresh:.0f}).")
    else:
        note = (f"Kolory zgodne (maks ΔE={max_de:.1f}, tło ΔE={avg_de:.1f}; "
                f"próg {de_thresh:.0f}).")
    return {
        "palette_a": pal_a, "palette_b": pal_b,
        "avg_hex_a": col_a.get("avg_hex"), "avg_hex_b": col_b.get("avg_hex"),
        "avg_delta_e": round(avg_de, 1), "max_delta_e": round(max_de, 1),
        "threshold": de_thresh, "pairs": pairs,
        "changed": changed, "note": note,
    }


def _load_artwork_profile_by_id(profile_id: int) -> Optional[dict]:
    """Load artwork profile directly by ID."""
    try:
        import json as _json
        from db import get_db as _gdb
        db = _gdb()
        try:
            row = db.execute(
                "SELECT * FROM artwork_profiles WHERE id=? AND is_active=1",
                (profile_id,)
            ).fetchone()
            if not row:
                return None
            fields = db.execute(
                "SELECT * FROM artwork_profile_fields WHERE profile_id=? ORDER BY sort_order",
                (row["id"],)
            ).fetchall()
            dict_rows = db.execute(
                "SELECT display_name, display_layout, default_rotation, comparison_mode, numeric_tolerance FROM artwork_field_dict"
            ).fetchall()
        finally:
            db.close()
        dict_map = {r["display_name"]: r for r in dict_rows}
        field_list = []
        for f in fields:
            fd = dict(f)
            dname = fd.get("display_name", "")
            if dname in dict_map:
                d = dict_map[dname]
                if not fd.get("display_layout") or fd["display_layout"] == "side_by_side":
                    fd["display_layout"] = d["display_layout"] or "side_by_side"
                if not fd.get("rotation"):
                    fd["rotation"] = int(d["default_rotation"] or 0)
                if not fd.get("comparison_mode") or fd["comparison_mode"] == "text":
                    fd["comparison_mode"] = d.get("comparison_mode") or "text"
                if not fd.get("numeric_tolerance"):
                    fd["numeric_tolerance"] = float(d.get("numeric_tolerance") or 0.0)
            field_list.append(fd)
        row_d = dict(row)
        return {
            "id":              row_d["id"],
            "name":            row_d["name"],
            "master_pdf_path": row_d.get("master_pdf_path", ""),
            "fields":          field_list,
        }
    except Exception as _e:
        logger.debug("Profile load by ID failed: %s", _e)
        return None


def _load_artwork_profile(ean: str = "", ref: str = "", dims_a=None) -> Optional[dict]:
    """
    Szuka profilu szablonowego dla danego artworku.
    Dopasowuje po EAN (dokładnie), potem po REF (lista ref_list_json lub ref_code).
    """
    try:
        import json as _json
        from db import get_db as _gdb
        db = _gdb()
        try:
            row = None

            # 1. Dopasowanie po EAN
            if ean and ean != "N/D":
                row = db.execute(
                    "SELECT * FROM artwork_profiles WHERE ean=? AND is_active=1",
                    (ean,)
                ).fetchone()

            # 2. Dopasowanie po REF — sprawdź ref_list_json (JSON array) i ref_code
            if not row and ref and ref != "N/D":
                ref_clean = ref.strip().upper()
                all_rows = db.execute(
                    "SELECT * FROM artwork_profiles WHERE is_active=1"
                ).fetchall()
                for candidate in all_rows:
                    # Sprawdź stare pole ref_code
                    if candidate["ref_code"] and candidate["ref_code"].strip().upper() == ref_clean:
                        row = candidate
                        break
                    # Sprawdź listę ref_list_json
                    try:
                        ref_list = _json.loads(candidate["ref_list_json"] or "[]")
                        if any(r.strip().upper() == ref_clean for r in ref_list):
                            row = candidate
                            break
                    except Exception:
                        pass

            if not row:
                return None

            fields = db.execute(
                "SELECT * FROM artwork_profile_fields WHERE profile_id=? ORDER BY sort_order",
                (row["id"],)
            ).fetchall()
            dict_rows = db.execute(
                "SELECT display_name, display_layout, default_rotation, comparison_mode, numeric_tolerance FROM artwork_field_dict"
            ).fetchall()
        finally:
            db.close()
        dict_map = {r["display_name"]: r for r in dict_rows}
        field_list = []
        for f in fields:
            fd = dict(f)
            dname = fd.get("display_name", "")
            if dname in dict_map:
                d = dict_map[dname]
                if not fd.get("display_layout") or fd["display_layout"] == "side_by_side":
                    fd["display_layout"] = d["display_layout"] or "side_by_side"
                if not fd.get("rotation"):
                    fd["rotation"] = int(d["default_rotation"] or 0)
                if not fd.get("comparison_mode") or fd["comparison_mode"] == "text":
                    fd["comparison_mode"] = d.get("comparison_mode") or "text"
                if not fd.get("numeric_tolerance"):
                    fd["numeric_tolerance"] = float(d.get("numeric_tolerance") or 0.0)
            field_list.append(fd)
        row_d = dict(row)
        return {
            "id":              row_d["id"],
            "name":            row_d["name"],
            "master_pdf_path": row_d.get("master_pdf_path", ""),
            "fields":          field_list,
        }
    except Exception as _e:
        logger.debug("Profile lookup failed: %s", _e)
        return None


def _crop_field_region(img, x1_pct: float, y1_pct: float,
                        x2_pct: float, y2_pct: float):
    """Wycina region z obrazu wg % współrzędnych. Skaluje do min 200px szer."""
    w, h = img.size
    x1 = max(0, int(x1_pct / 100 * w))
    y1 = max(0, int(y1_pct / 100 * h))
    x2 = min(w, int(x2_pct / 100 * w))
    y2 = min(h, int(y2_pct / 100 * h))
    if x2 <= x1 or y2 <= y1:
        return None
    crop = img.crop((x1, y1, x2, y2))
    cw, ch = crop.size
    if cw < 200:
        # Cel: ~200 px szerokości (czytelność OCR) — współczynnik upscalu.
        width_scale = min(max(2, 200 // max(cw, 1)), 8)
        # Górny limit wysokości wyniku ~4000 px (pamięć/wydajność OCR).
        height_limit = 4000 / ch
        # Clamp: nigdy nie SCHODZIMY poniżej 1.0 (brak downscalu wąskiego kadru) —
        # dla bardzo wysokich kadrów (ch>4000) to daje 1.0 (skip), ale dla kadrów z
        # zapasem wysokości pozwala faktycznie upscalować do limitu szerokości lub
        # wysokości, zamiast — jak wcześniej — wymuszać scale<1 i pomijać resize.
        scale = min(width_scale, max(1.0, height_limit))
        if scale > 1:
            crop = crop.resize((max(1, int(cw * scale)), max(1, int(ch * scale))), Image.LANCZOS)
    return crop


def _feature_match_locate(tmpl_gray, page_gray, ph: int, pw: int,
                           th: int, tw: int) -> tuple:
    """
    LightGlue → SIFT → AKAZE feature-based matching.
    Returns (coords_dict, confidence) or None.

    Tier order:
    1. LightGlue + SuperPoint (learned matcher — highest accuracy, handles large scale changes)
    2. SIFT (float descriptors, L2 — best for text/logos)
    3. AKAZE (binary descriptors — faster fallback)
    All use RANSAC homography to filter outliers.
    """
    import cv2
    import numpy as np

    MIN_INLIERS = 8

    # ── Tier 1: LightGlue + SuperPoint ────────────────────────────────────
    lg_result = _lightglue_match_locate(tmpl_gray, page_gray, ph, pw, th, tw)
    if lg_result is not None:
        return lg_result

    # ── Tier 2+3: SIFT → AKAZE ────────────────────────────────────────────
    MIN_KP = 6
    for det_name, norm, ratio_thr in (
        ("SIFT",  cv2.NORM_L2,      0.75),
        ("AKAZE", cv2.NORM_HAMMING, 0.80),
    ):
        try:
            det = cv2.SIFT_create() if det_name == "SIFT" else cv2.AKAZE_create()
            kp1, des1 = det.detectAndCompute(tmpl_gray, None)
            if des1 is None or len(kp1) < MIN_KP:
                continue
            kp2, des2 = det.detectAndCompute(page_gray, None)
            if des2 is None or len(kp2) < MIN_KP:
                continue

            bf = cv2.BFMatcher(norm)
            raw = bf.knnMatch(des1, des2, k=2)
            good = [p[0] for p in raw if len(p) >= 2 and p[0].distance < ratio_thr * p[1].distance]
            if len(good) < MIN_INLIERS:
                continue

            src_pts = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
            dst_pts = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
            M, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)
            if M is None:
                continue
            inliers = int(mask.ravel().sum())
            if inliers < MIN_INLIERS:
                continue

            corners = np.float32([[0, 0], [tw, 0], [tw, th], [0, th]]).reshape(-1, 1, 2)
            dst = cv2.perspectiveTransform(corners, M)
            xs, ys = dst[:, 0, 0], dst[:, 0, 1]
            x1 = int(max(0,  xs.min()))
            y1 = int(max(0,  ys.min()))
            x2 = int(min(pw, xs.max()))
            y2 = int(min(ph, ys.max()))
            if x2 <= x1 or y2 <= y1:
                continue

            area_ratio = ((x2 - x1) * (y2 - y1)) / max(tw * th, 1)
            if not (0.25 < area_ratio < 4.0):
                continue

            confidence = min(0.95, 0.5 + inliers * 0.04)
            logger.debug("_auto_locate %s: %d inliers conf=%.2f", det_name, inliers, confidence)
            return ({"x1_pct": x1/pw*100, "y1_pct": y1/ph*100,
                     "x2_pct": x2/pw*100, "y2_pct": y2/ph*100},
                    confidence)

        except Exception as exc:
            logger.debug("_auto_locate %s error: %s", det_name, exc)

    return None


def _auto_locate_field(master_crop, supplier_page,
                        orig_x1_pct: float, orig_y1_pct: float,
                        orig_x2_pct: float, orig_y2_pct: float,
                        confidence_threshold: float = 0.60) -> tuple:
    """
    Three-tier field localisation:

    1. Multi-scale template matching with progressive window expansion
       (±25% → ±45% → ±75% → full page).  Fast, good for small scale changes.
    2. Feature-based matching: SIFT → AKAZE + RANSAC homography.
       Scale- and rotation-invariant; handles larger layout differences.
    3. Fallback: original profile coordinates (confidence = 0).

    Returns (coords_pct_dict, confidence).
    """
    try:
        import cv2
        import numpy as np

        tmpl_full = np.array(master_crop.convert("L"), dtype=np.uint8)
        page_full = np.array(supplier_page.convert("L"), dtype=np.uint8)
        ph_full, pw_full = page_full.shape

        # Downscale to max 1200px — SIFT/AKAZE/template-matching don't need full 300 DPI.
        # Coordinates stay %-based so no remapping needed.
        _MAX_DIM = 1200
        _ds = min(1.0, _MAX_DIM / max(ph_full, pw_full, 1))
        if _ds < 1.0:
            page = cv2.resize(page_full, (int(pw_full*_ds), int(ph_full*_ds)),
                              interpolation=cv2.INTER_AREA)
            tmpl = cv2.resize(tmpl_full,
                              (max(4, int(tmpl_full.shape[1]*_ds)),
                               max(4, int(tmpl_full.shape[0]*_ds))),
                              interpolation=cv2.INTER_AREA)
        else:
            page, tmpl = page_full, tmpl_full

        ph, pw = page.shape
        th, tw = tmpl.shape

        cx = int((orig_x1_pct + orig_x2_pct) / 2 / 100 * pw)
        cy = int((orig_y1_pct + orig_y2_pct) / 2 / 100 * ph)

        # ── Tier 1: progressive template matching ─────────────────────────
        best_val = -1.0
        best_x = best_y = 0
        best_tw = tw
        best_th = th

        for margin_pct in (25, 45, 75, None):
            if margin_pct is None:
                sx1, sy1, sx2, sy2 = 0, 0, pw, ph
            else:
                mx = max(int(pw * margin_pct / 100), tw)
                my = max(int(ph * margin_pct / 100), th)
                sx1 = max(0, cx - mx);  sy1 = max(0, cy - my)
                sx2 = min(pw, cx + mx); sy2 = min(ph, cy + my)

            roi = page[sy1:sy2, sx1:sx2]
            for scale in (0.75, 0.85, 0.95, 1.00, 1.05, 1.15, 1.25):
                rtw = max(8, int(tw * scale))
                rth = max(8, int(th * scale))
                if rtw > roi.shape[1] or rth > roi.shape[0]:
                    continue
                t = cv2.resize(tmpl, (rtw, rth), interpolation=cv2.INTER_AREA)
                res = cv2.matchTemplate(roi, t, cv2.TM_CCOEFF_NORMED)
                _, mv, _, ml = cv2.minMaxLoc(res)
                if mv > best_val:
                    best_val = mv
                    best_x = sx1 + ml[0];  best_y = sy1 + ml[1]
                    best_tw = rtw;          best_th = rth

            if best_val >= confidence_threshold:
                break

        if best_val >= confidence_threshold:
            return ({"x1_pct": best_x/pw*100, "y1_pct": best_y/ph*100,
                     "x2_pct": (best_x+best_tw)/pw*100, "y2_pct": (best_y+best_th)/ph*100},
                    float(best_val))

        # ── Tier 2: SIFT / AKAZE feature matching on full page ────────────
        feat = _feature_match_locate(tmpl, page, ph, pw, th, tw)
        if feat is not None:
            return feat

    except Exception as _e:
        logger.debug("_auto_locate_field error: %s", _e)

    # ── Tier 3: original coordinates ──────────────────────────────────────
    return {"x1_pct": orig_x1_pct, "y1_pct": orig_y1_pct,
            "x2_pct": orig_x2_pct, "y2_pct": orig_y2_pct}, 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Comparison-mode helpers
# ─────────────────────────────────────────────────────────────────────────────

def _extract_numbers_from_text(text: str) -> list:
    """Return all numeric values extracted from OCR text, normalized to float.

    Handles European (1.234,56) and Anglo (1,234.56) formats via normalizer
    when available; falls back to a simple regex + comma-as-decimal heuristic.
    """
    if not text or text.strip().upper() in ("N/D", "ND", "—", "-"):
        return []
    try:
        from normalizer import normalize_number as _norm
    except ImportError:
        _norm = None
    # Optional leading minus (kept only when it directly precedes the digits and
    # is not part of a word/code like "REF-12") so signed table values such as a
    # permeation delta "-25,6" keep their sign — a sign flip is a real difference.
    pattern = re.compile(r'(?<![\w])-?\d[\d\s.,]*\d|(?<![\w])-?\d')
    result = []
    for m in pattern.finditer(text):
        raw = m.group().strip()
        if _norm:
            try:
                v = _norm(raw)
                if v is not None:
                    result.append(float(v))
                    continue
            except Exception:
                pass
        try:
            result.append(float(raw.replace(' ', '').replace(',', '.')))
        except ValueError:
            pass
    return result


def _numeric_values_differ(nums_a: list, nums_b: list, tolerance: float = 0.0) -> bool:
    """True when the two number lists differ beyond tolerance.

    Compares positionally (NOT sorted) to mirror ``_numeric_diffs_list`` — these
    are ordered table cells, so a swap (17,0/24,0 → 24,0/17,0) is a real change
    and must not be masked by sorting both sides first.
    """
    if len(nums_a) != len(nums_b):
        return True
    for a, b in zip(nums_a, nums_b):
        if abs(a - b) > tolerance:
            return True
    return False


def _numeric_diffs_list(nums_a: list, nums_b: list, tolerance: float = 0.0) -> list:
    """Enumerate every numeric difference positionally (table cells read in order).

    Returns a list of (a, b) tuples for each position whose values differ beyond
    tolerance, plus (a, None)/(None, b) for missing/extra values when the two
    lists have different lengths. Unlike ``_numeric_values_differ`` this does NOT
    sort — so a per-cell change (e.g. one table row that moved 17,0 → 24,0) is
    surfaced individually instead of being collapsed into a single boolean.
    """
    diffs = []
    for i in range(max(len(nums_a), len(nums_b))):
        a = nums_a[i] if i < len(nums_a) else None
        b = nums_b[i] if i < len(nums_b) else None
        if a is None or b is None or abs(a - b) > tolerance:
            diffs.append((a, b))
    return diffs


def _compare_lines_fuzzy(va: str, vb: str, threshold: float = 90.0) -> tuple:
    """Line-by-line fuzzy comparison for translation blocks.

    Returns (changed: bool, changed_lines: int, total_lines: int).
    Uses rapidfuzz.fuzz.ratio; falls back to exact equality when unavailable.
    """
    lines_a = [l.strip() for l in va.splitlines() if l.strip()]
    lines_b = [l.strip() for l in vb.splitlines() if l.strip()]
    if not lines_a and not lines_b:
        return False, 0, 0
    total = max(len(lines_a), len(lines_b))
    changed = abs(len(lines_a) - len(lines_b))
    try:
        from rapidfuzz import fuzz as _fuzz
        for la, lb in zip(lines_a, lines_b):
            if _fuzz.ratio(la, lb) < threshold:
                changed += 1
    except ImportError:
        for la, lb in zip(lines_a, lines_b):
            if la != lb:
                changed += 1
    return changed > 0, changed, total


def _process_profile_field(fld: dict, img_a, img_b,
                            field_overrides_b, profile_name: str,
                            field_stats: dict = None):
    """Process one profile field: crop → auto-locate → OCR → compare.
    Thread-safe: img_a/img_b are read-only PIL images shared across workers.
    field_stats: optional {field_name: {fpr, reviews}} from operator feedback.
    """
    if fld.get("skip_analysis"):
        return None
    if not all(k in fld for k in ("x1_pct", "y1_pct", "x2_pct", "y2_pct")):
        return None

    x1, y1 = fld["x1_pct"], fld["y1_pct"]
    x2, y2 = fld["x2_pct"], fld["y2_pct"]
    fname = fld.get("field_name", "")

    # Handle user overrides from preview validation step
    # Default rotation: from field-dict global default (merged into fld at load time)
    try:
        field_default_rotation = int(fld.get("rotation", 0) or 0) % 360
    except (TypeError, ValueError):
        field_default_rotation = 0
    user_rotation = field_default_rotation
    if field_overrides_b is not None and fname in field_overrides_b:
        override = field_overrides_b[fname]
        if override is None:
            # User explicitly skipped this field
            size_lbl = fld.get("size_label", "").strip()
            _dname = fld.get("display_name") or fld.get("field_name", "")
            field_label = (f"[Rozmiar {size_lbl}] {_dname}"
                           if size_lbl else f"[Szablon] {_dname}")
            return {
                "field":    field_label,
                "val_a":    "—",
                "val_b":    "⊘ pominięto",
                "severity": fld.get("severity", "critical"),
                "changed":  False,
                "note":     "Pole pominięte przez użytkownika podczas weryfikacji",
                "skipped":  True,
                "crop_a_b64": None,
                "crop_b_b64": None,
            }
        elif isinstance(override, dict):
            bx1 = override.get("x1_pct", x1)
            by1 = override.get("y1_pct", y1)
            bx2 = override.get("x2_pct", x2)
            by2 = override.get("y2_pct", y2)
            try:
                # Per-comparison override takes priority over field-dict default
                user_rotation = int(override.get("rotation", field_default_rotation) or field_default_rotation) % 360
            except (TypeError, ValueError):
                user_rotation = field_default_rotation
        else:
            bx1, by1, bx2, by2 = x1, y1, x2, y2
    else:
        bx1, by1, bx2, by2 = x1, y1, x2, y2

    crop_a = _crop_field_region(img_a, x1, y1, x2, y2)

    # Auto-locate field on B when no manual override was given
    if field_overrides_b is None or fname not in field_overrides_b:
        if crop_a:
            located, conf = _auto_locate_field(crop_a, img_b, bx1, by1, bx2, by2)
            if conf > 0:
                bx1, by1, bx2, by2 = (located["x1_pct"], located["y1_pct"],
                                       located["x2_pct"], located["y2_pct"])

    crop_b = _crop_field_region(img_b, bx1, by1, bx2, by2)

    _mode_early = fld.get("comparison_mode", "text")
    if _mode_early == "graphic":
        # Ikony/piktogramy nie mają sensownego tekstu — pomijamy CAŁĄ kaskadę OCR
        # (która na bezteksowych wycinkach przepada przez wolne lokalne modele i
        # potrafi zająć minuty). Tryb grafika decyduje wizualnie (pix_sim).
        crop_a_oriented, crop_b_oriented = crop_a, crop_b
        va = vb = ""
    elif user_rotation:
        # User-defined rotation applied identically to both sides — trusted,
        # so skip OCR-based orientation detection
        if crop_a:
            crop_a = crop_a.rotate(-user_rotation, expand=True)
        if crop_b:
            crop_b = crop_b.rotate(-user_rotation, expand=True)
        crop_a_oriented = crop_a
        crop_b_oriented = crop_b
        # OCR of A and B are independent HTTP/model calls — run them concurrently
        # so a field costs ~one OCR latency instead of two (the dominant per-field
        # cost in the field-queue path, where one request handles one field).
        def _ocr_fixed(crop):
            try:
                t = _extract_text_ocr(crop, auto_orient=False).strip() if crop else ""
                return " ".join(t.split())
            except Exception:
                return ""
        with ThreadPoolExecutor(max_workers=2) as _ex:
            _fa = _ex.submit(_ocr_fixed, crop_a)
            _fb = _ex.submit(_ocr_fixed, crop_b)
            va, vb = _fa.result(), _fb.result()
    else:
        # Per-crop orientation with garbage-fallback (handles small crops where
        # OSD has too little text and packaging panels facing opposite directions).
        # A and B are independent — OCR them concurrently to halve per-field latency.
        with ThreadPoolExecutor(max_workers=2) as _ex:
            _fa = _ex.submit(_ocr_with_orient_fallback, crop_a)
            _fb = _ex.submit(_ocr_with_orient_fallback, crop_b)
            crop_a_oriented, va = _fa.result()
            crop_b_oriented, vb = _fb.result()
        # Consistency guard: _ocr_with_orient_fallback runs independently on
        # each crop. For fields with little text (instruction sequences, icon
        # rows, language-code panels) the stop-word scorer is non-deterministic
        # and sometimes rotates one side 180° while leaving the other at 0°.
        # Detect orientation mismatch: OCR runs independently on each crop and can
        # pick a different rotation for A vs B (especially on icon/pictogram fields
        # with little text). Check all 4 rotations of B against A and snap B to
        # whichever orientation is most similar to A (15% threshold to avoid false
        # corrections on genuinely different content).
        if crop_a_oriented and crop_b_oriented:
            try:
                import numpy as _np
                _ref64 = crop_a_oriented.convert("L").resize((64, 64), Image.LANCZOS)
                _ref_arr = _np.array(_ref64, dtype=float)
                _b_curr = crop_b_oriented.convert("L").resize((64, 64), Image.LANCZOS)
                _curr_diff = float(_np.abs(_np.array(_b_curr, dtype=float) - _ref_arr).mean())
                _best_diff = _curr_diff
                _best_angle = 0
                for _ang in (90, 180, 270):
                    _b_rot = crop_b_oriented.rotate(_ang, expand=True)
                    _b_rot64 = _b_rot.convert("L").resize((64, 64), Image.LANCZOS)
                    _d = float(_np.abs(_np.array(_b_rot64, dtype=float) - _ref_arr).mean())
                    if _d < _best_diff * 0.85:
                        _best_diff = _d
                        _best_angle = _ang
                if _best_angle:
                    crop_b_oriented = crop_b_oriented.rotate(_best_angle, expand=True)
                    vb_corr = _extract_text_ocr(crop_b_oriented, auto_orient=False).strip()
                    vb_corr = " ".join(vb_corr.split())
                    if vb_corr:
                        vb = vb_corr
            except Exception:
                pass
    va = va or "N/D"
    vb = vb or "N/D"

    comparison_mode = fld.get("comparison_mode", "text")
    both_nd = va == "N/D" and vb == "N/D"
    changed = va != vb and not both_nd
    _num_diff_vals = set()   # konkretne różniące się liczby (do precyzyjnego boksowania)
    # Per-strona: wartości zmienione PO STRONIE A i PO STRONIE B osobno, żeby master
    # boksował swoją zmienioną liczbę (np. unikalne '2' w kolumnie Level), a dostawca
    # swoją — zamiast wspólnej unii zaznaczanej na obu wycinkach.
    _num_diff_vals_a = set()
    _num_diff_vals_b = set()
    # Strukturalne porównanie tabeli (img2table) — dokładne boxy różniących się komórek.
    _struct_boxes_a = None
    _struct_boxes_b = None
    # img2table służy WYŁĄCZNIE do precyzyjnych ramek komórek (addytywnie). NIE może
    # decydować o werdykcie ani pomijać weryfikacji — gdy źle posegmentuje tabelę,
    # przegapiłby realną różnicę (regres). Werdykt: numeryka + weryfikacja Sonnetem.
    _struct = None

    # ── Weryfikacja dokładnym modelem OCR dla pól KRYTYCZNYCH ───────────────
    # Domyślnie OCR jest szybki (Haiku). Gdy na polu krytycznym (tekst/tabela/
    # tłumaczenie) szybki model zasugerował RÓŻNICĘ, czytamy oba wycinki ponownie
    # dokładnym modelem (Sonnet) i na nim opieramy werdykt — żeby błąd OCR
    # szybkiego modelu (np. 'klasi' vs 'klasy') nie dał fałszywej różnicy.
    # Wyłączane przez ARTWORK_OCR_VERIFY=0; model: ARTWORK_OCR_VERIFY_MODEL.
    # Pomijane dla BARDZO DŁUGICH pól (ARTWORK_VERIFY_MAX_CHARS, domyślnie 1800):
    # ponowny odczyt Sonnetem gigantycznego, wielojęzycznego bloku 'Opis' to główne
    # źródło timeoutów (pole > 180-240 s), a przy takiej objętości różnica i tak jest
    # jednoznaczna z szybkiego odczytu.
    _verify_max = int(os.environ.get("ARTWORK_VERIFY_MAX_CHARS", "1800"))
    _too_long_for_verify = max(len(va or ""), len(vb or "")) > _verify_max
    # Tabele NIE używają weryfikacji Sonnetem — werdykt daje (szybki) diff numeryczny,
    # a podwójny odczyt Sonnetem na dużej tabeli to główna przyczyna timeoutów pola
    # (różnica i tak była łapana numerycznie). Weryfikacja zostaje dla text/translation.
    if (changed and not both_nd
            and comparison_mode in ("text", "translation")
            and fld.get("severity", "critical") == "critical"
            and not _too_long_for_verify
            and os.environ.get("ARTWORK_OCR_VERIFY", "1") not in ("0", "false", "no")
            and _ocr_verify_model()):
        # A and B verify reads are independent accurate-model (Sonnet) OCR calls.
        # On long description panels each takes many seconds, so run them
        # concurrently — same pattern as the initial OCR above — to halve the
        # per-field verification latency instead of waiting for them serially.
        with ThreadPoolExecutor(max_workers=2) as _vex:
            _vfa = _vex.submit(_ocr_verify_read, crop_a_oriented)
            _vfb = _vex.submit(_ocr_verify_read, crop_b_oriented)
            va2 = _vfa.result()
            vb2 = _vfb.result()
        if va2 or vb2:
            va = (va2 or va) or "N/D"
            vb = (vb2 or vb) or "N/D"
            both_nd = va == "N/D" and vb == "N/D"
            changed = va != vb and not both_nd
            note_parts_verify = ("🔬 Zweryfikowano dokładnym modelem OCR"
                                 + (" — różnica potwierdzona" if changed
                                    else " — różnica była błędem OCR (zgodne)"))
            logger.info("[artwork-diff] pole=%r weryfikacja OCR: %s → ZMIANA=%s",
                        fld.get("display_name") or fname,
                        "potwierdzona" if changed else "odrzucona (błąd OCR)", changed)
        else:
            note_parts_verify = None
    else:
        note_parts_verify = None

    pix_sim = None
    if crop_a and crop_b:
        _cb_for_sim = crop_b
        # Register B onto A before measuring visual similarity. Without it a few-px
        # print/render shift drags SSIM down and fires a false "wizualnie różne"
        # verdict in table/graphic mode even when the content is identical (same
        # root cause as the full-page diff). Only worth the alignment cost in the
        # modes where pix_sim actually drives `changed`.
        if _ALIGN_ENABLED and comparison_mode in ("graphic", "numeric", "table", "translation"):
            try:
                _cb_al, _ = _align_crop_to_master(crop_a, crop_b)
                if _cb_al is not None:
                    _cb_for_sim = _cb_al
            except Exception as _ae:
                logger.debug("pix_sim alignment failed: %s", _ae)
        pix_sim = _icon_similarity(crop_a, _cb_for_sim)

    note_parts = [f"Pole szablonu '{profile_name}'"]
    if note_parts_verify:
        note_parts.append(note_parts_verify)
    if pix_sim is not None:
        note_parts.append(f"Podobieństwo pixelowe: {pix_sim:.0f}%")

    # ── Learning: apply threshold adjustments from operator feedback ─────
    # Higher FPR (false positive rate) → raise visual threshold → less sensitive
    _field_stats = (field_stats or {}).get(fname, {})
    _fpr = float(_field_stats.get("fpr", 0.0))
    _reviews = int(_field_stats.get("reviews", 0))
    _pix_threshold = 90   # default pixel-similarity threshold for triggering changed
    _sev_adjust = 0        # 0=no change, 1=down one step, 2=down two steps
    if _reviews >= 5:
        if _fpr >= 0.90:
            _pix_threshold = 96
            _sev_adjust = 2
            note_parts.append(
                f"🤖 Uczenie: FPR={_fpr:.0%} ({_reviews} ocen) — progi znacznie poluzowane"
            )
        elif _fpr >= 0.80:
            _pix_threshold = 93
            _sev_adjust = 1
            note_parts.append(
                f"🤖 Uczenie: FPR={_fpr:.0%} ({_reviews} ocen) — progi lekko poluzowane"
            )
        elif _fpr >= 0.70:
            note_parts.append(
                f"ℹ Uczenie: FPR={_fpr:.0%} ({_reviews} ocen) — obserwuj false positives"
            )

    # ── Mode-specific changed determination ──────────────────────────────
    if comparison_mode == "graphic":
        # Icons/pictograms: text OCR is unreliable; compare visually only.
        # Only overwrite `changed` when pix_sim is actually available — if both
        # crops failed to render (pix_sim=None), preserve the text-diff result
        # so a genuine N/D vs real-text difference is still flagged.
        if pix_sim is not None:
            changed = pix_sim < 95
        note_parts.append("Tryb: grafika – porównanie wizualne (próg 95%)")

    elif comparison_mode == "numeric":
        nums_a = _extract_numbers_from_text(va)
        nums_b = _extract_numbers_from_text(vb)
        tolerance = float(fld.get("numeric_tolerance", 0.0) or 0.0)
        if nums_a or nums_b:
            changed = _numeric_values_differ(nums_a, nums_b, tolerance)
            if changed:
                for _v in _numeric_diffs_list(nums_a, nums_b, tolerance):
                    for _x in _v:
                        if _x is not None and _distinctive_num(_x):
                            _num_diff_vals.add(f"{_x:g}")
            else:
                # Transpozycja: multizbiory wartości równe, ale komórki w innej
                # KOLEJNOŚCI (np. 17 i 24 zamienione miejscami). Sortowana bramka
                # wyżej tego nie łapie, a bywa realną wadą. Surfujemy MIĘKKO —
                # oznaczamy do przeglądu z severity zdegradowaną do „info", żeby nie
                # eskalować nieszkodliwego przetasowania OCR do twardej różnicy.
                _swap = [(_a, _b) for _a, _b in _numeric_diffs_list(nums_a, nums_b, tolerance)
                         if _a is not None and _b is not None]
                if _swap:
                    changed = True
                    _sev_adjust = max(_sev_adjust, 2)
                    note_parts.append(
                        "⚠ Możliwa transpozycja wartości (ta sama treść, inna "
                        "kolejność: "
                        + ", ".join(f"{_a:g}↔{_b:g}" for _a, _b in _swap)
                        + ") — do weryfikacji")
                    for _a, _b in _swap:
                        for _x in (_a, _b):
                            if _distinctive_num(_x):
                                _num_diff_vals.add(f"{_x:g}")
            note_parts.append(
                f"Tryb: liczba | wzorzec: {nums_a} | dostawca: {nums_b}"
                + (f" | tolerancja ±{tolerance}" if tolerance else "")
            )
        else:
            # No numbers extracted — fall back to text diff
            if pix_sim is not None and pix_sim < _pix_threshold and not changed:
                changed = True
                note_parts.append("⚠ Wizualnie różne (brak liczb w OCR)")
            note_parts.append("Tryb: liczba (brak danych numerycznych – tryb tekstowy)")

    elif comparison_mode == "table":
        # Text diff + extra numeric consistency check
        nums_a = _extract_numbers_from_text(va)
        nums_b = _extract_numbers_from_text(vb)
        tolerance = float(fld.get("numeric_tolerance", 0.0) or 0.0)
        # Always enumerate ALL numeric differences (not just a single boolean) so
        # the operator sees every differing cell in a multi-value table — e.g. a
        # Chemical Permeation table where K, P, T and the level all changed, not
        # only the one region the visual diff happened to highlight.
        num_diffs = (_numeric_diffs_list(nums_a, nums_b, tolerance)
                     if (nums_a or nums_b) else [])
        if num_diffs:
            changed = True
            _fmt = lambda v: "—" if v is None else f"{v:g}"
            _pairs = ", ".join(f"{_fmt(a)}→{_fmt(b)}" for a, b in num_diffs[:12])
            _more = f" (+{len(num_diffs) - 12} więcej)" if len(num_diffs) > 12 else ""
            note_parts.append(
                f"⚠ Różnice liczbowe ({len(num_diffs)}): {_pairs}{_more}")
            for a, b in num_diffs:
                # Per-side sets get EVERY differing value (incl. bare ints like the
                # Level column 2/6) — _locate_value_boxes drops the ambiguous ones
                # (repeated/legend) so we mark the unique changed cell precisely.
                if a is not None:
                    _num_diff_vals_a.add(f"{a:g}")
                    if _distinctive_num(a):
                        _num_diff_vals.add(f"{a:g}")
                if b is not None:
                    _num_diff_vals_b.add(f"{b:g}")
                    if _distinctive_num(b):
                        _num_diff_vals.add(f"{b:g}")
        # Strukturalne porównanie komórka-po-komórce (img2table) — TYLKO dla DOKŁADNYCH
        # ramek zmienionych komórek (np. Level 2→6). Liczone gdy jest sygnał różnicy
        # (numeryczny LUB wizualny). 'changed' modyfikujemy WYŁĄCZNIE addytywnie —
        # img2table może DODAĆ różnicę, ale NIGDY jej nie skasować (inaczej zła
        # segmentacja tabeli ukryłaby realny błąd — patrz regres Tabela+danych_1).
        _has_diff_signal = changed or (pix_sim is not None and pix_sim < _pix_threshold)
        if _has_diff_signal:
            try:
                _struct = _compare_table_structured(crop_a_oriented, crop_b_oriented, tolerance)
            except Exception as _se:
                logger.debug("structured table compare failed: %s", _se)
                _struct = None
        if _struct is not None:
            if _struct.get("boxes_a") or _struct.get("boxes_b"):
                _struct_boxes_a = _struct.get("boxes_a") or []
                _struct_boxes_b = _struct.get("boxes_b") or []
            if _struct.get("changed"):
                changed = True   # addytywnie — tylko dodaje różnicę
            note_parts.append(_struct.get("note", ""))
        if pix_sim is not None and pix_sim < _pix_threshold and not changed:
            changed = True
            note_parts.append("⚠ Wizualnie różne mimo zgodnego tekstu")
        note_parts.append("Tryb: tabela – kontrola tekstu i liczb")

    elif comparison_mode == "translation":
        if not both_nd:
            trans_changed, changed_lines, total_lines = _compare_lines_fuzzy(va, vb)
            # Replace token-diff result with the fuzzy line-diff result so that
            # minor OCR noise (token differences that fuzzy matching ignores)
            # does not produce false positives.
            changed = trans_changed
            note_parts.append(
                f"Tryb: tłumaczenie | zmienionych linii: {changed_lines}/{total_lines}"
            )
        if pix_sim is not None and pix_sim < _pix_threshold and not changed:
            changed = True
            note_parts.append("⚠ Wizualnie różne mimo zgodnych linii tekstu")

    else:
        # text mode (default). Tekst identyczny → NIE eskaluj do „różnicy" na samej
        # niższej podobności pikseli: jitter sub-pikselowy / inna jakość druku dają
        # pix_sim < 90 mimo IDENTYCZNEJ treści (fałszywe czerwone boxy na „Adresy"
        # itp.). Flaguj jako różnicę tylko gdy obraz jest DRASTYCZNIE inny (prawdop.
        # inny layout/grafika); drobną różnicę zostaw jako informację (bez red-boxów).
        _visual_floor = int(os.environ.get("ARTWORK_TEXTMODE_VISUAL_FLOOR", "62"))
        if pix_sim is not None and not changed:
            if pix_sim < _visual_floor:
                changed = True
                note_parts.append("⚠ Wizualnie wyraźnie różne mimo identycznego tekstu OCR")
            elif pix_sim < _pix_threshold:
                note_parts.append("ℹ Drobna różnica wizualna przy zgodnym tekście "
                                  "(jakość druku / wyrównanie) — nie traktuję jako różnicy")

    if fld.get("notes"):
        note_parts.append(fld["notes"])

    size_lbl = fld.get("size_label", "").strip()
    _dname = fld.get("display_name") or fld.get("field_name", "")
    field_label = (f"[Rozmiar {size_lbl}] {_dname}"
                   if size_lbl else f"[Szablon] {_dname}")

    # ── Diagnostic: full per-field decision trail ────────────────────────
    # Shows HOW the difference verdict was reached and the OCR inputs behind it,
    # so weak points (bad OCR read, no numbers extracted, visual-only flag) are
    # traceable in the logs.
    def _short(t, n=180):
        t = " ".join((t or "").split())
        return (t[:n] + "…") if len(t) > n else t
    logger.info(
        "[artwork-diff] pole=%r tryb=%s → ZMIANA=%s | pix_sim=%s | "
        "OCR wzorzec=%r | OCR dostawca=%r",
        _dname, comparison_mode, changed,
        (f"{pix_sim:.0f}%" if pix_sim is not None else "—"),
        _short(va), _short(vb))

    # When changed: align supplier→master, then compute SSIM diff overlay.
    # Alignment handles labels printed at different scales (the typical case for
    # supplier prints vs master spec) — without it pixel comparison is noise.
    diff_boxes_a, diff_boxes_b = [], []
    crop_a_out = crop_a_oriented
    crop_b_out = crop_b_oriented
    crop_b_aligned_b64 = None
    diff_overlay = None
    align_conf = 0.0
    if changed and crop_a_oriented and crop_b_oriented:
        try:
            crop_b_aligned, align_conf = _align_crop_to_master(crop_a_oriented, crop_b_oriented)
        except Exception as exc:
            logger.debug("crop alignment failed: %s", exc)
            crop_b_aligned = crop_b_oriented
        if comparison_mode == "graphic":
            # Pixel diff for icon/pictogram fields using aligned images.
            # de_thresh=18: medical icons are black-on-white (ΔE 50+ for ink
            # vs white). Same icon rendered from two PDF sources differs by
            # ΔE 3-12. Threshold 18 cleanly separates rendering noise from a
            # genuinely missing/changed icon.
            # Only fire when alignment succeeded — misaligned images produce
            # false boxes even at high ΔE thresholds.
            try:
                if align_conf >= 0.15:
                    diff_boxes_a, diff_boxes_b = _pixel_diff_boxes(
                        crop_a_oriented, crop_b_aligned,
                        thresh=25, min_area=50, strip_ratio=8,
                        max_coverage=0.15, max_boxes=5, de_thresh=18.0)
            except Exception as exc:
                logger.debug("graphic pixel-diff boxes failed: %s", exc)
        else:
            # Box localization = OCR WORD boxes (precise, per-token). Whole-field
            # pixel diff is NOT used here: when the two prints are slightly
            # misaligned it lights up on every text stroke and paints boxes on
            # identical words while missing the real diff — so it is only a
            # last-resort fallback under STRONG alignment. The textual note
            # ("Różnice liczbowe: …") already lists every differing value even when
            # a tiny table cell cannot be boxed.
            # Najpierw: precyzyjne boksy różniących się LICZB (te, które naprawdę
            # się różnią) — niewrażliwe na wyrównanie i na identyczny tekst.
            _val_a, _val_b = [], []
            # Per-side targets (master boxes its own changed values, supplier its own).
            # POMIJAMY dla tabel/liczb: tam numeric-diff bywa „spłaszczony" (śmieciowe
            # pary), więc boksy wartości trafiają w legendę — tabela używa wyłącznie
            # pixel-diffu. Skip oszczędza też 2 przebiegi Tesseracta (szybciej, mniejsze
            # ryzyko timeoutu dużego pola tabeli).
            _tgt_a = _num_diff_vals_a or _num_diff_vals
            _tgt_b = _num_diff_vals_b or _num_diff_vals
            if comparison_mode not in ("table", "numeric") and (_tgt_a or _tgt_b):
                try:
                    _val_a = _locate_value_boxes(crop_a_oriented, _tgt_a)
                    _val_b = _locate_value_boxes(crop_b_oriented, _tgt_b)
                except Exception as exc:
                    logger.debug("value-box localization failed: %s", exc)

            if comparison_mode in ("table", "numeric"):
                # Ramki = REALNIE różne komórki — dokładnie to, co świeci na mapie różnic.
                # Źródłem jest WYŁĄCZNIE pixel-diff (ΔE CIEDE2000). Dlaczego nie boksy
                # wartości: (1) numeric-diff tabeli bywa „spłaszczony" i produkuje
                # śmieciowe pary → boksy po wartościach trafiały w legendę
                # (10/60/120/240/480 min); (2) weryfikacja RGB tych boksów jest czuła na
                # sub-pikselowy szum druku, którego ΔE/SSIM nie łapie (dlatego mapa różnic
                # jest czysta, a boksy wartości — nie). Próg ΔE>15 odsiewa szum
                # renderowania (ΔE 3-6); duże zmiany (inna cyfra/wartość) mają ΔE >>15 i
                # zostają. Bez wyrównania nie zaznaczamy (jak mapa różnic).
                diff_boxes_a, diff_boxes_b = [], []
                if align_conf >= 0.30 and crop_b_aligned is not None:
                    try:
                        diff_boxes_a, diff_boxes_b = _pixel_diff_boxes(
                            crop_a_oriented, crop_b_aligned,
                            thresh=30, min_area=80, strip_ratio=8,
                            max_coverage=0.35, max_boxes=14, de_thresh=15.0)
                    except Exception as exc:
                        logger.debug("table pixel-diff boxes failed: %s", exc)
                _n_raw, _dropped = len(diff_boxes_a), 0
            else:
                _ocr_a, _ocr_b = [], []
                # Word-boxing (Tesseract image_to_data) na DUŻYM polu tekstowym (np.
                # wielojęzyczny Opis_2) potrafi trwać minuty i wpychać pole w timeout.
                # Dla bardzo długich pól pomijamy je — różnica i tak jest pokazana w
                # sekcji OCR (podświetlone słowa), a pole zwraca werdykt + obrazy szybko.
                if not _too_long_for_verify:
                    try:
                        _ocr_a, _ocr_b = _diff_text_boxes(
                            crop_a_oriented, crop_b_oriented, va, vb,
                            field_label=fld.get("display_name") or fname)
                    except Exception as exc:
                        logger.debug("diff-box overlay failed: %s", exc)
                # Use master-frame boxes as canonical (fall back to supplier-frame),
                # mirror to both sides for a consistent location.
                _boxes = _ocr_a if _ocr_a else _ocr_b
                _n_raw = len(_boxes)
                # Pixel-verify: drop boxes whose pixels are identical on both prints
                # (OCR misread the same text differently, e.g. 'klasi' vs 'klasy').
                if _boxes and align_conf >= 0.15 and crop_b_aligned is not None:
                    _boxes = [b for b in _boxes
                              if _region_pixels_differ(crop_a_oriented, crop_b_aligned, b)]
                _dropped = _n_raw - len(_boxes)
                diff_boxes_a, diff_boxes_b = list(_boxes), list(_boxes)
                # Dorzuć precyzyjne boksy zmienionych liczb (jeśli były).
                if _val_a or _val_b:
                    diff_boxes_a = _dedupe_pct_boxes(diff_boxes_a + _val_a)
                    diff_boxes_b = _dedupe_pct_boxes(diff_boxes_b + _val_b)
            # Last-resort: OCR localized nothing on EITHER side AND alignment is
            # strong → a constrained pixel diff is safe (strong align ⇒ identical
            # text won't light up). Requires both sides empty so we never discard
            # value boxes found on just one side.
            if not diff_boxes_a and not diff_boxes_b and align_conf >= 0.45:
                try:
                    diff_boxes_a, diff_boxes_b = _pixel_diff_boxes(
                        crop_a_oriented, crop_b_aligned,
                        thresh=22, min_area=60, strip_ratio=8,
                        max_coverage=0.22, max_boxes=6, de_thresh=15.0)
                except Exception as exc:
                    logger.debug("pixel-diff boxes fallback failed: %s", exc)
            logger.info(
                "[artwork-diff] pole=%r tryb=%s align=%.0f%% | ocr-boxy=%d "
                "(odrzucone-jako-identyczne=%d) → razem=%d",
                fld.get("display_name") or fname, comparison_mode, align_conf * 100,
                _n_raw, _dropped, len(diff_boxes_a))
            if _dropped:
                logger.info("[artwork-diff]   ⚠ %d boxów OCR pominięto — OCR odczytał "
                            "inny tekst, ale piksele identyczne (błąd OCR, nie różnica)",
                            _dropped)
        if diff_boxes_a:
            crop_a_out = _annotate_crop_with_boxes(crop_a_oriented, diff_boxes_a)
        if diff_boxes_b:
            crop_b_out = _annotate_crop_with_boxes(crop_b_oriented, diff_boxes_b)
        try:
            diff_overlay = _ssim_diff_overlay(crop_a_oriented, crop_b_aligned)
        except Exception as exc:
            logger.debug("ssim diff overlay failed: %s", exc)
            diff_overlay = _pixel_diff_overlay(crop_a_oriented, crop_b_aligned)
        if align_conf >= 0.3:
            crop_b_aligned_b64 = _img_to_b64(crop_b_aligned, max_width=2400, quality=98)
        if align_conf > 0:
            note_parts.append(f"Wyrównanie geometryczne: conf={align_conf:.0%}")
    # Wszystkie cropy wychodzą w identycznej rozdzielczości (2400px) i identycznej
    # jakości JPEG (98) — żeby operator widział master i dostawcę w równej jakości
    # niezależnie od proporcji źródłowych PDF-ów.
    # Apply severity downgrade from learning stats
    _sev_map   = {"critical": 0, "important": 1, "info": 2}
    _sev_unmap = {0: "critical", 1: "important", 2: "info"}
    _orig_sev  = fld.get("severity", "critical")
    _final_sev = _sev_unmap.get(
        min(_sev_map.get(_orig_sev, 0) + _sev_adjust, 2), "info"
    ) if _sev_adjust > 0 else _orig_sev
    return {
        "field":    field_label,
        "val_a":    va,
        "val_b":    vb,
        "severity": _final_sev,
        "changed":  changed,
        "note":     " | ".join(note_parts),
        "display_layout": fld.get("display_layout", "side_by_side"),
        "crop_a_b64": _img_to_b64(crop_a_out, max_width=2400, quality=98) if crop_a_out else None,
        "crop_b_b64": _img_to_b64(crop_b_out, max_width=2400, quality=98) if crop_b_out else None,
        "crop_b_aligned_b64": crop_b_aligned_b64,
        "diff_overlay_b64": _img_to_b64(diff_overlay, max_width=2400, quality=95) if diff_overlay else None,
        "diff_boxes_a": diff_boxes_a,
        "diff_boxes_b": diff_boxes_b,
        "align_confidence": align_conf,
    }


def _apply_profile_comparison(path_a: str, path_b: str,
                               profile: dict, page_a=None, page_b=None,
                               field_overrides_b: dict = None,
                               profiler=None) -> list:
    """
    Wycina pola z obu artworków wg definicji szablonu (300 DPI).
    Uruchamia OCR na każdym wycinku i porównuje — pola przetwarzane równolegle.

    field_overrides_b: optional dict {field_name: {x1_pct,y1_pct,x2_pct,y2_pct} | None}
      None value → skip this field (user marked as skipped during preview)
      dict value → use these coords for B crop instead of profile coords
    """
    if not HAS_PIL:
        return []

    pages_a = _load_pages(path_a, _PROFILE_RENDER_DPI, page_idx=page_a)
    pages_b = _load_pages(path_b, _PROFILE_RENDER_DPI, page_idx=page_b)

    if not pages_a or not pages_b:
        return []

    img_a = pages_a[0]
    img_b = pages_b[0]
    profile_name = profile.get("name", "")

    fields = profile.get("fields", [])
    # Cap parallel workers: LightGlue+EasyOCR are CPU-heavy, so too many workers
    # saturates CPU and cascades into 30s timeouts on production servers. Default 3;
    # raise via ARTWORK_FIELD_WORKERS for templates that are mostly text/translation
    # fields (API-bound OCR, little local CPU work).
    _max_workers = int(os.environ.get("ARTWORK_FIELD_WORKERS", "3"))
    workers = min(max(1, _max_workers), len(fields) or 1)

    def _timed_field(fld):
        # Runs in a worker thread — time the actual field processing and record it
        # to the (thread-safe) profiler so the report can highlight slow fields.
        _s = time.perf_counter()
        try:
            return _process_profile_field(fld, img_a, img_b, field_overrides_b, profile_name)
        finally:
            if profiler is not None:
                _nm = (fld.get("display_name") or fld.get("field_name")
                       or fld.get("size_label") or "?")
                profiler.field(str(_nm), (time.perf_counter() - _s) * 1000.0)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            (fld, executor.submit(_timed_field, fld))
            for fld in fields
        ]
        # Per-field timeout kept UNDER gunicorn's request timeout (default 180s) so a
        # single slow field fails gracefully as an error row instead of blocking the
        # whole compare past the worker timeout and killing it. ARTWORK_FIELD_TIMEOUT.
        try:
            _field_timeout = int(os.environ.get("ARTWORK_FIELD_TIMEOUT", "150"))
        except ValueError:
            _field_timeout = 150
        rows = []
        for fld, f in futures:
            _t_field = time.time()
            try:
                rows.append(f.result(timeout=_field_timeout))
            except Exception as _fe:
                _elapsed_field = time.time() - _t_field
                logger.error("Profile field compare worker error: %s: %s",
                             type(_fe).__name__, _fe or "(no message)")
                size_lbl = fld.get("size_label", "").strip()
                _dname = fld.get("display_name") or fld.get("field_name", "")
                _is_timeout = isinstance(_fe, TimeoutError) or "timeout" in type(_fe).__name__.lower()
                _note = (f"Timeout przetwarzania pola po {_elapsed_field:.0f}s"
                         if _is_timeout else f"Błąd przetwarzania pola: {_fe}")
                rows.append({
                    "field":    (f"[Rozmiar {size_lbl}] {_dname}"
                                 if size_lbl else f"[Szablon] {_dname}"),
                    "val_a":    "N/D",
                    "val_b":    "N/D",
                    "severity": fld.get("severity", "critical"),
                    "changed":  True,
                    "note":     _note,
                    "display_layout": fld.get("display_layout", "side_by_side"),
                    "crop_a_b64": None,
                    "crop_b_b64": None,
                })

    return [r for r in rows if r is not None]


# ─── QUEUE-BASED FIELD COMPARISON ─────────────────────────────────────────────

# {queue_id: {"img_a": PIL, "img_b": PIL, "profile": dict, "ts": float}}
_field_queue_cache: dict = {}
_field_queue_lock = __import__("threading").RLock()
_FIELD_QUEUE_TTL = 3600  # 1 hour

# Shared-disk persistence dir for cross-worker access (all gunicorn workers share /tmp)
_QUEUE_PERSIST_DIR = os.path.join(os.path.dirname(__file__), "instance", "queues")


def _qpersist(queue_id: str, ctx: dict) -> None:
    """Save queue context to shared disk so other gunicorn workers can load it."""
    try:
        import json as _j
        os.makedirs(_QUEUE_PERSIST_DIR, exist_ok=True)
        png_a = os.path.join(_QUEUE_PERSIST_DIR, f"{queue_id}_a.png")
        png_b = os.path.join(_QUEUE_PERSIST_DIR, f"{queue_id}_b.png")
        ctx["img_a"].save(png_a, "PNG")
        ctx["img_b"].save(png_b, "PNG")
        meta = {
            "png_a": png_a, "png_b": png_b,
            "profile": ctx["profile"], "ts": ctx["ts"],
        }
        with open(os.path.join(_QUEUE_PERSIST_DIR, f"{queue_id}.json"), "w") as f:
            _j.dump(meta, f)
    except Exception as _e:
        logger.debug("queue persist failed: %s", _e)


def _qload(queue_id: str) -> dict | None:
    """Load queue context from disk (cross-worker recovery). Returns None on miss/expiry."""
    try:
        import json as _j
        meta_path = os.path.join(_QUEUE_PERSIST_DIR, f"{queue_id}.json")
        if not os.path.exists(meta_path):
            return None
        with open(meta_path) as f:
            meta = _j.load(f)
        import time as _t
        if _t.time() - meta.get("ts", 0) > _FIELD_QUEUE_TTL:
            return None
        from PIL import Image as _PILImg
        _raw_a = _PILImg.open(meta["png_a"])
        try:
            _img_a = _raw_a.copy()
        finally:
            _raw_a.close()
        _raw_b = _PILImg.open(meta["png_b"])
        try:
            _img_b = _raw_b.copy()
        finally:
            _raw_b.close()
        ctx = {
            "img_a": _img_a,
            "img_b": _img_b,
            "profile": meta["profile"], "ts": meta["ts"],
            "field_overrides": {}, "field_stats": {},
            "_temp_files": [],
        }
        return ctx
    except Exception as _e:
        logger.debug("queue load from disk failed: %s", _e)
        return None


def _qdelete(queue_id: str) -> None:
    """Remove persisted queue files from disk."""
    for suffix in ("_a.png", "_b.png", ".json"):
        p = os.path.join(_QUEUE_PERSIST_DIR, f"{queue_id}{suffix}")
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


def field_queue_init(path_a: str, path_b: str, profile: dict,
                     page_a: int = 0, page_b: int = 0,
                     field_overrides: dict = None,
                     temp_files: list = None,
                     field_stats: dict = None) -> dict:
    """
    Load both pages at profile DPI and cache them under a new queue_id.
    Returns {queue_id, fields: [{field_name, display_name, severity,
                                  crop_a_b64, crop_b_b64, coords}]}
    field_overrides: optional {field_name: {x1_pct,y1_pct,x2_pct,y2_pct}|null}
      where a dict overrides supplier crop coords, and None means "use master coords".
    temp_files: optional list of file paths to delete when the queue is destroyed.
    """
    import hashlib, time as _t
    if not HAS_PIL:
        return {"error": "PIL niedostępny"}

    pages_a = _load_pages(path_a, _PROFILE_RENDER_DPI, page_idx=page_a)
    pages_b = _load_pages(path_b, _PROFILE_RENDER_DPI, page_idx=page_b)
    if not pages_a or not pages_b:
        return {"error": "Nie można załadować stron PDF"}

    img_a = pages_a[0]
    img_b = pages_b[0]

    # Capture timestamp once so that eviction cannot use a later now= that
    # would make the just-inserted entry appear stale (e.g. after clock jump).
    import os as _os
    ts_now = _t.time()
    queue_id = hashlib.md5(f"{path_a}{path_b}{ts_now}".encode(), usedforsecurity=False).hexdigest()[:16]
    overrides = field_overrides or {}
    queue_entry = {
        "img_a": img_a, "img_b": img_b,
        "profile": profile, "ts": ts_now,
        "field_overrides": overrides,
        "field_stats": field_stats or {},
        "_temp_files": [p for p in (temp_files or []) if p],
    }
    with _field_queue_lock:
        _field_queue_cache[queue_id] = queue_entry
        # Evict old entries and clean up their temp files.
        # Use the same ts_now so the new entry is never considered stale.
        for k in [k for k, v in _field_queue_cache.items()
                  if k != queue_id and (ts_now - v["ts"]) > _FIELD_QUEUE_TTL]:
            ctx = _field_queue_cache.pop(k)
            for p in ctx.get("_temp_files", []):
                try:
                    if _os.path.exists(p):
                        _os.remove(p)
                except Exception:
                    pass
            _qdelete(k)

    # Persist to shared disk so other gunicorn workers can load this queue.
    # Use the local reference captured before the lock to avoid a KeyError
    # if field_queue_destroy() races between lock-release and this call.
    _qpersist(queue_id, queue_entry)

    # Build field preview list
    fields_out = []
    for fld in profile.get("fields", []):
        if fld.get("skip_analysis"):
            continue
        if not all(k in fld for k in ("x1_pct", "y1_pct", "x2_pct", "y2_pct")):
            continue
        x1, y1, x2, y2 = fld["x1_pct"], fld["y1_pct"], fld["x2_pct"], fld["y2_pct"]
        crop_a = _crop_field_region(img_a, x1, y1, x2, y2)

        # Supplier crop: prefer verified/auto-located coords from overrides
        ov = overrides.get(fld.get("field_name"))
        # Default rotation from field-dict global default (merged at load time)
        try:
            rot = int(fld.get("rotation", 0) or 0) % 360
        except (TypeError, ValueError):
            rot = 0
        if isinstance(ov, dict):
            if all(k in ov for k in ("x1_pct", "y1_pct", "x2_pct", "y2_pct")):
                bx1, by1, bx2, by2 = ov["x1_pct"], ov["y1_pct"], ov["x2_pct"], ov["y2_pct"]
            else:
                bx1, by1, bx2, by2 = x1, y1, x2, y2
            try:
                rot = int(ov.get("rotation", rot) or rot) % 360
            except (TypeError, ValueError):
                pass
        else:
            bx1, by1, bx2, by2 = x1, y1, x2, y2
        crop_b = _crop_field_region(img_b, bx1, by1, bx2, by2)

        # Rotation applies to BOTH crops for display, but crop_a (master) must be
        # rotated from a fresh reference — the loop variable must not be mutated
        # because `crop_a` here is derived from profile coords that don't change.
        crop_a_preview = crop_a.rotate(-rot, expand=True) if (rot and crop_a) else crop_a
        crop_b_preview = crop_b.rotate(-rot, expand=True) if (rot and crop_b) else crop_b

        # Show raw crops in preview. OCR-determined orientation comes back in
        # the compare result and replaces these placeholders.
        fields_out.append({
            "field_name":   fld.get("field_name", ""),
            "display_name": fld.get("display_name", fld.get("field_name", "")),
            "severity":     fld.get("severity", "critical"),
            "display_layout": fld.get("display_layout", "side_by_side"),
            "coords": {"x1_pct": x1, "y1_pct": y1, "x2_pct": x2, "y2_pct": y2},
            "rotation": rot,
            "crop_a_b64": _img_to_b64(crop_a_preview, max_width=2400, quality=98) if crop_a_preview else None,
            "crop_b_b64": _img_to_b64(crop_b_preview, max_width=2400, quality=98) if crop_b_preview else None,
        })

    return {"queue_id": queue_id, "fields": fields_out}


def field_queue_compare_one(queue_id: str, field_name: str,
                             override_b: dict = None) -> dict:
    """
    Compare a single field from a queued context.
    override_b: optional {x1_pct, y1_pct, x2_pct, y2_pct} for B crop.
    Returns the same row dict as _process_profile_field.
    """
    with _field_queue_lock:
        ctx = _field_queue_cache.get(queue_id)
    if not ctx:
        # Queue not in this worker's memory — try loading from shared disk
        # (happens when a different gunicorn worker handled the init request).
        ctx = _qload(queue_id)
        if ctx:
            with _field_queue_lock:
                _field_queue_cache[queue_id] = ctx
        else:
            return {"error": "Kolejka wygasła — uruchom porównanie od nowa"}

    profile = ctx["profile"]
    img_a = ctx["img_a"]
    img_b = ctx["img_b"]

    fld = next((f for f in profile.get("fields", [])
                if f.get("field_name") == field_name), None)
    if not fld:
        return {"error": f"Nieznane pole: {field_name}"}

    # Fall back to verified coords stored at init time if caller didn't supply one.
    # Critical: stored value may be None (skip sentinel) — must use a non-None
    # sentinel to detect "key absent" vs "key present with None value" because
    # `if override_b` would treat a skip-None as "no override" and process the field.
    _field_overrides = ctx.get("field_overrides") or {}
    has_override = override_b is not None or field_name in _field_overrides
    if override_b is None and field_name in _field_overrides:
        override_b = _field_overrides[field_name]  # None = skip, dict = coords
    overrides = {field_name: override_b} if has_override else None
    field_stats = ctx.get("field_stats") or {}
    result = _process_profile_field(fld, img_a, img_b, overrides,
                                    profile.get("name", ""),
                                    field_stats=field_stats)
    return result or {"error": "Brak wyniku"}


def field_queue_finalize(queue_id: str, field_rows: list,
                         file_a: str = "", file_b: str = ""):
    """Zbuduj pełny ArtworkCompareResult z JUŻ policzonych wierszy kolejki pól —
    BEZ ponownego liczenia porównania. Wiersze (z cropami) wystarczą do raportu,
    więc finalize NIE wymaga kontekstu kolejki: gdy kolejka jeszcze żyje, dokłada
    podgląd stron / kolorymetrię / kody; gdy wygasła — raport ma same pola. Dzięki
    temu finalize ZAWSZE się udaje i nie ma potrzeby ponownego, timeoutującego runu."""
    ctx = None
    with _field_queue_lock:
        ctx = _field_queue_cache.get(queue_id)
    if not ctx and queue_id:
        ctx = _qload(queue_id)
        if ctx:
            with _field_queue_lock:
                _field_queue_cache[queue_id] = ctx
    img_a = ctx.get("img_a") if ctx else None
    img_b = ctx.get("img_b") if ctx else None
    profile = (ctx.get("profile") if ctx else None) or {}

    # Pomijamy tylko CELOWO wykluczone (skipped/removed). Pola z BŁĘDEM/timeoutem
    # NIE są wyrzucane — pole, które się nie policzyło, musi być WIDOCZNE w raporcie
    # jako krytyczne (ciche APPROVED z pominiętym polem byłoby groźne).
    rows = []
    for r in (field_rows or []):
        if not isinstance(r, dict) or r.get("skipped") or r.get("removed"):
            continue
        if r.get("error") and not r.get("changed"):
            r = dict(r)
            r["severity"] = "critical"
            r["changed"] = True
            r.setdefault("note", "Pole nie zostało przetworzone (błąd/timeout) — wymaga ręcznej weryfikacji")
            r.setdefault("val_a", "—")
            r.setdefault("val_b", "—")
        rows.append(r)
    crit = sum(1 for r in rows if r.get("severity") == "critical" and r.get("changed"))
    imp  = sum(1 for r in rows if r.get("severity") == "important" and r.get("changed"))
    info = sum(1 for r in rows if r.get("severity") == "info"      and r.get("changed"))
    okc  = sum(1 for r in rows if not r.get("changed"))
    # Map field-count severity → overall risk_level scale (ok/warning/error/critical).
    # Must stay identical to the mapping in compare_artworks (~7495):
    #   critical change → critical, important change → error, info change → warning.
    risk = "critical" if crit else "error" if imp else "warning" if info else "ok"

    res = ArtworkCompareResult(
        file_a=file_a or "wzorzec", file_b=file_b or "dostawca",
        pages_a=1, pages_b=1,
        field_report=rows,
        profile_active=True, profile_name=profile.get("name", ""),
        critical_count=crit, important_count=imp, info_count=info, ok_count=okc,
        risk_level=risk, mapped_field_count=len(rows),
    )
    # Podgląd stron + kolorymetria + kody — TYLKO gdy mamy jeszcze obrazy z kolejki.
    if img_a is not None and img_b is not None:
        try:
            _a_t, _b_t = _img_to_b64(img_a), _img_to_b64(img_b)
            _a_h = _img_to_b64(img_a, max_width=HIRES_W)
            _b_h = _img_to_b64(img_b, max_width=HIRES_W)
            res.page_diffs = [PageDiff(
                page_num=1, pixel_diff_pct=0.0, diff_regions=[], text_diffs=[], field_diffs=[],
                img_a_b64=_a_t, img_b_b64=_b_t,
                img_a_annot_b64=_a_t, img_b_annot_b64=_b_t, img_diff_b64=None,
                img_a_hires_b64=_a_h, img_b_hires_b64=_b_h,
                img_a_annot_hires_b64=_a_h, img_b_annot_hires_b64=_b_h,
            )]
        except Exception as _e:
            logger.debug("finalize page preview failed: %s", _e)
        try:
            res.colorimetry["a"] = _get_colorimetry(img_a)
            res.colorimetry["b"] = _get_colorimetry(img_b)
            res.colorimetry["global"] = _global_color_compare(
                res.colorimetry.get("a"), res.colorimetry.get("b"))
        except Exception:
            pass
        try:
            from barcode_validator import validate_artwork_barcodes
            res.barcode_report = validate_artwork_barcodes("", "", [img_a], [img_b])
        except Exception:
            res.barcode_report = {}
    res.summary = f"Wykryto {crit} krytycznych i {imp} ważnych zmian ({len(rows)} pól)."
    return res


def field_queue_destroy(queue_id: str) -> None:
    """Release cached images and delete temp files for a finished queue."""
    with _field_queue_lock:
        ctx = _field_queue_cache.pop(queue_id, None)
    if ctx:
        import os as _os
        for p in ctx.get("_temp_files", []):
            try:
                if _os.path.exists(p):
                    _os.remove(p)
            except Exception:
                pass
    _qdelete(queue_id)


def extract_field_crops_for_preview(path_a: str, path_b: str,
                                      profile: dict,
                                      page_a: int = 0,
                                      page_b: int = 0) -> dict:
    """Extract field crops from both documents for user preview/validation.

    Returns a dict with:
      fields: list of {field_name, display_name, severity, coords_pct, crop_a_b64, crop_b_b64}
      page_b_b64: rendered page B image (for the crop editor)
      page_b_w, page_b_h: pixel dimensions of page_b_b64 image
    """
    if not HAS_PIL:
        return {"error": "PIL niedostępny"}

    pages_a = _load_pages(path_a, _PROFILE_RENDER_DPI, page_idx=page_a)
    pages_b = _load_pages(path_b, _PROFILE_RENDER_DPI, page_idx=page_b)

    if not pages_a or not pages_b:
        return {"error": "Nie można wyrenderować stron"}

    img_a = pages_a[0]
    img_b = pages_b[0]
    # Source images stay unrotated so profile coords, auto-locate, and the user's
    # drag-to-adjust rectangle on page_b all share the same coordinate system.

    fields_out = []
    for fld in profile.get("fields", []):
        if not all(k in fld for k in ("x1_pct", "y1_pct", "x2_pct", "y2_pct")):
            continue
        x1, y1, x2, y2 = fld["x1_pct"], fld["y1_pct"], fld["x2_pct"], fld["y2_pct"]
        crop_a = _crop_field_region(img_a, x1, y1, x2, y2)

        # Auto-locate this field on page B
        bx1, by1, bx2, by2 = x1, y1, x2, y2
        auto_conf = 0.0
        if crop_a:
            located, auto_conf = _auto_locate_field(crop_a, img_b, x1, y1, x2, y2)
            if auto_conf > 0:
                bx1, by1, bx2, by2 = (located["x1_pct"], located["y1_pct"],
                                       located["x2_pct"], located["y2_pct"])

        crop_b = _crop_field_region(img_b, bx1, by1, bx2, by2)
        # Do NOT bake the user's rotation into the preview crop — the client applies
        # it via CSS so it stays editable and is reflected consistently everywhere
        # (and "remembered", because we return `rotation` below). Auto OCR-orientation
        # is applied only when the user hasn't set an explicit rotation.
        try:
            fld_rot = int(fld.get("rotation", 0) or 0) % 360
        except (TypeError, ValueError):
            fld_rot = 0
        if fld_rot:
            crop_a_disp, crop_b_disp = crop_a, crop_b
        else:
            crop_a_disp = _orient_for_ocr(crop_a) if crop_a else None
            crop_b_disp = _orient_for_ocr(crop_b) if crop_b else None
        fields_out.append({
            "field_name":   fld.get("field_name", ""),
            "display_name": fld.get("display_name", fld.get("field_name", "")),
            "severity":     fld.get("severity", "critical"),
            "size_label":   fld.get("size_label", ""),
            "rotation":     fld_rot,
            "coords_pct":   {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
            "auto_coords_pct": {"x1": bx1, "y1": by1, "x2": bx2, "y2": by2},
            "auto_confidence": round(auto_conf * 100),
            "crop_a_b64":   _img_to_b64(crop_a_disp, max_width=500, quality=90) if crop_a_disp else None,
            "crop_b_b64":   _img_to_b64(crop_b_disp, max_width=500, quality=90) if crop_b_disp else None,
        })

    # Medium-res page B for the crop editor (kept unrotated — user's rectangle
    # coordinates must match the source image coordinate system)
    page_b_b64 = _img_to_b64(img_b, max_width=1200, quality=88)
    return {
        "fields":    fields_out,
        "page_b_b64": page_b_b64,
        "page_b_w":  img_b.size[0],
        "page_b_h":  img_b.size[1],
    }


def _get_api_key_artwork() -> str:
    """Czyta klucz API z env lub z bazy danych ustawień."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    try:
        from db import get_db
        db = get_db()
        try:
            row = db.execute(
                "SELECT value FROM settings WHERE category='ai' AND key='anthropic_api_key'"
            ).fetchone()
        finally:
            db.close()
        if row and row[0]:
            key = row[0].strip()
            if key:
                os.environ["ANTHROPIC_API_KEY"] = key
                return key
    except Exception:
        pass
    return ""


def _compare_size_tables_transformer(img_a, img_b) -> list:
    """Detect size tables with Table Transformer, then OCR each region and compare.

    Returns field_report rows like _compare_size_table_vision, or [] when
    the model is unavailable or no tables are detected in either image.
    """
    if img_a is None or img_b is None:
        return []
    tables_a = _detect_tables_transformer(img_a)
    tables_b = _detect_tables_transformer(img_b)
    if not tables_a and not tables_b:
        return []

    rows = []
    # Compare the highest-scoring table from each side
    best_a = tables_a[0] if tables_a else None
    best_b = tables_b[0] if tables_b else None

    def _ocr_table_crop(img, t):
        if t is None:
            return "N/D"
        crop = _crop_field_region(img, t["x1_pct"], t["y1_pct"],
                                   t["x2_pct"], t["y2_pct"])
        if crop is None:
            return "N/D"
        # img2table gives structured cell content; fall back to raw OCR cascade
        text = _img2table_extract(crop)
        if not text:
            text = _extract_text_ocr(crop, auto_orient=False).strip()
        text = " ".join(text.split())
        return text or "N/D"

    text_a = _ocr_table_crop(img_a, best_a)
    text_b = _ocr_table_crop(img_b, best_b)

    if best_a is None:
        rows.append({"field": "Tabela rozmiarów (Transformer)",
                     "val_a": "BRAK", "val_b": text_b,
                     "severity": "critical", "changed": True,
                     "note": "Table Transformer: brak tabeli w matrycy"})
    elif best_b is None:
        rows.append({"field": "Tabela rozmiarów (Transformer)",
                     "val_a": text_a, "val_b": "BRAK",
                     "severity": "critical", "changed": True,
                     "note": "Table Transformer: brak tabeli u dostawcy"})
    else:
        note = (f"Table Transformer: pewność A={best_a['score']:.0%} B={best_b['score']:.0%} | "
                f"lokalizacja A=({best_a['x1_pct']:.0f}%,{best_a['y1_pct']:.0f}%) "
                f"B=({best_b['x1_pct']:.0f}%,{best_b['y1_pct']:.0f}%)")
        both_nd = text_a == "N/D" and text_b == "N/D"
        if both_nd:
            # OCR failed on both sides — flag as unverified rather than silently OK
            text_a = text_b = "Nie zweryfikowano (OCR)"
            changed = True
            note += " | Brak odczytu OCR po obu stronach — zweryfikuj ręcznie"
        else:
            changed = text_a != text_b
        rows.append({"field": "Tabela rozmiarów (Transformer)",
                     "val_a": text_a, "val_b": text_b,
                     "severity": "critical", "changed": changed, "note": note})
    return rows


def _compare_size_table_vision(img_a_b64: Optional[str], img_b_b64: Optional[str]) -> list:
    """
    Dedykowane wywołanie Claude Vision dla tabeli rozmiarów.
    Analizuje HIRES obrazy i porównuje tabelę rozmiar→długość palców.
    Zwraca listę wpisów kompatybilnych z field_report.
    """
    import urllib.request as _ur
    api_key = _get_api_key_artwork()
    if not api_key or not img_a_b64 or not img_b_b64:
        return []

    def _prep_img(b64: str, max_kb: int = 1100) -> Optional[str]:
        try:
            import base64 as _b64
            from io import BytesIO
            raw = _b64.b64decode(b64)
            with Image.open(BytesIO(raw)) as _im:
                img = _im.convert("RGB")   # zamknij oryginalny uchwyt PIL
            for quality in (80, 65, 50):
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=quality)
                enc = buf.getvalue()
                if len(enc) <= max_kb * 1024:
                    return _b64.b64encode(enc).decode()
            return None
        except Exception:
            return None

    ia = _prep_img(img_a_b64)
    ib = _prep_img(img_b_b64)
    if not ia or not ib:
        return []

    content = [
        {"type": "text",  "text": "ARTWORK A (wzorzec ACME / master):"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": ia}},
        {"type": "text",  "text": "ARTWORK B (proof dostawcy / supplier):"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": ib}},
        {"type": "text",  "text": (
            "Przeszukaj oba artworki pod kątem tabeli rozmiarów rękawic.\n"
            "Tabela zawiera rozmiary (XS/S/M/L/XL) z zakresami długości palców (np. 5-6 cm).\n\n"
            "ZASADY — stosuj rygorystycznie:\n"
            "- Jeśli tabela NIE jest wyraźnie widoczna i czytelna → ustaw table_found na false\n"
            "- NIE zgaduj wartości — tylko odczytaj to co WYRAŹNIE widać w tekście\n"
            "- Jeśli wartość nieczytelna → pomiń ten wiersz (nie dopisuj)\n"
            "- Raportuj TYLKO rzeczywiste różnice między A i B\n\n"
            "Zwróć WYŁĄCZNIE JSON (bez markdown):\n"
            '{"table_found_a": true/false, "table_found_b": true/false,\n'
            ' "rows_a": [{"size": "XS", "value": "5-6"}, ...],\n'
            ' "rows_b": [{"size": "XS", "value": "5-6"}, ...],\n'
            ' "differences": ["opis różnicy..."],\n'
            ' "identical": true/false}'
        )},
    ]

    payload = json.dumps({
        "model": _current_ai_model(),
        "max_tokens": 800,
        "messages": [{"role": "user", "content": content}],
    }).encode("utf-8")

    try:
        req = _ur.Request(
            "https://api.anthropic.com/v1/messages", data=payload,
            headers={"Content-Type": "application/json", "x-api-key": api_key,
                     "anthropic-version": "2023-06-01"}, method="POST")
        # Bandit B310: URL stały w kodzie (https://api.anthropic.com), schemat nie pochodzi z wejścia.
        with _ur.urlopen(req, timeout=35) as resp:  # nosec B310
            data = json.loads(resp.read())
        try:
            from api_usage_tracker import record_usage
            record_usage(_current_ai_model(), data.get("usage", {}), call_type="artwork_size_table")
        except Exception:
            pass
        _content = data.get("content") or []
        if not _content:
            return []
        raw = (_content[0].get("text") or "").strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
        if not raw.startswith("{"):
            m = re.search(r"\{[\s\S]+\}", raw)
            if m:
                raw = m.group()
        res = json.loads(raw)

        rows_a = {r["size"]: r.get("value", "?") for r in (res.get("rows_a") or []) if r.get("size")}
        rows_b = {r["size"]: r.get("value", "?") for r in (res.get("rows_b") or []) if r.get("size")}
        fa = res.get("table_found_a", bool(rows_a))
        fb = res.get("table_found_b", bool(rows_b))

        report_rows = []
        if fa and not fb:
            report_rows.append({
                "field": "Tabela rozmiarów",
                "val_a": "obecna", "val_b": "BRAK",
                "severity": "critical", "changed": True,
                "note": "Tabela rozmiarów nie znaleziona u dostawcy"
            })
        elif not fa and fb:
            report_rows.append({
                "field": "Tabela rozmiarów",
                "val_a": "BRAK", "val_b": "obecna",
                "severity": "critical", "changed": True,
                "note": "Tabela rozmiarów nie znaleziona w matrycy"
            })
        elif not fa and not fb:
            return []  # brak tabeli w obu — nie dotyczy

        # Porównanie wiersz po wierszu
        size_order = ["XS", "S", "M", "L", "XL", "XXL"]
        all_sizes = sorted(
            set(list(rows_a.keys()) + list(rows_b.keys())),
            key=lambda s: size_order.index(s) if s in size_order else 99
        )
        for sz in all_sizes:
            va = rows_a.get(sz, "N/D")
            vb = rows_b.get(sz, "N/D")
            changed = va != vb
            report_rows.append({
                "field": f"Rozmiar {sz} — dł. palców",
                "val_a": va, "val_b": vb,
                "severity": "critical" if changed else "ok",
                "changed": changed,
                "note": "Tabela rozmiarów — krytyczna dla identyfikacji wyrobu medycznego"
            })
        return report_rows
    except Exception:
        return []


def _ai_analyze(result: "ArtworkCompareResult") -> Optional[dict]:
    import urllib.request
    api_key = _get_api_key_artwork()
    if not api_key:
        return None

    # Zbierz zmienione pola z analizy regex
    changed_fields = "\n".join(
        f"  [{r['severity'].upper()}] {r['field']}: {r['val_a']!r} → {r['val_b']!r}"
        + (f" ({r['note']})" if r.get("note") else "")
        for r in result.field_report if r["changed"]
    ) or "  (brak wykrytych zmian w polach tekstowych)"

    ok_fields = ", ".join(
        r["field"] for r in result.field_report if not r["changed"] and r["val_a"] != "—"
    ) or "—"

    # Zbuduj wiadomość — z obrazami jeśli dostępne
    content = []

    # Spróbuj dodać obrazy pierwszej strony (vision)
    page0 = result.page_diffs[0] if result.page_diffs else None
    has_images = page0 and (page0.img_a_b64 or page0.img_b_b64)

    def _safe_img(b64: str, max_kb: int = 900) -> Optional[str]:
        """Zwraca base64 obrazu — skaluje w dół jeśli za duże (>max_kb KB)."""
        if not b64:
            return None
        if len(b64) * 3 // 4 > max_kb * 1024:
            try:
                import base64 as _b64
                from io import BytesIO
                raw = _b64.b64decode(b64)
                if not HAS_PIL:
                    return b64
                _PIL = __import__("PIL.Image", fromlist=["Image"])
                # Context manager zamyka oryginalny obraz nawet przy wyjątku;
                # resize() tworzy NOWY obraz, więc stary trzeba domknąć osobno
                # (wcześniej img.close() zamykał tylko przeskalowany → wyciek).
                with _PIL.open(BytesIO(raw)) as img:
                    w, h = img.size
                    pixel_bytes = w * h * 3
                    scale = min(1.0, ((max_kb * 1024) / max(pixel_bytes, 1)) ** 0.5)
                    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
                    resized = img.resize((nw, nh), _PIL.LANCZOS)
                try:
                    with BytesIO() as buf:
                        resized.save(buf, format="JPEG", quality=75)
                        return _b64.b64encode(buf.getvalue()).decode()
                finally:
                    resized.close()
            except Exception:
                return None
        return b64

    if has_images:
        img_a = _safe_img(page0.img_a_b64)
        img_b = _safe_img(page0.img_b_b64)
        img_d = _safe_img(page0.img_diff_b64, max_kb=600)
        content.append({"type": "text", "text": f"ARTWORK A: {result.file_a}"})
        if img_a:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_a}})
        content.append({"type": "text", "text": f"ARTWORK B: {result.file_b}"})
        if img_b:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b}})
        if img_d:
            content.append({"type": "text", "text": "MAPA RÓŻNIC (czerwone=duże, żółte=średnie, niebieskie=małe):"})
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_d}})

    content.append({
        "type": "text",
        "text": f"""Porównaj artwork A vs B. Dane z analizy automatycznej:

Format: A={result.dims_a or '?'} | B={result.dims_b or '?'}
Pixel diff: {result.total_pixel_diff:.1f}% | Stron: A={result.pages_a} B={result.pages_b}

WYKRYTE ZMIANY (OCR/regex):
{changed_fields}

POLA BEZ ZMIAN: {ok_fields}

{"Zbadaj wizualnie oba artworki i mapę różnic. " if has_images else "Brak obrazów — oceń na podstawie danych tekstowych. "}Wygeneruj szczegółowy raport JSON zgodnie z instrukcją systemową."""
    })

    content = [
        b for b in content
        if not (b.get("type") == "text" and not (b.get("text") or "").strip())
    ]

    payload = json.dumps({
        "model": _risk_model(),
        "max_tokens": 2000,
        "system": _AI_SYSTEM,
        "messages": [{"role": "user", "content": content}],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=payload,
        headers={"Content-Type": "application/json", "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"}, method="POST")
    try:
        # Bandit B310: URL stały w kodzie (https://api.anthropic.com), schemat nie pochodzi z wejścia.
        with urllib.request.urlopen(req, timeout=40) as resp:  # nosec B310
            data = json.loads(resp.read().decode("utf-8"))
        try:
            from api_usage_tracker import record_usage
            record_usage(_risk_model(), data.get("usage", {}), call_type="artwork_analysis")
        except Exception:
            pass
        _content = data.get("content") or []
        if not _content:
            return {}
        text = (_content[0].get("text") or "").strip()
        text = re.sub(r"^```(?:json)?\s*", "", text).strip().rstrip("`").strip()
        if not text.startswith("{"):
            m = re.search(r"\{[\s\S]+\}", text)
            if m:
                text = m.group()
        return json.loads(text)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        return {"error": f"HTTP {e.code}: {body or e.reason}"}
    except Exception as e:
        return {"error": str(e)[:200]}


def _claude_detect_diff_boxes(img_a: "Image.Image", img_b: "Image.Image") -> list:
    """Sends both images to Claude Vision to semantically detect differences.

    Returns regions in internal format (same as _compute_pixel_diff regions),
    with source='claude' so they can be styled distinctly.
    Only called when ANTHROPIC_API_KEY is set.
    """
    api_key = _get_api_key_artwork()
    if not api_key:
        return []
    try:
        import base64 as _b64, httpx as _hx
        from io import BytesIO

        def _to_b64(img, max_px=1400):
            w, h = img.size
            if max(w, h) > max_px:
                scale = max_px / max(w, h)
                img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            buf = BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=82)
            return _b64.b64encode(buf.getvalue()).decode()

        b64_a = _to_b64(img_a)
        b64_b = _to_b64(img_b)

        prompt = (
            "Compare ARTWORK A and ARTWORK B. Find ALL differences — text, numbers, "
            "codes, colors, missing/added elements.\n\n"
            "Return ONLY a JSON array. Each item:\n"
            '{"description":"what changed (e.g. REF: NL753-S-40 → NL753-S-42)",'
            '"severity":"critical|important|minor",'
            '"x1_pct":10.5,"y1_pct":20.3,"x2_pct":45.2,"y2_pct":35.1}\n\n'
            "Coordinates = percentage of image size (0-100), tightly wrapping the changed area.\n"
            "Return [] if artworks are identical. Return ONLY the JSON array."
        )

        resp = _hx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                     "Content-Type": "application/json"},
            json={
                "model": _current_ai_model(),
                "max_tokens": 1200,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": "ARTWORK A:"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64_a}},
                    {"type": "text", "text": "ARTWORK B:"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64_b}},
                    {"type": "text", "text": prompt},
                ]}],
            },
            timeout=45,
        )
        if resp.status_code != 200:
            try:
                _body = resp.text[:400]
            except Exception:
                _body = ""
            logger.warning("Claude diff-detect HTTP %s — model=%s body=%s",
                           resp.status_code, _current_ai_model(), _body)
            return []
        try:
            from api_usage_tracker import record_usage
            record_usage(_current_ai_model(), resp.json().get("usage", {}), call_type="claude_diff_detect")
        except Exception:
            pass
        _content = (resp.json().get("content") or [])
        if not _content:
            return []
        text = (_content[0].get("text") or "").strip()
        text = re.sub(r"^```(?:json)?\s*", "", text).strip().rstrip("`").strip()
        if not text.startswith("["):
            m = re.search(r"\[[\s\S]+\]", text)
            if m:
                text = m.group()
        raw = json.loads(text)
        if not isinstance(raw, list):
            return []

        sev_map = {"critical": "high", "important": "medium", "minor": "low"}
        _img_w, _img_h = img_a.size
        regions = []
        for item in raw:
            try:
                x1 = float(item["x1_pct"])
                y1 = float(item["y1_pct"])
                x2 = float(item["x2_pct"])
                y2 = float(item["y2_pct"])
                if x2 <= x1 or y2 <= y1 or x1 < 0 or y1 < 0 or x2 > 100 or y2 > 100:
                    continue
                # Compute actual pixel area from percentage bbox and image dimensions
                _px_w = int((x2 - x1) / 100 * _img_w)
                _px_h = int((y2 - y1) / 100 * _img_h)
                _area = max(1, _px_w * _px_h)
                regions.append({
                    "x_pct": x1, "y_pct": y1,
                    "w_pct": x2 - x1, "h_pct": y2 - y1,
                    "severity": sev_map.get(str(item.get("severity", "important")), "medium"),
                    "description": str(item.get("description", ""))[:200],
                    "source": "claude",
                    "avg_diff": 80.0,
                    "area": _area,
                })
            except (KeyError, TypeError, ValueError):
                continue
        logger.info("Claude Vision detected %d diff regions", len(regions))
        return regions
    except Exception as exc:
        logger.warning("Claude diff detection failed: %s", exc)
        return []


def _draw_claude_boxes(img_annot: "Image.Image", regions: list) -> "Image.Image":
    """Draw Claude-detected regions as orange boxes on an already-annotated image."""
    if not regions:
        return img_annot
    W, H = img_annot.size
    img = img_annot.convert("RGBA")
    draw = ImageDraw.Draw(img, "RGBA")
    for r in regions:
        x1 = int(r["x_pct"] / 100 * W)
        y1 = int(r["y_pct"] / 100 * H)
        x2 = int((r["x_pct"] + r["w_pct"]) / 100 * W)
        y2 = int((r["y_pct"] + r["h_pct"]) / 100 * H)
        # Orange fill + thick border — distinct from pixel diff red/yellow
        draw.rectangle([x1, y1, x2, y2], fill=(249, 115, 22, 70))
        for offset in range(3):
            draw.rectangle([x1 - offset, y1 - offset, x2 + offset, y2 + offset],
                           outline=(249, 115, 22, max(60, 230 - offset * 60)))
    # Return RGB — the result is later encoded as JPEG (_img_to_b64), which
    # cannot write an RGBA image and would raise OSError, failing the page diff.
    return img.convert("RGB")


def count_pdf_pages(path: str) -> int:
    """Returns number of pages in a PDF (0 for non-PDF or error)."""
    if not path.lower().endswith(".pdf") or not HAS_FITZ:
        return 0
    try:
        doc = fitz.open(path)
        try:
            return len(doc)
        finally:
            doc.close()
    except Exception:
        return 0


def extract_pdf_page(path: str, page_idx: int, out_path: str) -> str:
    """Extract a single page from a PDF into a new single-page PDF. Returns out_path."""
    if not HAS_FITZ:
        raise RuntimeError("Zainstaluj pymupdf: pip install pymupdf")
    doc = fitz.open(path)
    try:
        if not (0 <= page_idx < len(doc)):
            raise ValueError(f"Strona {page_idx} poza zakresem (plik ma {len(doc)} stron)")
        out = fitz.open()
        try:
            out.insert_pdf(doc, from_page=page_idx, to_page=page_idx)
            out.save(out_path)
        finally:
            out.close()
    finally:
        doc.close()
    return out_path




def compare_artworks(path_a: str, path_b: str,
                      use_ai: bool = False,
                      use_ai_sections: bool = True,
                      max_pages: int = 10,
                      page_a: int = None,
                      page_b: int = None,
                      profile_id: int = None,
                      field_overrides_b: dict = None,
                      profile_perf: bool = False,
                      progress_id: str = None) -> ArtworkCompareResult:
    """Porównuje dwa artworki opakowań. Zwraca ArtworkCompareResult.
    page_a / page_b: 0-based page index to extract from multi-page PDFs.
    profile_id: gdy podane, ładuje profil szablonowy z BD i ogranicza raport do zmapowanych pól.
    profile_perf: gdy True (lub ARTWORK_PROFILE=1) zbiera szczegółowy rozkład czasów
      do result.performance (sekcja 'Wydajność' w raporcie).
    progress_id: gdy podane, raportuje bieżący etap do set_progress() — frontend
      odpytuje get_progress() i pokazuje realny status na pasku."""
    file_a = os.path.basename(path_a)
    file_b = os.path.basename(path_b)
    _t0 = time.time()
    set_progress(progress_id, 5, "Wczytywanie plików…")

    # Performance profiler — on by default (overhead is negligible: a few perf_counter
    # calls + a dict) so the report's "Wydajność" section is always available. Disable
    # with ARTWORK_PROFILE_OFF=1. (profile_perf / ARTWORK_PROFILE kept as explicit opt-in.)
    _prof_on = (profile_perf or _env_flag("ARTWORK_PROFILE", "0")
                or not _env_flag("ARTWORK_PROFILE_OFF", "0"))
    _prof = _PerfProfiler() if _prof_on else _NullProfiler()

    # File sizes in bytes
    _size_a = _size_b = 0
    try:
        _size_a = os.path.getsize(path_a)
        _size_b = os.path.getsize(path_b)
    except OSError:
        pass

    # Load explicit profile early (before rendering)
    _explicit_profile: Optional[dict] = None
    if profile_id:
        _explicit_profile = _load_artwork_profile_by_id(profile_id)
        if _explicit_profile:
            logger.info("Explicit profile loaded: '%s' (id=%d)", _explicit_profile["name"], profile_id)

    # Skip AI for files over 50 MB to avoid timeouts
    _AI_SIZE_LIMIT = 50 * 1024 * 1024
    try:
        _total_size = _size_a + _size_b
        if _total_size > _AI_SIZE_LIMIT:
            logger.warning("Pliki za duże (%d MB) — AI pominięte", _total_size // (1024*1024))
            use_ai = False
            use_ai_sections = False
    except OSError:
        pass

    dims_a = _get_dims_mm(path_a, page_idx=page_a)
    dims_b = _get_dims_mm(path_b, page_idx=page_b)

    # When an explicit profile with active fields is provided, skip heavy full-page
    # OCR and badge renders — only preview images (RENDER_DPI) are needed.
    _skip_ocr_loads = bool(
        _explicit_profile and
        any(not f.get("skip_analysis") for f in _explicit_profile.get("fields", []))
    )

    # Parallel page loading: render every DPI tier (preview / OCR / badge) for both
    # files concurrently to cut wall-clock render time. OCR_DPI (300) is a genuine
    # render distinct from RENDER_DPI (150) — submit it to the pool too rather than
    # rendering it serially after the preview pages are ready.
    set_progress(progress_id, 12, "Renderowanie stron PDF…")
    with _prof.stage("render_pages"), ThreadPoolExecutor(max_workers=6) as _pool:
        _fut_at = _pool.submit(_load_pages, path_a, RENDER_DPI, page_a)
        _fut_bt = _pool.submit(_load_pages, path_b, RENDER_DPI, page_b)
        if not _skip_ocr_loads:
            _fut_ab = _pool.submit(_load_pages, path_a, BADGE_DPI, page_a)
            _fut_bb = _pool.submit(_load_pages, path_b, BADGE_DPI, page_b)
            _fut_ao = _pool.submit(_load_pages, path_a, OCR_DPI, page_a)
            _fut_bo = _pool.submit(_load_pages, path_b, OCR_DPI, page_b)
        try:
            pages_a_t = _fut_at.result(timeout=120)[:max_pages]
            pages_b_t = _fut_bt.result(timeout=120)[:max_pages]
            if _skip_ocr_loads:
                pages_a_o = pages_b_o = []
                pages_a_b = pages_b_b = []
            else:
                pages_a_o = _fut_ao.result(timeout=120)[:max_pages]
                pages_b_o = _fut_bo.result(timeout=120)[:max_pages]
                pages_a_b = _fut_ab.result(timeout=120)[:1]
                pages_b_b = _fut_bb.result(timeout=120)[:1]
        except FuturesTimeoutError:
            # Czytelny komunikat zamiast surowego TimeoutError z puli wątków.
            raise RuntimeError("Renderowanie PDF przekroczyło limit czasu (120 s) — "
                               "plik może być zbyt duży lub uszkodzony.")

    n_a, n_b = len(pages_a_t), len(pages_b_t)

    def _page_texts(path, page_idx, max_p):
        if not path.lower().endswith(".pdf"):
            return []
        all_texts = _extract_text_from_pdf(path)
        if page_idx is not None and 0 <= page_idx < len(all_texts):
            return [all_texts[page_idx]]
        return all_texts[:max_p]

    if _skip_ocr_loads:
        texts_a = [""] * n_a
        texts_b = [""] * n_b
        badges_a = badges_b = []
    else:
        texts_a = _page_texts(path_a, page_a, max_pages)
        texts_b = _page_texts(path_b, page_b, max_pages)
        while len(texts_a) < n_a: texts_a.append("")
        while len(texts_b) < n_b: texts_b.append("")

        # OCR: artworki mają tekst w warstwie graficznej (niewidoczny dla PDF parser)
        # Run OCR for all pages of A and B in parallel to avoid sequential Tesseract waits
        set_progress(progress_id, 32, "OCR — odczyt tekstu…")
        with _prof.stage("ocr_fullpage"), ThreadPoolExecutor(max_workers=4) as _ocr_pool:
            _ocr_futs_a = [
                _ocr_pool.submit(_extract_text_ocr, pages_a_o[i])
                for i in range(n_a) if i < len(pages_a_o)
            ]
            _ocr_futs_b = [
                _ocr_pool.submit(_extract_text_ocr, pages_b_o[i])
                for i in range(n_b) if i < len(pages_b_o)
            ]
            _ocr_results_a = [f.result(timeout=60) for f in _ocr_futs_a]
            _ocr_results_b = [f.result(timeout=60) for f in _ocr_futs_b]

        for i in range(n_a):
            if i < len(_ocr_results_a):
                ocr_text = _ocr_results_a[i]
                existing_lines = set(l.strip().lower() for l in texts_a[i].splitlines() if l.strip())
                extra = "\n".join(
                    l for l in ocr_text.splitlines()
                    if l.strip() and l.strip().lower() not in existing_lines
                )
                if extra:
                    texts_a[i] = texts_a[i] + "\n" + extra
        for i in range(n_b):
            if i < len(_ocr_results_b):
                ocr_text = _ocr_results_b[i]
                existing_lines = set(l.strip().lower() for l in texts_b[i].splitlines() if l.strip())
                extra = "\n".join(
                    l for l in ocr_text.splitlines()
                    if l.strip() and l.strip().lower() not in existing_lines
                )
                if extra:
                    texts_b[i] = texts_b[i] + "\n" + extra

        badges_a = _ocr_yellow_badges(pages_a_b[0]) if pages_a_b else []
        badges_b = _ocr_yellow_badges(pages_b_b[0]) if pages_b_b else []

    full_a = "\n".join(texts_a)
    full_b = "\n".join(texts_b)
    ocr_failed = len(full_a.strip()) < 10 and len(full_b.strip()) < 10

    result = ArtworkCompareResult(
        file_a=file_a, file_b=file_b, pages_a=n_a, pages_b=n_b,
        dims_a=f"{dims_a[0]}×{dims_a[1]} mm" if dims_a[0] else "",
        dims_b=f"{dims_b[0]}×{dims_b[1]} mm" if dims_b[0] else "",
        ocr_failed=ocr_failed,
        file_size_a=_size_a,
        file_size_b=_size_b,
    )

    try:
        def _md5_chunked(path):
            h = hashlib.md5(usedforsecurity=False)
            with open(path, "rb") as _f:
                for _chunk in iter(lambda: _f.read(65536), b""):
                    h.update(_chunk)
            return h.hexdigest()
        ha = _md5_chunked(path_a)
        hb = _md5_chunked(path_b)
        if ha == hb:
            result.identical = True; result.risk_level = "ok"
            result.summary = "Pliki są identyczne."
            return result
    except Exception:
        pass

    # Determine if profile mode is active (explicit or auto-detected)
    # In profile mode we skip full OCR/zone analysis and show only mapped fields
    _profile: Optional[dict] = _explicit_profile

    # Compute colorimetry from first rendered page (quick — uses already-loaded images)
    with _prof.stage("colorimetry"):
        if pages_a_t:
            result.colorimetry["a"] = _get_colorimetry(pages_a_t[0])
        if pages_b_t:
            result.colorimetry["b"] = _get_colorimetry(pages_b_t[0])
        if result.colorimetry.get("a") and result.colorimetry.get("b"):
            result.colorimetry["global"] = _global_color_compare(
                result.colorimetry["a"], result.colorimetry["b"])

    # Count active mapped fields in explicit profile (non-skip)
    if _profile:
        _active_fields = [f for f in _profile.get("fields", []) if not f.get("skip_analysis")]
        _profile_has_fields = len(_active_fields) > 0
    else:
        _active_fields = []
        _profile_has_fields = False

    if _profile_has_fields:
        # ── Profile mode: only mapped bbox fields ──────────────────────────────
        # Skip expensive full-page OCR analysis and zone detection
        result.profile_active = True
        result.profile_name = _profile["name"]

        set_progress(progress_id, 50, "Porównywanie pól szablonu…")
        with _prof.stage("field_compare"):
            _template_rows = _apply_profile_comparison(path_a, path_b, _profile, page_a=page_a, page_b=page_b,
                                                        field_overrides_b=field_overrides_b, profiler=_prof)
        if _template_rows:
            result.field_report = _template_rows
            result.mapped_field_count = len([r for r in _template_rows if not r.get("skipped")])
        _zone_regions = []
    else:
        # ── Standard mode: full auto analysis ─────────────────────────────────
        set_progress(progress_id, 50, "Porównywanie treści i pól…")
        with _prof.stage("field_compare"):
            result.field_report = _build_field_report(full_a, full_b, badges_a, badges_b, dims_a, dims_b, use_ai=use_ai,
                                                       file_a=path_a, file_b=path_b)

        # ── Analiza strefowa (automatyczna, bez szablonów) ──────────────────────────
        _zone_regions = []
        if pages_a_o and pages_b_o:
            try:
                with _prof.stage("zones"):
                    _zone_rows, _zone_regions = _zone_ocr_compare(
                        pages_a_o[0], pages_b_o[0],
                        path_a=path_a, path_b=path_b
                    )
                if _zone_rows:
                    result.field_report.extend(_zone_rows)
            except Exception as _ze:
                logger.warning("Zone comparison failed: %s", _ze)

        # ── Auto-detect profile po EAN/REF (gdy nie podano explicite) ──────────
        _ean_val = next((f["val_a"] for f in result.field_report if f["field"] == "EAN / GTIN" and f["val_a"] != "N/D"), "")
        _ref_val = next((f["val_a"] for f in result.field_report if f["field"] == "REF / Nr katalogowy" and f["val_a"] != "N/D"), "")
        _auto_profile = _load_artwork_profile(ean=_ean_val, ref=_ref_val, dims_a=dims_a)
        if _auto_profile:
            _profile = _auto_profile
            logger.info("Artwork profile matched: '%s' (%d fields)", _profile["name"], len(_profile["fields"]))
            with _prof.stage("field_compare"):
                _template_rows = _apply_profile_comparison(path_a, path_b, _profile, page_a=page_a, page_b=page_b,
                                                           profiler=_prof)
            if _template_rows:
                result.field_report.extend(_template_rows)

    try:
        from barcode_validator import validate_artwork_barcodes
        _bc_pages_a = pages_a_o[:1] if pages_a_o else (pages_a_t[:1] if pages_a_t else [])
        _bc_pages_b = pages_b_o[:1] if pages_b_o else (pages_b_t[:1] if pages_b_t else [])
        set_progress(progress_id, 64, "Weryfikacja kodów kreskowych…")
        with _prof.stage("barcode"):
            result.barcode_report = validate_artwork_barcodes(
                full_a, full_b, _bc_pages_a, _bc_pages_b,
            )
    except Exception:
        result.barcode_report = {}

    # Detekcja i porównanie ikon/piktogramów — na stronie 0 obu etykiet (pomijane w profile mode)
    if pages_a_t and pages_b_t and not result.profile_active:
        try:
            with _prof.stage("icons"):
                result.icon_comparison = compare_icon_regions(
                    pages_a_t[0], pages_b_t[0], RENDER_DPI,
                    hires_a=pages_a_o[0] if pages_a_o else None,
                    hires_b=pages_b_o[0] if pages_b_o else None,
                )
        except Exception:
            result.icon_comparison = []

    # AI section decomposition — only in standard mode (profile mode skips text analysis)
    _ai_sec_diffs: Optional[list] = None
    if use_ai_sections and not ocr_failed and not result.profile_active:
        with _prof.stage("ai_sections"):
            _ai_sec_diffs = _compare_sections_ai(full_a, full_b)

    set_progress(progress_id, 82, "Analiza wizualna (pixel diff)…")
    pixel_diffs = []
    for i in range(max(n_a, n_b)):
        img_a_t = pages_a_t[i] if i < n_a else None
        img_b_t = pages_b_t[i] if i < n_b else None
        text_a  = texts_a[i] if i < len(texts_a) else ""
        text_b  = texts_b[i] if i < len(texts_b) else ""

        diff_pct = 0.0
        regions  = []
        img_a_b64 = img_b_b64 = img_a_annot_b64 = img_b_annot_b64 = img_diff_b64 = None
        page_size_mismatch = False

        img_a_hires_b64 = img_b_hires_b64 = None
        img_a_annot_hires_b64 = img_b_annot_hires_b64 = None

        if img_a_t and img_b_t:
            try:
                wa, ha = img_a_t.size
                wb, hb = img_b_t.size
                # Detect significant dimension mismatch (>8%) — pixel diff would be misleading
                page_size_mismatch = (
                    max(wa, wb) / max(min(wa, wb), 1) > 1.08 or
                    max(ha, hb) / max(min(ha, hb), 1) > 1.08
                )
                img_a_b64 = _img_to_b64(img_a_t)        # thumb for reports
                img_b_b64 = _img_to_b64(img_b_t)
                img_a_hires_b64 = _img_to_b64(img_a_t, max_width=HIRES_W)  # hires for viewer
                img_b_hires_b64 = _img_to_b64(img_b_t, max_width=HIRES_W)
                if result.profile_active or page_size_mismatch:
                    # Profile mode: no pixel diff — just show clean images
                    img_a_annot_b64 = img_a_b64
                    img_b_annot_b64 = img_b_b64
                    img_a_annot_hires_b64 = img_a_hires_b64
                    img_b_annot_hires_b64 = img_b_hires_b64
                else:
                    _t_px = time.perf_counter()
                    diff_pct, img_a_annot, img_b_annot, img_diff, regions = \
                        _compute_pixel_diff(img_a_t, img_b_t)
                    _px_ms = (time.perf_counter() - _t_px) * 1000.0
                    _prof.add("pixel_diff", _px_ms)
                    _prof.page({"page": i + 1, "ms": round(_px_ms, 1),
                                "diff_pct": round(diff_pct, 2),
                                "regions": len(regions)})
                    pixel_diffs.append(diff_pct)

                    # Claude Vision semantic detection — finds differences pixel diff misses
                    if use_ai and img_a_t and img_b_t:
                        try:
                            claude_regions = _claude_detect_diff_boxes(img_a_t, img_b_t)
                            if claude_regions:
                                img_a_annot = _draw_claude_boxes(img_a_annot, claude_regions)
                                img_b_annot = _draw_claude_boxes(img_b_annot, claude_regions)
                                regions = regions + claude_regions
                        except Exception as _cde:
                            logger.warning("Claude diff detection skipped: %s", _cde)

                    img_a_annot_b64 = _img_to_b64(img_a_annot)
                    img_b_annot_b64 = _img_to_b64(img_b_annot)
                    img_a_annot_hires_b64 = _img_to_b64(img_a_annot, max_width=HIRES_W)
                    img_b_annot_hires_b64 = _img_to_b64(img_b_annot, max_width=HIRES_W)
                    img_diff_b64    = _img_to_b64(img_diff)
            except Exception as _px_exc:
                logger.warning("Pixel diff failed on page %d: %s", i, _px_exc)
        elif img_a_t:
            img_a_b64 = _img_to_b64(img_a_t)
            img_a_hires_b64 = _img_to_b64(img_a_t, max_width=HIRES_W)
        elif img_b_t:
            img_b_b64 = _img_to_b64(img_b_t)
            img_b_hires_b64 = _img_to_b64(img_b_t, max_width=HIRES_W)

        if result.profile_active:
            text_diffs = []
            sec_diffs = []
        else:
            text_diffs = _compare_texts(text_a, text_b)
            if i == 0:
                sec_diffs = _ai_sec_diffs if _ai_sec_diffs is not None else _compare_sections(text_a, text_b)
            else:
                sec_diffs = []
        field_diffs  = [f for f in result.field_report if f["changed"]] if i == 0 else []
        has_crit  = (any(d["severity"]=="error"   for d in text_diffs)
                     or any(f["severity"]=="critical" and f["changed"] for f in result.field_report))
        has_imp   = (any(d["severity"]=="warning" for d in text_diffs)
                     or any(f["severity"]=="important" and f["changed"] for f in result.field_report))

        parts = []
        if page_size_mismatch:
            parts.append(f"RÓŻNE FORMATY — pixel diff pominięty ({result.dims_a} vs {result.dims_b})")
        elif diff_pct > 20:
            parts.append(f"Duże różnice wizualne ({diff_pct:.0f}%)")
        elif diff_pct > 5:
            parts.append(f"Różnice wizualne ({diff_pct:.0f}%)")
        n_c = sum(1 for d in text_diffs if d["severity"]=="error")
        n_i = sum(1 for d in text_diffs if d["severity"]=="warning")
        if n_c: parts.append(f"{n_c} krytycznych zmian")
        if n_i: parts.append(f"{n_i} ważnych zmian")

        result.page_diffs.append(PageDiff(
            page_num=i+1, pixel_diff_pct=diff_pct,
            diff_regions=regions, text_diffs=text_diffs, field_diffs=field_diffs,
            img_a_b64=img_a_b64, img_b_b64=img_b_b64,
            img_a_annot_b64=img_a_annot_b64, img_b_annot_b64=img_b_annot_b64,
            img_diff_b64=img_diff_b64,
            img_a_hires_b64=img_a_hires_b64, img_b_hires_b64=img_b_hires_b64,
            img_a_annot_hires_b64=img_a_annot_hires_b64,
            img_b_annot_hires_b64=img_b_annot_hires_b64,
            has_critical=has_crit, has_important=has_imp,
            size_mismatch=page_size_mismatch,
            summary=" | ".join(parts) if parts else "Brak istotnych różnic",
            section_diffs=sec_diffs,
        ))

    result.total_pixel_diff = sum(pixel_diffs)/len(pixel_diffs) if pixel_diffs else 0.0

    # Nałóż strefy z różnicami na obraz strony 0 jako kolorowe ramki
    if _zone_regions and result.page_diffs:
        _pd0 = result.page_diffs[0]
        _sev_map = {"critical": "high", "important": "medium", "info": "low"}
        for _zr in _zone_regions:
            _pd0.diff_regions.append({
                "x_pct": 0, "y_pct": _zr["y1_pct"],
                "w_pct": 100, "h_pct": _zr["y2_pct"] - _zr["y1_pct"],
                "severity": _sev_map.get(_zr["severity"], "medium"),
                "avg_diff": 100 - _zr["pix_sim"],
                "area": 0,
                "label": _zr["name"],
            })

    # Dedykowana analiza tabeli rozmiarów (Vision) — uruchamiana gdy AI aktywne i są obrazy,
    # ALE tylko gdy:
    #   1. profil bbox NIE pokrywa już tabeli rozmiarów (size_label), ORAZ
    #   2. nie ma aktywnego profilu bbox (_profile) — profil definiuje co porównywać,
    #      Vision size halucynuje gdy tabela jest nieczytelna lub produkt nie ma tabeli rozmiarów.
    _bbox_covers_sizes = any(
        r.get("field", "").startswith("[Rozmiar ")
        for r in result.field_report
    )
    if result.page_diffs and not _bbox_covers_sizes and not _profile:
      with _prof.stage("size_table"):
        if use_ai:
            _p0 = result.page_diffs[0]
            _size_rows = _compare_size_table_vision(
                _p0.img_a_hires_b64 or _p0.img_a_b64,
                _p0.img_b_hires_b64 or _p0.img_b_b64,
            )
            if _size_rows:
                result.field_report.extend(_size_rows)
        else:
            # Table Transformer fallback when AI is disabled
            _img_a_src = pages_a_o[0] if pages_a_o else (pages_a_t[0] if pages_a_t else None)
            _img_b_src = pages_b_o[0] if pages_b_o else (pages_b_t[0] if pages_b_t else None)
            if _img_a_src and _img_b_src:
                try:
                    _tt_rows = _compare_size_tables_transformer(_img_a_src, _img_b_src)
                    if _tt_rows:
                        result.field_report.extend(_tt_rows)
                except Exception as _tte:
                    logger.debug("Table Transformer compare failed: %s", _tte)

    # field_report counts
    result.critical_count  = sum(1 for f in result.field_report if f["severity"] == "critical"  and f["changed"])
    result.important_count = sum(1 for f in result.field_report if f["severity"] == "important" and f["changed"])
    result.info_count      = sum(1 for f in result.field_report if f["severity"] == "info"      and f["changed"])
    result.ok_count        = sum(1 for f in result.field_report if not f["changed"])

    # Icon comparison counts — ikony z innym układem / brakujące MUSZĄ wpłynąć na risk score
    for _ic in result.icon_comparison:
        _st = _ic.get("status", "")
        if _st in ("Blad", "Brak w A", "Brak w B"):
            result.critical_count += 1
        elif _st in ("Inny układ", "Roznica"):
            result.important_count += 1

    # Map field-count severity → overall risk_level scale (ok/warning/error/critical).
    # Must stay identical to the mapping in field_queue_finalize (~6389):
    #   critical change → critical, important change → error, info change → warning.
    result.risk_level = (
        "critical" if result.critical_count > 0 else
        "error"    if result.important_count > 0 else
        "warning"  if result.info_count > 0      else "ok"
    )
    result.summary = (
        f"Wykryto {result.critical_count} krytycznych i {result.important_count} ważnych zmian. "
        f"Pixel diff: {result.total_pixel_diff:.1f}%."
    )
    if use_ai:
        set_progress(progress_id, 92, "Ocena ryzyka (AI)…")
        with _prof.stage("ai_report"):
            result.ai_report = _ai_analyze(result)

    # Finalise the performance breakdown (no-op profiler returns {"enabled": False})
    _prof.set_meta(
        file_a=file_a, file_b=file_b,
        pages_a=n_a, pages_b=n_b,
        render_dpi=RENDER_DPI, ocr_dpi=OCR_DPI,
        profile_active=result.profile_active,
        profile_name=result.profile_name,
        mapped_field_count=result.mapped_field_count,
        use_ai=bool(use_ai),
        align_enabled=_ALIGN_ENABLED,
        adaptive_thresh=_ADAPTIVE_THRESH,
        ocr_local_engines=_OCR_LOCAL_ENGINES,
        file_size_a=_size_a, file_size_b=_size_b,
    )
    result.performance = _prof.to_dict()

    set_progress(progress_id, 95, "Finalizacja analizy…")
    logger.info("Artwork compare %s vs %s: %.1fs", file_a, file_b, time.time() - _t0)
    return result
