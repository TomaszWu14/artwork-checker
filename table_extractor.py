"""
table_extractor.py — silnik porównania tabelarycznego PDF.
Obsługuje PO (Purchase Order), PI (Proforma Invoice), WZ, CMR.
REF może być w osobnej kolumnie lub jako pierwsza linia komórki z opisem.
"""

import re
import logging
from dataclasses import dataclass, field
from typing import Optional
import pdfplumber

logger = logging.getLogger(__name__)
from rapidfuzz import fuzz, process
from normalizer import normalize_number as _norm_number
from normalizer import normalize_date, payment_terms_equal, dates_possibly_equal

# ─────────────────────────────────────────────────────────────────────────────
# MODULE-LEVEL CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

# FIX 16: named constant for header-detection row scan limit (was magic number 8)
_HEADER_SCAN_ROWS = 10

# ─────────────────────────────────────────────────────────────────────────────
# WYBÓR SILNIKA OCR (do testów A/B z poziomu porównywarki)
# ─────────────────────────────────────────────────────────────────────────────
# Domyślnie 'cascade' — pełna kaskada (Camelot→Claude→Mistral→img2table→Tesseract).
# Można wymusić pojedynczy silnik: 'camelot','claude','mistral','img2table',
# 'tesseract','glm'. Ustawiane per-żądanie przez set_ocr_engine() (contextvar →
# propaguje do zadań liczonych inline; w workerze RQ ustawiane na starcie zadania).
import contextvars as _contextvars

OCR_ENGINES = ("cascade", "camelot", "claude", "mistral", "img2table", "tesseract", "glm")
_OCR_ENGINE = _contextvars.ContextVar("ocr_engine", default="cascade")


def set_ocr_engine(name):
    """Ustaw wymuszony silnik OCR dla bieżącego kontekstu (porównania)."""
    n = (name or "cascade").strip().lower()
    _OCR_ENGINE.set(n if n in OCR_ENGINES else "cascade")


def get_ocr_engine() -> str:
    return _OCR_ENGINE.get()


def _ocr_engine_forced() -> bool:
    """Czy wybrano konkretny silnik (≠ pełna kaskada)?"""
    return get_ocr_engine() not in ("cascade", "auto", "")


# ─────────────────────────────────────────────────────────────────────────────
# STRUKTURY
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class HeaderField:
    key: str
    val_a: Optional[str]
    val_b: Optional[str]
    status: str
    severity: str
    comment: str = ""

@dataclass
class ItemRow:
    ref: str
    desc_a: Optional[str]
    desc_b: Optional[str]
    qty_a: Optional[str]
    qty_b: Optional[str]
    price_a: Optional[str]
    price_b: Optional[str]
    net_a: Optional[str]
    net_b: Optional[str]
    lot_b: Optional[str]
    status: str = "ok"
    issues: list = field(default_factory=list)

@dataclass
class TableCompareResult:
    doc_type_a: str
    doc_type_b: str
    headers: list
    items: list
    total_a: Optional[str]
    total_b: Optional[str]
    total_qty_a: Optional[str]
    total_qty_b: Optional[str]
    ok_count: int = 0
    format_count: int = 0
    warn_count: int = 0
    diff_count: int = 0
    risk_level: str = "ok"
    summary: str = ""

    def to_dict(self):
        def _clean_total(v):
            if not v: return v
            # Użyj kanonicznego normalizera (obsługuje 13.500,00 EU i 13,500.00 EN);
            # naiwne .replace(',','') psuło sumy europejskie. Fallback do surowej
            # postaci (bez $) gdy normalize_number nie rozpozna liczby.
            _nv = _norm_number(str(v))
            if _nv is not None:
                return str(_nv)
            return str(v).strip().lstrip('$').strip()

        def _hdict(h):
            d = vars(h).copy()
            # None → pusty string dla val_a / val_b
            d['val_a'] = d.get('val_a') or ''
            d['val_b'] = d.get('val_b') or ''
            return d

        return {
            "doc_type_a": self.doc_type_a, "doc_type_b": self.doc_type_b,
            "headers": [_hdict(h) for h in self.headers],
            "items": [_idict(i) for i in self.items],
            "total_a": _clean_total(self.total_a),
            "total_b": _clean_total(self.total_b),
            "total_qty_a": self.total_qty_a, "total_qty_b": self.total_qty_b,
            "ok_count": self.ok_count, "format_count": self.format_count,
            "warn_count": self.warn_count, "diff_count": self.diff_count,
            "risk_level": self.risk_level, "summary": self.summary,
        }

def _idict(i: object) -> dict:
    return vars(i)

# ─────────────────────────────────────────────────────────────────────────────
# LICZBY
# ─────────────────────────────────────────────────────────────────────────────

def parse_num(text: Optional[str]) -> Optional[float]:
    if not text: return None
    t = str(text).upper()
    t = t.replace("\u00a0","").replace(" ","").strip()
    if not t or t in ["-","—","","N/A","NONE"]: return None
    # Notacja naukowa (1E5, 1.5E3) MUSI iść przed strip-em liter — inaczej regex
    # niżej zjadałby 'E' (1E5→15). Deleguj wprost do normalizera, który ją obsługuje.
    if re.fullmatch(r'[-+]?\d+\.?\d*E[-+]?\d+', t):
        val = _norm_number(t)
        return round(float(val), 6) if val is not None else None
    # Usuń litery (waluty/jednostki: USD/PLN/PCS/SZT/X…) jednym przejściem, unicode-
    # aware. Wcześniejszy replace podciągów ('X','PC') psuł wartości: 'PCS'→'S' itd.
    t = re.sub(r'[^\W\d_]+', '', t)
    if not t or t in ["-","—",""]: return None
    # Delegate to the canonical normalizer for app-wide invariants
    # (1.500=1500, "5%"→None, European thousands).
    val = _norm_number(t)
    return round(float(val), 6) if val is not None else None

# ─────────────────────────────────────────────────────────────────────────────
# DETEKCJA TYPU
# ─────────────────────────────────────────────────────────────────────────────

def detect_doc_type(text: str) -> str:
    tl = text.lower()
    if re.search(r"proforma invoice|pro-forma", tl): return "Proforma Invoice"
    if re.search(r"purchase order", tl): return "Purchase Order"
    if re.search(r"faktura vat", tl): return "Faktura VAT"
    if re.search(r"\binvoice\b", tl): return "Invoice"
    if re.search(r"wydanie zewn|\bwz\b", tl): return "WZ"
    if re.search(r"\bcmr\b|list przewozowy", tl): return "CMR"
    if re.search(r"packing list", tl): return "Packing List"
    return "Dokument"

# ─────────────────────────────────────────────────────────────────────────────
# ROZPOZNANIE KOLUMN
# ─────────────────────────────────────────────────────────────────────────────

def identify_columns(header_row: list[str]) -> dict[str, int]:
    """
    Zwraca {col_type: col_index} dla wiersza nagłówkowego.
    Obsługuje wielojęzyczne nagłówki, kombinowane komórki, wieloliniowe nagłówki.
    """
    ci = {}
    # Normalizuj nagłówki: strip whitespace, lowercase, zamień newline na spację
    header_str = [re.sub(r'\s+', ' ', str(h or "").lower()).strip() for h in header_row]

    rules = [
        # (col_type, [słowa kluczowe], priorytet)
        ("ref", [
            "product code", "ref / desc", "ref/desc", "ref. / desc", "ref/description",
            "code", "sku", "article no", "article no.", "art. no", "art no",
            "item no", "item no.", "item number", "nr ref", "indeks", "kod art",
            "catalog no", "cat no", "cat. no", "catalogue no", "part no", "part number",
            "material", "material no", "numer art", "numer artykułu",
            "product ref", "ref.", "reference", "reference no",
        ], 10),
        ("qty", [
            "order qty", "quantity/unit", "quantity", "qty", "ilość", "ilosc",
            "menge", "qty(pcs)", "pcs", "pieces", "count", "liczba",
            "ordered qty", "shipped qty", "shipped quantity", "q-ty",
            "amount qty", "total qty", "quantity (pcs)",
        ], 8),
        ("price", [
            "unit-price", "unit price", "price per", "cena jedn", "preis",
            "price", "unit price (usd)", "unit price (eur)", "uprice",
            "unit cost", "cena", "price per unit", "rate",
            "unit value", "cena jednostkowa",
        ], 8),
        ("net", [
            "net value", "net amount", "wartość netto", "amount (usd)", "amount(usd)",
            "amount", "total amount", "total value", "total net", "netto",
            "line total", "line amount", "extended", "extension", "total price",
            "value", "wartość",
        ], 8),
        ("lot", [
            "lot number", "lot numbers", "lot no", "lot no.", "lot", "batch",
            "batch no", "batch number", "seria", "numer serii", "nr serii",
            "lote", "charge", "chargennummer",
        ], 8),
        ("desc", [
            "description of goods", "description", "opis", "nazwa", "goods",
            "item description", "goods description", "product description",
            "commodity", "commodity description", "details",
            "name of goods", "product name", "towar",
        ], 8),
        ("unit", [
            "order unit", "base unit", "unit", "jm", "uom", "j.m.", "um",
            "unit of measure", "jednostka",
        ], 4),
        ("no", [
            "no", "lp", "l.p.", "s.no", "sno", "no.", "nr", "pos",
            "position", "pozycja", "pos.", "item",
        ], 4),
        ("exp", [
            "expiry", "exp date", "expiry date", "exp.", "exp", "use by",
            "ważność", "data ważności",
        ], 6),
    ]

    assigned = {}
    assigned_scores = {}
    for idx, h in enumerate(header_str):
        if not h:
            continue
        best_score, best_type = 0, None
        for ctype, keywords, priority in rules:
            for kw in keywords:
                kw_c = kw.strip()
                # Exact match
                if h == kw_c:
                    score = len(kw_c) * priority * 2
                    if score > best_score:
                        best_score, best_type = score, ctype
                # Contains match
                elif kw_c in h:
                    score = len(kw_c) * priority
                    if score > best_score:
                        best_score, best_type = score, ctype
        if best_type:
            prev_score = assigned_scores.get(best_type, 0)
            if best_type not in assigned or best_score > prev_score:
                assigned[best_type] = idx
                assigned_scores[best_type] = best_score

    # Fuzzy fallback: jeśli nadal brak kluczowych kolumn, szukaj fuzzy match
    if "ref" not in assigned or "qty" not in assigned or "price" not in assigned:
        FUZZY_TARGETS = {
            "ref":   ["product code", "reference", "article", "item no"],
            "qty":   ["quantity", "ilość", "pcs", "pieces"],
            "price": ["unit price", "price", "cena"],
            "net":   ["net value", "amount", "total"],
        }
        for ctype, targets in FUZZY_TARGETS.items():
            if ctype in assigned:
                continue
            for idx, h in enumerate(header_str):
                if not h or idx in assigned.values():
                    continue
                for target in targets:
                    score = fuzz.ratio(h, target)
                    if score >= 75:
                        assigned[ctype] = idx
                        break
                if ctype in assigned:
                    break

    # Specjalny przypadek: "REF / Description" → ref + desc w tej samej kolumnie
    for idx, h in enumerate(header_str):
        if ("ref" in h or "code" in h) and ("desc" in h or "description" in h or "name" in h):
            assigned["ref"] = idx
            assigned["_ref_desc_combined"] = idx
            break

    # Specjalny przypadek: "S.No" / "No." bez REF → użyj jako REF
    if "no" in assigned and "ref" not in assigned:
        assigned["ref"] = assigned["no"]
        assigned["_sno_format"] = True

    return assigned


# Wzorce wiersza „suma" — prekompilowane raz (is_total_row wołane per-rząd).
# \btotal(?![\w-]) unika dopasowania refów produktów typu 'TOTAL-CARE-SET'/'TOTALIZER'.
_TOTAL_ROW_PATTERNS = tuple(re.compile(p) for p in (
    r'\btotal(?![\w-])', r'\brazem\b', r'\bsuma\b', r'\błącznie\b',
    r'\bgrand\s+total', r'\btotal:',
))


def is_total_row(row: list[str]) -> bool:
    text = " ".join(str(c or "") for c in row).lower()
    return any(p.search(text) for p in _TOTAL_ROW_PATTERNS)

def is_ref_value(val: Optional[str]) -> bool:
    """Czy wartość wygląda jak kod referencyjny (nie nagłówek, nie opis)."""
    if not val or len(val) < 2 or len(val) > 60: return False
    v = val.strip().split("\n")[0].strip()
    # Odrzuć numery PO SAP (zaczynają się na 45 i mają 10 cyfr)
    if re.match(r'^45\d{8}$', v):
        return False
    # Odrzuć czysto numeryczne wartości > 8 cyfr (numery dokumentów, nie REF)
    if re.match(r'^\d{9,}$', v):
        return False
    # Standardowy kod REF: litery+cyfry
    if re.match(r'^[A-Za-z0-9][A-Za-z0-9\-_\.]{1,34}$', v) and v.upper() not in [
        "NO","REF","CODE","ITEM","LOT","QTY","PRICE","NET","TOTAL","USD","EUR"
    ]:
        return True
    # Format PI: "1.AT-NFA-S 1" lub "1.AT-NFFA-S 2 E"
    if re.match(r'^\d+\.(AT|NF|BL|CI|PO|PI)[A-Za-z0-9\-_\s\.]{2,40}$', v):
        return True
    return False

# ─────────────────────────────────────────────────────────────────────────────
# PARSOWANIE PDF
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DocData:
    doc_type: str
    items: list
    total_net: Optional[str]
    total_qty: Optional[str]
    header_fields: dict
    raw_text: str

def get_cell(row: list[str], ci: dict[str, int], ctype: str) -> Optional[str]:
    idx = ci.get(ctype)
    if idx is None or idx >= len(row): return None
    v = row[idx]
    if v is None: return None
    # FIX 15: join up to 3 non-empty lines instead of only taking the first line
    lines = [l.strip() for l in str(v).split('\n') if l.strip()]
    v = ' '.join(lines[:3])
    return v if v else None


def _apply_supplier_column_map(header_row: list, supplier_map: dict) -> Optional[dict]:
    """
    Próbuje dopasować kolumny z mapy dostawcy do nagłówka.
    supplier_map = {'ref': 'S.No', 'desc': 'Description', 'qty': 'Qty(pcs)', ...}
    Zwraca ci dict lub None jeśli brak dopasowania.
    """
    if not supplier_map:
        return None

    def normalize_hdr(s):
        """Normalizuj nagłówek: usuń whitespace/newlines, lowercase."""
        return re.sub(r'\s+', ' ', str(s or '').lower()).strip()

    header_norm = [normalize_hdr(h) for h in header_row]
    ci = {}

    for role, col_name in supplier_map.items():
        target = normalize_hdr(col_name)
        best_idx = None
        best_score = 0
        for idx, h in enumerate(header_norm):
            # Exact match
            if h == target:
                best_idx = idx
                best_score = 100
                break
            # Contains
            if target in h and len(target) > 3:
                score = len(target) / max(len(h), 1) * 90
                if score > best_score:
                    best_score = score
                    best_idx = idx
            elif h in target and len(h) > 3:
                score = len(h) / max(len(target), 1) * 70
                if score > best_score:
                    best_score = score
                    best_idx = idx
        if best_idx is not None and best_score > 40:
            ci[role] = best_idx

    # Dodaj flagę _ref_desc_combined jeśli ref wskazuje na kolumnę zawierającą "/"
    if 'ref' in ci:
        ref_header = normalize_hdr(header_row[ci['ref']])
        if '/' in ref_header and ('desc' in ref_header or 'ref' in ref_header):
            ci['_ref_desc_combined'] = ci['ref']
        # Flaga S.No format (prefiks numeryczny "1.AT-...")
        if 's.no' in ref_header or 'sno' in ref_header:
            ci['_sno_format'] = True

    # Sprawdź czy mamy przynajmniej ref + qty/net
    if 'ref' in ci and ('qty' in ci or 'net' in ci):
        return ci
    return None


def parse_pdf(path: str, supplier_column_map: dict = None) -> DocData:
    all_text = []
    all_items = []
    total_net = None
    total_qty = None

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            all_text.append(txt)
            tables = page.extract_tables()

            for table in tables:
                if not table or len(table) < 2: continue

                # Znajdź wiersz nagłówkowy (w pierwszych 8 wierszach)
                ci = {}
                header_idx = -1
                for i, row in enumerate(table[:_HEADER_SCAN_ROWS]):
                    if not row: continue
                    # 1. Najpierw próbuj mapę dostawcy
                    candidate = None
                    if supplier_column_map:
                        candidate = _apply_supplier_column_map(row, supplier_column_map)
                    # 2. Fallback: automatyczna detekcja
                    if not candidate:
                        candidate = identify_columns(row)
                    # Dobry nagłówek: ma ref + qty lub net
                    if ("ref" in candidate or "_ref_desc_combined" in candidate) and \
                       ("qty" in candidate or "net" in candidate):
                        ci = candidate
                        header_idx = i
                        break

                if header_idx < 0 or not ci: continue

                # Zbierz wiersze danych
                ref_col = ci.get("ref", 0)
                combined = "_ref_desc_combined" in ci
                sno_format = "_sno_format" in ci  # PI: "1.AT-NFA-S 1" format

                for row in table[header_idx+1:]:
                    if not row: continue
                    if is_total_row(row):
                        total_net = get_cell(row, ci, "net") or total_net
                        total_qty = get_cell(row, ci, "qty") or total_qty
                        continue

                    # Wyciągnij ref
                    ref_cell = str(row[ref_col] or "") if ref_col < len(row) else ""
                    ref_lines = ref_cell.strip().split("\n")
                    ref = ref_lines[0].strip()

                    # Obsłuż format PI: "1.AT-NFA-S 1" → "AT-NFA-S_1"
                    if sno_format and ref:
                        m = re.match(r'^\d+\.((?:AT|NF|BL|CI|PO|PI)[\w\-\s]+)', ref, re.IGNORECASE)
                        if m:
                            ref_raw = m.group(1).strip()
                            # Normalizuj: AT-NFA-S 1 → AT-NFA-S_1, AT-NFA-S 10 E → AT-NFA-S_10_E
                            ref_raw = re.sub(r'\s+', '_', ref_raw)
                            ref_raw = re.sub(r'-(\d+)-([A-Z])$', r'_\1_\2', ref_raw)
                            ref = ref_raw.upper()

                    if not is_ref_value(ref) and not (sno_format and re.match(r'^(?:AT|NF|BL|CI|PO|PI)', ref, re.IGNORECASE)): continue

                    # Opis
                    desc = None
                    if combined and len(ref_lines) > 1:
                        # Opis w drugiej linii tej samej komórki
                        desc = " ".join(ref_lines[1:]).strip()
                    else:
                        desc_raw = get_cell(row, ci, "desc")
                        if desc_raw:
                            desc = str(desc_raw or "").replace("\n"," ").strip()

                    # Normalizuj cenę — obsłuż format angielski i europejski
                    price_raw = get_cell(row, ci, "price") or ""
                    if price_raw:
                        _pv = _norm_number(price_raw.lstrip("$").strip())
                        price_clean = str(_pv) if _pv is not None else price_raw.lstrip("$").strip()
                    else:
                        price_clean = None
                    net_raw = get_cell(row, ci, "net") or ""
                    if net_raw:
                        _nv = _norm_number(net_raw.lstrip("$").strip())
                        net_clean = str(_nv) if _nv is not None else net_raw.lstrip("$").strip()
                    else:
                        net_clean = None

                    all_items.append({
                        "ref":   ref,
                        "desc":  desc,
                        "qty":   get_cell(row, ci, "qty"),
                        "price": price_clean,
                        "net":   net_clean,
                        "lot":   get_cell(row, ci, "lot"),
                        "unit":  get_cell(row, ci, "unit"),
                    })

    raw = "\n".join(all_text)

    # OCR fallback dla skanów. Przy WYMUSZONYM silniku (test A/B) uruchamiamy OCR
    # zawsze — także na cyfrowym PDF — i wynik wybranego silnika ma pierwszeństwo.
    if not all_items or _ocr_engine_forced():
        try:
            ocr_items, ocr_tnet, ocr_tqty = _doc_ocr_cascade(path, supplier_column_map)
            if ocr_items:
                all_items = ocr_items
                total_net = ocr_tnet or total_net
                total_qty = ocr_tqty or total_qty
        except Exception as _e:
            logger.warning("OCR cascade failed for %s: %s", path, str(_e)[:200])

    return DocData(
        doc_type=detect_doc_type(raw),
        items=all_items,
        total_net=total_net,
        total_qty=total_qty,
        header_fields=extract_header_fields(raw),
        raw_text=raw,
    )


# ─────────────────────────────────────────────────────────────────────────────
# OCR CASCADE — FALLBACK FOR SCANNED DOCUMENTS
# ─────────────────────────────────────────────────────────────────────────────

def _pdf_to_images(path: str, dpi: int = 220):
    try:
        import fitz
        doc = fitz.open(path)
        try:
            imgs = []
            for page in doc:
                mat = fitz.Matrix(dpi / 72, dpi / 72)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                from PIL import Image
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                imgs.append(img)
            return imgs
        finally:
            doc.close()
    except Exception:
        return []


def _camelot_extract(path: str) -> list:
    """Layers 1+2: Camelot lattice then stream."""
    try:
        import camelot
        for flavor in ("lattice", "stream"):
            try:
                tables = camelot.read_pdf(path, pages="all", flavor=flavor)
                if tables and len(tables) > 0:
                    rows = []
                    for tbl in tables:
                        df = tbl.df
                        for _, row in df.iterrows():
                            rows.append(list(row))
                    if rows:
                        return rows
            except Exception:
                continue
    except ImportError:
        pass
    return []


def _claude_extract_table(img) -> list:
    """Layer 3: Claude Vision → JSON list of row dicts."""
    import os, base64, io, json, httpx
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return []
    try:
        buf = io.BytesIO()
        w, h = img.size
        if max(w, h) > 2048:
            scale = 2048 / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)))
        img.save(buf, format="JPEG", quality=92)
        b64 = base64.b64encode(buf.getvalue()).decode()
        prompt = (
            "This is a trade document (Purchase Order, Proforma Invoice, Packing List, etc.). "
            "Extract ALL product/item rows from the table. "
            "Return ONLY a JSON array where each element has these fields (null if absent): "
            '{"ref":"product code","desc":"description","qty":"quantity",'
            '"price":"unit price","net":"net value","lot":"lot/batch","unit":"UOM"}. '
            'Also add a {"ref":"TOTAL","net":"...","qty":"..."} row for totals if present. '
            "Return ONLY the JSON array, no markdown, no explanation."
        )
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-4-6", "max_tokens": 4096, "messages": [
                {"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64",
                                                  "media_type": "image/jpeg", "data": b64}},
                    {"type": "text", "text": prompt},
                ]},
            ]},
            timeout=60,
        )
        if resp.status_code != 200:
            return []
        text = next((c.get("text", "") for c in (resp.json().get("content") or [])
                     if c.get("type") == "text"), "")
        m = re.search(r'\[[\s\S]*\]', text)
        if not m:
            return []
        rows = json.loads(m.group())
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _parse_markdown_table(markdown: str) -> list:
    rows = []
    for line in markdown.splitlines():
        line = line.strip()
        if not line.startswith('|') or not line.endswith('|'):
            continue
        if re.match(r'^\|[-| :]+\|$', line):
            continue
        cells = [c.strip() for c in line[1:-1].split('|')]
        if any(cells):
            rows.append(cells)
    return rows


def _mistral_extract_table(img) -> list:
    """Layer 4: Mistral OCR → markdown → row lists."""
    import os, base64, io, httpx
    api_key = os.environ.get("MISTRAL_API_KEY", "")
    if not api_key:
        return []
    try:
        buf = io.BytesIO()
        w, h = img.size
        if max(w, h) > 2048:
            scale = 2048 / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)))
        img.save(buf, format="JPEG", quality=92)
        b64 = base64.b64encode(buf.getvalue()).decode()
        resp = httpx.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": "mistral-ocr-latest", "messages": [
                {"role": "user", "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text":
                     "Extract all table rows from this document. Return as markdown table."},
                ]},
            ]},
            timeout=60,
        )
        if resp.status_code != 200:
            return []
        content = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
        return _parse_markdown_table(content)
    except Exception:
        return []


def _img2table_extract(img) -> list:
    """Layer 5: img2table cell detection."""
    try:
        from img2table.document import Image as I2TImage
        from img2table.ocr import TesseractOCR
        import io
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        buf.seek(0)
        ocr = TesseractOCR(lang="eng+pol")
        doc = I2TImage(src=buf)
        tables = doc.extract_tables(ocr=ocr, implicit_rows=True, borderless_tables=True)
        rows = []
        for tbl in (tables or []):
            for row in (tbl.content or {}).values():
                cells = [c.value or "" for c in (row or [])]
                if any(cells):
                    rows.append(cells)
        return rows
    except Exception:
        return []


def _tesseract_extract_table(img) -> list:
    """Layer 6: Tesseract PSM 6 + whitespace split."""
    try:
        import pytesseract
        text = pytesseract.image_to_string(img, lang="eng+pol", config="--psm 6")
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            cells = re.split(r'\t|  +', line)
            cells = [c.strip() for c in cells if c.strip()]
            if len(cells) >= 2:
                rows.append(cells)
        return rows
    except Exception:
        return []


def _rows_to_items(raw_rows: list, supplier_column_map: dict = None) -> tuple:
    """Convert raw row lists to (items, total_net, total_qty)."""
    if not raw_rows or len(raw_rows) < 2:
        return [], None, None
    ci = {}
    header_idx = -1
    for i, row in enumerate(raw_rows[:_HEADER_SCAN_ROWS]):
        if not row:
            continue
        candidate = None
        if supplier_column_map:
            candidate = _apply_supplier_column_map(row, supplier_column_map)
        if not candidate:
            candidate = identify_columns(row)
        if ("ref" in candidate or "_ref_desc_combined" in candidate) and \
           ("qty" in candidate or "net" in candidate):
            ci = candidate
            header_idx = i
            break
    if header_idx < 0 or not ci:
        return [], None, None

    all_items, total_net, total_qty = [], None, None
    ref_col = ci.get("ref", 0)
    combined = "_ref_desc_combined" in ci
    sno_format = "_sno_format" in ci

    for row in raw_rows[header_idx + 1:]:
        if not row:
            continue
        if is_total_row(row):
            total_net = get_cell(row, ci, "net") or total_net
            total_qty = get_cell(row, ci, "qty") or total_qty
            continue
        ref_cell = str(row[ref_col] or "") if ref_col < len(row) else ""
        ref_lines = ref_cell.strip().split("\n")
        ref = ref_lines[0].strip()
        if sno_format and ref:
            m = re.match(r'^\d+\.((?:AT|NF|BL|CI|PO|PI)[\w\-\s]+)', ref, re.IGNORECASE)
            if m:
                ref_raw = re.sub(r'\s+', '_', m.group(1).strip())
                ref_raw = re.sub(r'-(\d+)-([A-Z])$', r'_\1_\2', ref_raw)
                ref = ref_raw.upper()
        if not is_ref_value(ref) and not (sno_format and re.match(r'^(?:AT|NF|BL|CI|PO|PI)', ref, re.IGNORECASE)):
            continue
        desc = None
        if combined and len(ref_lines) > 1:
            desc = " ".join(ref_lines[1:]).strip()
        else:
            desc_raw = get_cell(row, ci, "desc")
            if desc_raw:
                desc = str(desc_raw or "").replace("\n", " ").strip()
        price_raw = get_cell(row, ci, "price") or ""
        _pv = _norm_number(price_raw.lstrip("$").strip()) if price_raw else None
        price_clean = str(_pv) if _pv is not None else (price_raw.lstrip("$").strip() or None)
        net_raw = get_cell(row, ci, "net") or ""
        _nv = _norm_number(net_raw.lstrip("$").strip()) if net_raw else None
        net_clean = str(_nv) if _nv is not None else (net_raw.lstrip("$").strip() or None)
        all_items.append({
            "ref": ref, "desc": desc,
            "qty": get_cell(row, ci, "qty"),
            "price": price_clean, "net": net_clean,
            "lot": get_cell(row, ci, "lot"),
            "unit": get_cell(row, ci, "unit"),
        })
    return all_items, total_net, total_qty


def _doc_ocr_cascade(path: str, supplier_column_map: dict = None) -> tuple:
    """6-layer OCR cascade for scanned documents. Returns (items, total_net, total_qty).

    Silnik można wymusić przez set_ocr_engine() — wtedy działa tylko wybrana warstwa
    (do testów A/B: kaskada vs GLM-OCR vs pojedyncza biblioteka)."""
    import logging
    logger = logging.getLogger(__name__)

    _eng = get_ocr_engine()
    def _want(name: str) -> bool:
        return _eng in ("cascade", "auto", "") or _eng == name

    # Layers 1+2: Camelot (PDF vector tables)
    if _want("camelot"):
        try:
            raw_rows = _camelot_extract(path)
            if raw_rows:
                items, tnet, tqty = _rows_to_items(raw_rows, supplier_column_map)
                if items:
                    logger.info("doc_ocr: camelot → %d items", len(items))
                    return items, tnet, tqty
        except Exception as e:
            logger.debug("doc_ocr camelot: %s", e)

    imgs = _pdf_to_images(path, dpi=220)
    if not imgs:
        return [], None, None

    all_items, all_tnet, all_tqty = [], None, None

    for pi, img in enumerate(imgs):
        page_items = []

        # Layer 2.5: GLM-OCR (gdy wymuszony lub skonfigurowany endpoint)
        if _want("glm") and (_eng == "glm" or _glm_ocr_configured()):
            try:
                raw_rows = _glm_extract_table(img)
                if raw_rows:
                    items, tnet, tqty = _rows_to_items(raw_rows, supplier_column_map)
                    if items:
                        all_items.extend(items)
                        all_tnet = all_tnet or tnet
                        all_tqty = all_tqty or tqty
                        logger.info("doc_ocr: glm → %d items page %d", len(items), pi)
                        continue
            except Exception as e:
                logger.debug("doc_ocr glm page %d: %s", pi, e)

        # Layer 3: Claude Vision
        if _want("claude"):
          try:
            rows = _claude_extract_table(img)
            if rows:
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    ref = str(row.get("ref") or "").strip()
                    if ref.upper() == "TOTAL":
                        all_tnet = str(row.get("net") or "") or all_tnet
                        all_tqty = str(row.get("qty") or "") or all_tqty
                        continue
                    if not is_ref_value(ref):
                        continue
                    pr = str(row.get("price") or "")
                    nr = str(row.get("net") or "")
                    _pv = _norm_number(pr.lstrip("$").strip()) if pr else None
                    _nv = _norm_number(nr.lstrip("$").strip()) if nr else None
                    page_items.append({
                        "ref": ref,
                        "desc": str(row.get("desc") or "") or None,
                        "qty": str(row.get("qty") or "") or None,
                        "price": str(_pv) if _pv is not None else (pr.lstrip("$").strip() or None),
                        "net": str(_nv) if _nv is not None else (nr.lstrip("$").strip() or None),
                        "lot": str(row.get("lot") or "") or None,
                        "unit": str(row.get("unit") or "") or None,
                    })
                if page_items:
                    logger.info("doc_ocr: claude → %d items page %d", len(page_items), pi)
                    all_items.extend(page_items)
                    continue
          except Exception as e:
            logger.debug("doc_ocr claude page %d: %s", pi, e)

        # Layer 4: Mistral OCR
        if _want("mistral"):
          try:
            raw_rows = _mistral_extract_table(img)
            if raw_rows:
                items, tnet, tqty = _rows_to_items(raw_rows, supplier_column_map)
                if items:
                    all_items.extend(items)
                    all_tnet = all_tnet or tnet
                    all_tqty = all_tqty or tqty
                    logger.info("doc_ocr: mistral → %d items page %d", len(items), pi)
                    continue
          except Exception as e:
            logger.debug("doc_ocr mistral page %d: %s", pi, e)

        # Layer 5: img2table
        if _want("img2table"):
          try:
            raw_rows = _img2table_extract(img)
            if raw_rows:
                items, tnet, tqty = _rows_to_items(raw_rows, supplier_column_map)
                if items:
                    all_items.extend(items)
                    all_tnet = all_tnet or tnet
                    all_tqty = all_tqty or tqty
                    logger.info("doc_ocr: img2table → %d items page %d", len(items), pi)
                    continue
          except Exception as e:
            logger.debug("doc_ocr img2table page %d: %s", pi, e)

        # Layer 6: Tesseract PSM 6
        if _want("tesseract"):
          try:
            raw_rows = _tesseract_extract_table(img)
            if raw_rows:
                items, tnet, tqty = _rows_to_items(raw_rows, supplier_column_map)
                if items:
                    all_items.extend(items)
                    all_tnet = all_tnet or tnet
                    all_tqty = all_tqty or tqty
                    logger.info("doc_ocr: tesseract → %d items page %d", len(items), pi)
          except Exception as e:
            logger.debug("doc_ocr tesseract page %d: %s", pi, e)

    return all_items, all_tnet, all_tqty


def _glm_ocr_configured() -> bool:
    """Czy GLM-OCR ma skonfigurowany endpoint (self-host/API)?"""
    import os
    return bool(os.environ.get("GLM_OCR_URL", "").strip())


def _glm_extract_table(img) -> Optional[list]:
    """Adapter GLM-OCR (Zhipu) — wysyła obraz strony do skonfigurowanego endpointu
    i zwraca wiersze [{ref,desc,qty,price,net,lot,unit}] jak inne warstwy.

    Konfiguracja (env): GLM_OCR_URL (wymagany), GLM_OCR_API_KEY (opcjonalny Bearer).
    Endpoint może zwrócić JSON z 'rows' (lista dictów) albo tekst/markdown tabeli —
    parsujemy oba warianty, więc adapter działa z self-hostem i z gatewayem API."""
    import os, io, json, base64
    import urllib.request as _ur
    import urllib.error as _ue

    url = os.environ.get("GLM_OCR_URL", "").strip()
    if not url.lower().startswith(("http://", "https://")):   # pusty lub np. file://
        return None
    api_key = os.environ.get("GLM_OCR_API_KEY", "").strip()

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    payload = json.dumps({
        "image_base64": b64,
        "task": "table",
        "prompt": ("Extract the line-items table. Return JSON array 'rows' with keys "
                   "ref, desc, qty, price, net, lot, unit. If a TOTAL row exists, "
                   "include it with ref='TOTAL'."),
    }).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = _ur.Request(url, data=payload, method="POST", headers=headers)
    try:
        # Bandit B310: schemat http(s) z GLM_OCR_URL sprawdzany wyżej.
        with _ur.urlopen(req, timeout=120) as resp:  # nosec B310
            body = resp.read().decode("utf-8", "replace")
    except (_ue.URLError, OSError, TimeoutError) as e:
        logger.warning("GLM-OCR endpoint error: %s", str(e)[:200])
        return None

    # 1) JSON z 'rows'/'data'
    try:
        data = json.loads(body)
    except Exception:
        data = None
    if isinstance(data, dict):
        for key in ("rows", "data", "items", "result"):
            node = data.get(key)
            if isinstance(node, list) and node and isinstance(node[0], dict):
                return node
        # 2) JSON z tekstem/markdownem
        for key in ("text", "markdown", "content", "ocr"):
            if isinstance(data.get(key), str) and data[key].strip():
                return _parse_markdown_table(data[key])
    elif isinstance(data, list) and data and isinstance(data[0], dict):
        return data
    # 3) Surowy tekst/markdown
    if body.strip():
        return _parse_markdown_table(body)
    return None


def _parse_markdown_table(text: str) -> list:
    """Bardzo prosty parser tabeli markdown/plaintext → listy dictów z heurystyką
    kolumn (ref/qty/price/net). Używany jako fallback, gdy GLM zwróci tekst."""
    rows = []
    for line in (text or "").splitlines():
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        # pomiń separatory typu |---|---|
        if not cells or all(set(c) <= set("-: ") for c in cells):
            continue
        rows.append(cells)
    if len(rows) < 2:
        return []
    # potraktuj pierwszy wiersz jako nagłówek do mapy kolumn
    ci = identify_columns(rows[0]) or {}
    if "ref" not in ci:
        return []
    out = []
    for r in rows[1:]:
        ref = str(r[ci["ref"]]).strip() if ci.get("ref", 0) < len(r) else ""
        if not ref:
            continue
        out.append({
            "ref": ref,
            "desc": str(r[ci["desc"]]) if ci.get("desc") is not None and ci["desc"] < len(r) else "",
            "qty": str(r[ci["qty"]]) if ci.get("qty") is not None and ci["qty"] < len(r) else "",
            "price": str(r[ci["price"]]) if ci.get("price") is not None and ci["price"] < len(r) else "",
            "net": str(r[ci["net"]]) if ci.get("net") is not None and ci["net"] < len(r) else "",
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# POLA NAGŁÓWKOWE
# ─────────────────────────────────────────────────────────────────────────────

HPATTERNS = [
    ("Numer PO",           "error",   [r"purchase order\s*(\d{6,15})", r"order\s*no\.?\s*[:\s]+(\d{6,15})", r"customer order no[:\s]+(\d{6,15})"]),
    ("Nr PI dostawcy",     "warning", [r"FD\s*PI\s*NO[:\s]+([A-Z0-9\-]{5,30})", r"([A-Z]\d{3}-\d{8})", r"invoice\s*no[^\n]*\n([A-Z0-9\-]{5,20})"]),
    ("Data dokumentu",     "info",    [r"^Date\s*[:\s]+(\d{4}[-/]\d{1,2}[-/]\d{1,2})", r"DATED[:\s]+(\d{2}/\d{2}/\d{4})", r"(\d{4}/\d{1,2}/\d{1,2})"]),
    ("Data dostawy",       "warning", [r"Required\s+shipping\s+date[:\s]+(\d{4}[-/]\d{1,2}[-/]\d{1,2})", r"Delivery\s+Date[:\s]+([^\n]{4,25})"]),
    # Warunki płatności: szukaj specyficznych wzorców PRZED ogólnym "Trade Terms"
    ("Warunki płatności",  "error",   [r"Terms of payment[:\s]+([^\n]{5,70})",
                                        r"(100%\s+Payment[^\n]{5,60})",
                                        r"(Payment\s+within[^\n]{5,50})",
                                        r"\b(IZ\d{2}[^\n]{0,60})"]),
    ("Warunki dostawy",    "warning", [r"Terms of delivery\s*[:\s]*([A-Z]{3}[^\n]{0,25})",
                                        r"Trade Terms[:\s]+(FOB|CIF|EXW|DAP|CFR|CPT|DDP)[^\n]{0,25}"]),
    ("Waluta",             "error",   [r"currency[^\n:]*?[:\s]+(USD|EUR|PLN|GBP|CHF)", r"\b(USD|EUR|PLN)\b"]),
    ("Port załadunku",     "info",    [r"Port of (?:Loading|Departure)[^\n:]*?[:\n]+([^\n]{3,40})"]),
    ("Port rozładunku",    "info",    [r"Port of (?:Discharge|Destination)[^\n:]*?[:\n]+([^\n]{3,40})"]),
    ("Kraj pochodzenia",   "info",    [r"country of origin[^\n:]*?[:\s]+([A-Z]{2,20})"]),
]

def extract_header_fields(text: str) -> dict:
    result = {}
    for name, _, patterns in HPATTERNS:
        for p in patterns:
            m = re.search(p, text, re.IGNORECASE | re.MULTILINE)
            if m:
                # BUGFIX: wzorzec bez grupy → group(1) rzuca IndexError; użyj całości
                val = (m.group(1) if m.lastindex else m.group(0)).strip()
                if len(val) >= 2:
                    result[name] = val
                    break
    return result

# ─────────────────────────────────────────────────────────────────────────────
# PORÓWNANIE NAGŁÓWKÓW
# ─────────────────────────────────────────────────────────────────────────────

def cmp_headers(fa: dict, fb: dict) -> list:
    results = []
    all_keys = sorted(set(list(fa.keys()) + list(fb.keys())))
    for key in all_keys:
        va, vb = fa.get(key), fb.get(key)
        severity = next((s for n,s,_ in HPATTERNS if n==key), "info")
        if va is None:
            results.append(HeaderField(key, va, vb, "brak_w_a", severity, "Tylko w dok. B")); continue
        if vb is None:
            results.append(HeaderField(key, va, vb, "brak_w_b", severity, "Tylko w dok. A")); continue

        van, vbn = va.lower().strip(), vb.lower().strip()
        if van == vbn:
            results.append(HeaderField(key, va, vb, "ok", "info", "")); continue

        # Normalizuj daty przez normalizer
        key_lower = key.lower()
        if 'data' in key_lower or 'date' in key_lower:
            try:
                da, db = normalize_date(va), normalize_date(vb)
                if da and db:
                    if da == db:
                        results.append(HeaderField(key, va, vb, "format", "info", f"Ta sama data, różny format ({va} = {vb})"))
                    elif dates_possibly_equal(va, vb):
                        results.append(HeaderField(key, va, vb, "watpliwe", "warning",
                            f"Niejednoznaczny format daty (D/M vs M/D): {va} vs {vb}"))
                    else:
                        results.append(HeaderField(key, va, vb, "roznica", severity, f"Różne daty: {va} ≠ {vb}"))
                    continue
            except Exception:
                pass

        # Normalizuj warunki płatności
        if 'płatno' in key_lower or 'payment' in key_lower:
            try:
                if payment_terms_equal(va, vb):
                    results.append(HeaderField(key, va, vb, "ok", "info", "Równoważne warunki")); continue
            except Exception:
                pass

        # Normalizuj liczby przez kanoniczny parser (parse_num→normalize_number),
        # który sam usuwa $ i poprawnie rozróżnia 1.234,56 (EU) vs 1,234.56 (EN).
        # Wcześniejsze ręczne .replace(',','') psuło format europejski (1.234,56 → 1.234).
        fa_n, fb_n = parse_num(va), parse_num(vb)
        if fa_n is not None and fb_n is not None:
            if abs(fa_n-fb_n) < 0.005:
                results.append(HeaderField(key, va, vb, "format", "info", "Ta sama wartość, różny format"))
            else:
                delta = fb_n - fa_n
                pct = abs(delta)/max(abs(fa_n),0.001)*100
                results.append(HeaderField(key, va, vb, "roznica", severity, f"Różnica: {delta:+,.2f} ({pct:.1f}%)"))
            continue

        sim = fuzz.token_sort_ratio(van, vbn)
        if sim >= 92:   results.append(HeaderField(key, va, vb, "format", "info", f"Drobna różnica formatu ({sim}%)"))
        elif sim >= 70: results.append(HeaderField(key, va, vb, "watpliwe", "warning", f"Podobieństwo {sim}%"))
        else:           results.append(HeaderField(key, va, vb, "roznica", severity, f"Różne wartości (sim={sim}%)"))
    return results

# ─────────────────────────────────────────────────────────────────────────────
# PORÓWNANIE POZYCJI
# ─────────────────────────────────────────────────────────────────────────────

def cmp_num(va, vb, label) -> Optional[tuple]:
    if not va or not vb: return None
    fa, fb = parse_num(va), parse_num(vb)
    if fa is None or fb is None: return None
    if abs(fa-fb) <= 0.005:
        if va.strip().replace(",",".").replace(" ","") != vb.strip().replace(",",".").replace(" ",""):
            return (f"{label}: format ({va.strip()} vs {vb.strip()}) — wartość identyczna", "format")
        return None
    delta = fb - fa
    pct = abs(delta)/max(abs(fa),0.001)*100
    return (f"{label}: {va.strip()} → {vb.strip()} (Δ={delta:+,.4f}, {pct:.2f}%)", "error")

def match_items(items_a: list[ItemRow], items_b: list[ItemRow]) -> list[dict]:
    map_a = {i["ref"]: i for i in items_a if i.get("ref")}
    map_b = {i["ref"]: i for i in items_b if i.get("ref")}
    used_b = set()
    results = []

    # Przypisanie globalne best-first (zamiast zachłannego w kolejności wierszy):
    # 1) najpierw dokładne dopasowania ref (score 100),
    # 2) potem wszystkie kandydujące pary fuzzy ≥80 sortowane malejąco po score —
    #    każdy ref_a i ref_b użyty raz. Eliminuje „kradzież" najlepszego matchu przez
    #    wcześniejszy wiersz (np. NL753S40 vs NL753S45 parowane krzyżowo).
    matched: dict = {}  # ref_a -> (ref_b, score)
    for ref_a in map_a:
        if ref_a in map_b and ref_a not in used_b:
            matched[ref_a] = (ref_a, 100)
            used_b.add(ref_a)
    _cands = []
    for ref_a in map_a:
        if ref_a in matched:
            continue
        for ref_b in map_b:
            if ref_b in used_b:
                continue
            sc = fuzz.token_sort_ratio(ref_a, ref_b)
            if sc >= 80:
                _cands.append((sc, ref_a, ref_b))
    _cands.sort(key=lambda c: c[0], reverse=True)
    _taken_a = set()
    for sc, ref_a, ref_b in _cands:
        if ref_a in _taken_a or ref_b in used_b:
            continue
        matched[ref_a] = (ref_b, sc)
        used_b.add(ref_b)
        _taken_a.add(ref_a)

    for ref_a, ia in map_a.items():
        if ref_a in matched:
            match_ref, score = matched[ref_a]
            ib = map_b[match_ref]
            issues_raw = []
            for va, vb, label in [
                (ia.get("qty"),   ib.get("qty"),   "Ilość"),
                (ia.get("price"), ib.get("price"), "Cena jedn."),
                (ia.get("net"),   ib.get("net"),   "Wartość netto"),
            ]:
                r = cmp_num(va, vb, label)
                if r: issues_raw.append(r)
            if score < 100:
                issues_raw.append((f"Ref fuzzy: '{ref_a}'→'{match_ref}' ({score}%)", "warning"))
            sevs = [s for _,s in issues_raw]
            status = "roznica" if "error" in sevs else "watpliwe" if "warning" in sevs else "format" if "format" in sevs else "ok"
            results.append(ItemRow(
                ref=ref_a, desc_a=ia.get("desc"), desc_b=ib.get("desc"),
                qty_a=ia.get("qty"), qty_b=ib.get("qty"),
                price_a=ia.get("price"), price_b=ib.get("price"),
                net_a=ia.get("net"), net_b=ib.get("net"),
                lot_b=ib.get("lot"), status=status,
                issues=[d for d,_ in issues_raw],
            ))
        else:
            results.append(ItemRow(
                ref=ref_a, desc_a=ia.get("desc"), desc_b=None,
                qty_a=ia.get("qty"), qty_b=None,
                price_a=ia.get("price"), price_b=None,
                net_a=ia.get("net"), net_b=None,
                lot_b=None, status="tylko_a",
                issues=["Pozycja tylko w dok. A — brak w dok. B"],
            ))

    for ref_b, ib in map_b.items():
        if ref_b not in used_b:
            results.append(ItemRow(
                ref=ref_b, desc_a=None, desc_b=ib.get("desc"),
                qty_a=None, qty_b=ib.get("qty"),
                price_a=None, price_b=ib.get("price"),
                net_a=None, net_b=ib.get("net"),
                lot_b=ib.get("lot"), status="tylko_b",
                issues=["Pozycja tylko w dok. B — brak w dok. A"],
            ))

    return results

# ─────────────────────────────────────────────────────────────────────────────
# GŁÓWNA FUNKCJA
# ─────────────────────────────────────────────────────────────────────────────

def compare_tables(path_a: str, path_b: str,
                   doc_type_a: str = "auto",
                   doc_type_b: str = "auto") -> TableCompareResult:

    # Wykryj dostawcę z tekstu dokumentów i pobierz mapę kolumn
    supplier_map_a = None
    supplier_map_b = None
    try:
        from supplier_profiles import detect_supplier, get_supplier
        import pdfplumber
        texts = []
        for path in [path_a, path_b]:
            try:
                with pdfplumber.open(path) as pdf:
                    texts.append('\n'.join(p.extract_text() or '' for p in pdf.pages[:2]))
            except Exception:
                texts.append('')
        combined_text = ' '.join(texts)
        supplier = detect_supplier(combined_text)
        if supplier:
            col_map_json = supplier.get('column_mapping_json') or {}
            if isinstance(col_map_json, str):
                import json
                col_map_json = json.loads(col_map_json) if col_map_json else {}
            # Dopasuj mapę do typu dokumentu
            dt_a_key = doc_type_a if doc_type_a != 'auto' else 'PO'
            dt_b_key = doc_type_b if doc_type_b != 'auto' else 'PI'
            supplier_map_a = col_map_json.get(dt_a_key) or col_map_json.get('PO')
            supplier_map_b = col_map_json.get(dt_b_key) or col_map_json.get('PI')
    except Exception:
        pass

    da = parse_pdf(path_a, supplier_column_map=supplier_map_a)
    db = parse_pdf(path_b, supplier_column_map=supplier_map_b)

    # Nadpisz wykryty typ jeśli user wskazał ręcznie
    TYPE_MAP = {
        "PO": "Purchase Order", "PI": "Proforma Invoice",
        "CI": "Commercial Invoice", "CI_SET": "Commercial Invoice (zestaw)",
        "PL": "Packing List", "SAD": "SAD/ZC415",
        "BL": "Bill of Lading", "WZ": "WZ",
        "FV": "Faktura VAT", "auto": None,
    }
    if doc_type_a != "auto" and doc_type_a in TYPE_MAP:
        da.doc_type = TYPE_MAP[doc_type_a]
    if doc_type_b != "auto" and doc_type_b in TYPE_MAP:
        db.doc_type = TYPE_MAP[doc_type_b]

    # Logika specjalna dla znanych par
    pair = f"{doc_type_a}+{doc_type_b}"

    headers = cmp_headers(da.header_fields, db.header_fields)
    items   = match_items(da.items, db.items)

    # Dla PO vs PI: różnice cen do 2 miejsc to format (PO zaokrągla), nie błąd
    if doc_type_a in ("PO","PI") and doc_type_b in ("PO","PI"):
        for item in items:
            if item.status == "format" and item.issues and all(
                    "Cena" in iss or "format" in iss.lower()
                    for iss in item.issues):
                item.status = "ok"
                item.issues = []

    # Suma kontrolna
    if da.total_net and db.total_net:
        # parse_num→normalize_number sam usuwa $/spacje i rozróżnia EU/EN format;
        # ręczne .replace(',','') psuło sumy europejskie (13.500,00 → 13.500.00).
        fa, fb = parse_num(da.total_net), parse_num(db.total_net)
        if fa is not None and fb is not None:
            if abs(fa-fb) > 0.005:
                delta = fb - fa
                headers.insert(0, HeaderField(
                    "⚠ SUMA KONTROLNA (TOTAL)", da.total_net, db.total_net,
                    "roznica", "error", f"Różnica: {delta:+,.2f} USD"
                ))
            else:
                headers.insert(0, HeaderField(
                    "Suma kontrolna (TOTAL)", da.total_net, db.total_net, "ok", "info",
                    "Sumy zgodne"
                ))

    ok_count     = sum(1 for i in items if i.status=="ok")
    format_count = sum(1 for i in items if i.status=="format")
    warn_count   = sum(1 for i in items if i.status in ("watpliwe","tylko_a","tylko_b"))
    diff_count   = sum(1 for i in items if i.status=="roznica")
    diff_count  += sum(1 for h in headers if h.status in ("roznica","brak_w_a","brak_w_b"))
    warn_count  += sum(1 for h in headers if h.status=="watpliwe")
    format_count+= sum(1 for h in headers if h.status=="format")
    ok_count    += sum(1 for h in headers if h.status=="ok")

    if diff_count>0:     risk="blad"
    elif warn_count>0:   risk="ostrzezenie"
    elif format_count>0: risk="format"
    else:                risk="ok"

    n_items = len(items)
    if diff_count==0 and warn_count==0:
        summary = f"Dokumenty zgodne. Sprawdzono {n_items} pozycji i {len(headers)} pól nagłówkowych."
    else:
        summary = (f"Wykryto {diff_count} rozbieżności, {warn_count} wątpliwości, "
                   f"{format_count} różnic formatowania w {n_items} pozycjach.")

    return TableCompareResult(
        doc_type_a=da.doc_type, doc_type_b=db.doc_type,
        headers=headers, items=items,
        total_a=da.total_net, total_b=db.total_net,
        total_qty_a=da.total_qty, total_qty_b=db.total_qty,
        ok_count=ok_count, format_count=format_count,
        warn_count=warn_count, diff_count=diff_count,
        risk_level=risk, summary=summary,
    )
