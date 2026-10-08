"""
pdf_extractor.py — ulepszony silnik ekstrakcji tekstu i tabel z PDF.

Hierarchia metod:
1. PyMuPDF (fitz)   — najszybszy, najdokładniejszy dla natywnych PDF
2. Camelot          — specjalistyczny do tabel (lattice + stream)
3. pdfplumber       — fallback ogólny
4. OCR (tesseract)  — dla skanów (gdy wszystkie powyższe zwracają pusty tekst)
"""

import os
import re
import io
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── Dostępne biblioteki ───────────────────────────────────────────────────────
try:
    import fitz  # PyMuPDF
    HAS_FITZ = True
except ImportError:
    HAS_FITZ = False

try:
    import camelot
    HAS_CAMELOT = True
except ImportError:
    HAS_CAMELOT = False

try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except ImportError:
    HAS_PDFPLUMBER = False

try:
    import pytesseract
    from PIL import Image
    import numpy as np
    HAS_OCR = True
except ImportError:
    HAS_OCR = False

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

try:
    import anthropic as _anthropic_module
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False


# ─────────────────────────────────────────────────────────────────────────────
# EKSTRAKCJA TEKSTU
# ─────────────────────────────────────────────────────────────────────────────

def extract_text(pdf_path: str, method: str = 'auto') -> dict:
    """
    Ekstrahuje tekst z PDF używając najlepszej dostępnej metody.
    
    Returns:
        {
            'text': str,          # pełny tekst
            'pages': list[str],   # tekst per strona
            'method': str,        # użyta metoda
            'is_scan': bool,      # czy to skan (OCR użyty)
            'confidence': float,  # pewność ekstrakcji 0-1
            'page_count': int,
        }
    """
    result = {
        'text': '', 'pages': [], 'method': 'none',
        'is_scan': False, 'confidence': 0.0, 'page_count': 0
    }

    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"Plik nie istnieje: {pdf_path}")

    # 1. Próba PyMuPDF
    if method in ('auto', 'fitz') and HAS_FITZ:
        r = _extract_fitz(pdf_path)
        if r and len(r['text'].strip()) > 50:
            return r

    # 2. Próba pdfplumber
    if method in ('auto', 'pdfplumber') and HAS_PDFPLUMBER:
        r = _extract_pdfplumber(pdf_path)
        if r and len(r['text'].strip()) > 50:
            return r

    # 3. OCR jako fallback (skan)
    if method in ('auto', 'ocr') and HAS_OCR and HAS_FITZ:
        r = _extract_ocr(pdf_path)
        if r:
            return r

    return result


def _extract_fitz(pdf_path: str) -> Optional[dict]:
    """Ekstrakcja przez PyMuPDF — najdokładniejsza."""
    try:
        # Context manager — zamknięcie dokumentu gwarantowane także na ścieżce
        # wyjątku (wcześniej doc.close() było poza wyjątkiem → wyciek pamięci).
        with fitz.open(pdf_path) as doc:
            pages = []
            total_text = []

            for page in doc:
                # Użyj dict mode dla lepszego layoutu
                blocks = page.get_text("blocks", sort=True)
                page_text = []
                for b in blocks:
                    if b[6] == 0:  # typ 0 = tekst
                        page_text.append(b[4].strip())
                text = '\n'.join(t for t in page_text if t)
                pages.append(text)
                total_text.append(text)

            full_text = '\n\n'.join(total_text)

        return {
            'text': full_text,
            'pages': pages,
            'method': 'fitz',
            'is_scan': False,
            'confidence': 0.95,
            'page_count': len(pages),
        }
    except Exception as e:
        logger.warning(f"fitz failed: {e}")
        return None


def _extract_pdfplumber(pdf_path: str) -> Optional[dict]:
    """Ekstrakcja przez pdfplumber."""
    try:
        import pdfplumber
        pages = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ''
                pages.append(text)
        full_text = '\n\n'.join(pages)
        return {
            'text': full_text,
            'pages': pages,
            'method': 'pdfplumber',
            'is_scan': False,
            'confidence': 0.85,
            'page_count': len(pages),
        }
    except Exception as e:
        logger.warning(f"pdfplumber failed: {e}")
        return None


def _extract_ocr(pdf_path: str, lang: str = 'pol+eng') -> Optional[dict]:
    """OCR z preprocessingiem dla skanów."""
    try:
        pages_text = []
        total_confidence = []

        # Context manager — gwarantuje zamknięcie dokumentu także przy wyjątku.
        with fitz.open(pdf_path) as doc:
            for page_num in range(len(doc)):
                page = doc[page_num]
                # Renderuj w wysokiej rozdzielczości
                mat = fitz.Matrix(2.5, 2.5)  # 2.5x = ~180 DPI
                pix = page.get_pixmap(matrix=mat, alpha=False)
                img_data = pix.tobytes("png")

                from PIL import Image
                img = Image.open(io.BytesIO(img_data))

                # Preprocessing z OpenCV jeśli dostępne
                if HAS_CV2:
                    img = _preprocess_for_ocr(img)

                # OCR
                try:
                    custom_config = r'--oem 3 --psm 6 -l ' + lang
                    data = pytesseract.image_to_data(
                        img, config=custom_config,
                        output_type=pytesseract.Output.DICT
                    )
                    # Wyciągnij tekst i pewność
                    words = []
                    confidences = []
                    for i, word in enumerate(data['text']):
                        try:
                            conf = int(float(data['conf'][i]))
                        except (ValueError, TypeError):
                            continue
                        if conf > 30 and word.strip():
                            words.append(word)
                            confidences.append(conf)

                    page_text = ' '.join(words)
                    pages_text.append(page_text)
                    if confidences:
                        total_confidence.extend(confidences)
                except Exception as e:
                    logger.warning(f"OCR page {page_num}: {e}")
                    pages_text.append('')

        full_text = '\n\n'.join(pages_text)
        avg_conf = sum(total_confidence) / len(total_confidence) / 100 if total_confidence else 0.5

        return {
            'text': full_text,
            'pages': pages_text,
            'method': 'ocr',
            'is_scan': True,
            'confidence': avg_conf,
            'page_count': len(pages_text),
        }
    except Exception as e:
        logger.warning(f"OCR failed: {e}")
        return None


def _preprocess_for_ocr(img) -> 'Image':
    """
    Preprocessing obrazu przed OCR:
    - Konwersja do skali szarości
    - Deskew (wyprostowanie przekrzywionego skanu)
    - Kontrast adaptacyjny
    - Denoising
    """
    import numpy as np
    img_array = np.array(img)

    # Konwersja do skali szarości
    if len(img_array.shape) == 3:
        gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)
    else:
        gray = img_array

    # Deskew
    gray = _deskew(gray)

    # Adaptacyjny próg (lepszy niż globalny dla skanów)
    thresh = cv2.adaptiveThreshold(
        gray, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY, 11, 2
    )

    # Denoising
    denoised = cv2.fastNlMeansDenoising(thresh, h=10)

    from PIL import Image
    return Image.fromarray(denoised)


def _deskew(image: 'np.ndarray') -> 'np.ndarray':
    """Wykrywa i koryguje kąt obrotu skanu."""
    try:
        coords = np.column_stack(np.where(image > 0))
        # Pusta strona (brak pikseli pierwszego planu) → minAreaRect rzuca; pomiń.
        if coords.size == 0:
            return image
        angle = cv2.minAreaRect(coords)[-1]
        if angle < -45:
            angle = -(90 + angle)
        else:
            angle = -angle
        # Koryguj tylko małe kąty (do 15°) — większe to błąd detekcji
        if abs(angle) > 15:
            return image
        h, w = image.shape
        center = (w // 2, h // 2)
        M = cv2.getRotationMatrix2D(center, angle, 1.0)
        rotated = cv2.warpAffine(image, M, (w, h),
                                  flags=cv2.INTER_CUBIC,
                                  borderMode=cv2.BORDER_REPLICATE)
        return rotated
    except Exception:
        return image


# ─────────────────────────────────────────────────────────────────────────────
# EKSTRAKCJA TABEL
# ─────────────────────────────────────────────────────────────────────────────

def extract_tables(pdf_path: str) -> list[dict]:
    """
    Ekstrahuje tabele z PDF używając najlepszej metody.
    
    Returns lista tabel, każda jako:
    {
        'dataframe': pd.DataFrame lub list[list],
        'page': int,
        'method': str,
        'accuracy': float,
        'rows': int, 'cols': int,
    }
    """
    results = []

    # 1. Camelot lattice (tabele z liniami siatki — PO, PI, CI)
    if HAS_CAMELOT:
        camelot_results = _extract_camelot(pdf_path, flavor='lattice')
        results.extend(camelot_results)

        # Jeśli lattice nie znalazł nic — próbuj stream (bez linii)
        if not camelot_results:
            stream_results = _extract_camelot(pdf_path, flavor='stream')
            results.extend(stream_results)

    # 2. PyMuPDF jako fallback lub uzupełnienie
    if HAS_FITZ and not results:
        fitz_results = _extract_tables_fitz(pdf_path)
        results.extend(fitz_results)

    # 3. pdfplumber jako fallback
    if HAS_PDFPLUMBER and not results:
        plumber_results = _extract_tables_plumber(pdf_path)
        results.extend(plumber_results)

    # 4. Claude Vision API — ostateczny fallback gdy żadna metoda nie wyciągnęła tabeli
    if not results and HAS_ANTHROPIC and HAS_FITZ:
        vision_results = _extract_tables_claude_vision(pdf_path)
        results.extend(vision_results)

    return results


def _set_ghostscript_path():
    """
    Ustawia ścieżkę do Ghostscript portable jeśli nie jest zainstalowany systemowo.
    Szuka w folderze projektu: ghostscript/ lub ghostpcl/
    """
    import os, sys
    # Już ustawione
    if os.environ.get('PATH_TO_GHOSTSCRIPT'):
        return

    # Szukaj portable w katalogu projektu i katalogach nadrzędnych
    base = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(base, 'ghostscript', 'gpcl6win64.exe'),
        os.path.join(base, 'ghostscript', 'gswin64c.exe'),
        os.path.join(base, 'ghostscript', 'gswin64.exe'),
        os.path.join(base, 'ghostpcl', 'gpcl6win64.exe'),
        os.path.join(base, 'gs', 'gswin64c.exe'),
        # Windows Program Files (bez admina)
        r'C:\Program Files\gs\gs10.07.0\bin\gswin64c.exe',
        r'C:\Program Files (x86)\gs\gs10.07.0\bin\gswin32c.exe',
    ]
    for path in candidates:
        if os.path.exists(path):
            os.environ['PATH_TO_GHOSTSCRIPT'] = path
            logger.info(f"Ghostscript portable: {path}")
            return


def _extract_camelot(pdf_path: str, flavor: str = 'lattice') -> list[dict]:
    """Ekstrakcja tabel przez Camelot."""
    _set_ghostscript_path()
    try:
        tables = camelot.read_pdf(
            pdf_path,
            pages='all',
            flavor=flavor,
            suppress_stdout=True,
        )
        result = []
        for i, tbl in enumerate(tables):
            df = tbl.df
            # Usuń puste wiersze i kolumny
            df = df.replace('', float('nan')).dropna(how='all').dropna(axis=1, how='all')
            df = df.fillna('')
            # Po usunięciu pustych wierszy/kolumn etykiety i indeks zostają „dziurawe",
            # co psuje pozycyjne odwołania — przenumeruj kolumny i wiersze od zera.
            df.columns = range(df.shape[1])
            df = df.reset_index(drop=True)
            if df.shape[0] < 2 or df.shape[1] < 2:
                continue
            result.append({
                'dataframe': df,
                'page': tbl.page,
                'method': f'camelot_{flavor}',
                'accuracy': tbl.accuracy / 100.0,
                'rows': df.shape[0],
                'cols': df.shape[1],
            })
        return result
    except Exception as e:
        logger.warning(f"Camelot {flavor} failed: {e}")
        return []


def _extract_tables_fitz(pdf_path: str) -> list[dict]:
    """Ekstrakcja tabel przez PyMuPDF (nowa funkcja find_tables)."""
    try:
        results = []
        # Context manager — patrz wyżej (zamknięcie gwarantowane).
        with fitz.open(pdf_path) as doc:
            for page_num, page in enumerate(doc):
                try:
                    tables = page.find_tables()
                    for tbl in tables:
                        df_data = tbl.extract()
                        if not df_data or len(df_data) < 2:
                            continue
                        if HAS_PANDAS:
                            import pandas as pd
                            # Header in row 0 (Camelot-compatible): _find_header_row()
                            # in enhanced_comparator scans data rows, not column names.
                            df = pd.DataFrame(df_data)
                            df = df.fillna('')
                        else:
                            df = df_data
                        results.append({
                            'dataframe': df,
                            'page': page_num + 1,
                            'method': 'fitz_tables',
                            'accuracy': 0.85,
                            'rows': len(df_data) - 1,
                            'cols': len(df_data[0]) if df_data else 0,
                        })
                except Exception:
                    pass
        return results
    except Exception as e:
        logger.warning(f"fitz tables failed: {e}")
        return []


def _extract_tables_plumber(pdf_path: str) -> list[dict]:
    """Ekstrakcja tabel przez pdfplumber."""
    try:
        import pdfplumber
        results = []
        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages):
                tables = page.extract_tables()
                for tbl in (tables or []):
                    if not tbl or len(tbl) < 2:
                        continue
                    if HAS_PANDAS:
                        import pandas as pd
                        # Header in row 0 (Camelot-compatible): _find_header_row()
                        # in enhanced_comparator scans data rows, not column names.
                        df = pd.DataFrame(tbl)
                        df = df.fillna('')
                    else:
                        df = tbl
                    results.append({
                        'dataframe': df,
                        'page': page_num + 1,
                        'method': 'pdfplumber',
                        'accuracy': 0.75,
                        'rows': len(tbl) - 1,
                        'cols': len(tbl[0]) if tbl else 0,
                    })
        return results
    except Exception as e:
        logger.warning(f"pdfplumber tables failed: {e}")
        return []


def _render_page_to_b64(doc, page_num: int, dpi: int = 200) -> str:
    """Renderuje stronę PDF do obrazu PNG zakodowanego w base64."""
    import base64
    page = doc[page_num]
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    return base64.b64encode(pix.tobytes("png")).decode()


_VISION_TABLE_PROMPT = """\
You are a document parser. Extract ALL product/item tables from this trade document image \
(Purchase Order, Proforma Invoice, Commercial Invoice, Packing List, etc.).

Return ONLY a JSON object — no markdown, no explanation:
{
  "tables": [
    {
      "headers": ["column1", "column2", ...],
      "rows": [
        ["val1", "val2", ...],
        ...
      ]
    }
  ]
}

Rules:
- Include ALL rows with product data (item lines).
- Preserve exact values: numbers, codes, dates as they appear.
- One headers array per table. If multiple tables on the page, include all.
- Empty cell = empty string "".
- Do NOT include summary/total rows unless they appear in a separate table.
- If no table found on this page, return {"tables": []}.
"""


def _extract_tables_claude_vision(pdf_path: str) -> list[dict]:
    """
    Fallback ekstrakcji tabel przez Claude Vision API.
    Renderuje każdą stronę PDF do obrazu i prosi Claude o wyciągnięcie tabel jako JSON.
    Uruchamiany tylko gdy wszystkie inne metody zawiodą.
    """
    import json
    import os

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        logger.warning("Claude Vision fallback skipped: ANTHROPIC_API_KEY not set")
        return []

    results = []
    try:
        client = _anthropic_module.Anthropic(api_key=api_key, timeout=120.0)
        with fitz.open(pdf_path) as doc:
            for page_num in range(len(doc)):
                try:
                    img_b64 = _render_page_to_b64(doc, page_num, dpi=200)
                except Exception as e:
                    logger.warning(f"Vision render page {page_num}: {e}")
                    continue

                try:
                    response = client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=4096,
                        messages=[{"role": "user", "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": img_b64,
                                },
                            },
                            {"type": "text", "text": _VISION_TABLE_PROMPT},
                        ]}],
                    )
                except Exception as e:
                    logger.warning(f"Claude Vision API page {page_num}: {e}")
                    continue

                # Log API usage for cost tracking
                try:
                    from api_usage_tracker import record_usage as _record
                    _record("claude-haiku-4-5-20251001", {
                        "input_tokens":  response.usage.input_tokens,
                        "output_tokens": response.usage.output_tokens,
                    }, call_type="table_vision")
                except Exception:
                    pass

                try:
                    raw = response.content[0].text.strip()
                except (IndexError, AttributeError) as e:
                    logger.warning(f"Claude Vision empty response page {page_num}: {e}")
                    continue
                # Strip markdown fences if model wraps response anyway
                if "```" in raw:
                    parts = raw.split("```")
                    raw = parts[1] if len(parts) > 1 else parts[0]
                    if raw.startswith("json"):
                        raw = raw[4:]
                    raw = raw.strip()

                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError as e:
                    logger.warning(f"Claude Vision JSON parse error page {page_num}: {e}")
                    continue

                for tbl in payload.get("tables", []):
                    headers = tbl.get("headers", [])
                    rows = tbl.get("rows", [])
                    if not headers or not rows:
                        continue
                    # Normalize row lengths to match headers
                    ncols = len(headers)
                    norm_rows = []
                    for row in rows:
                        if len(row) < ncols:
                            row = list(row) + [""] * (ncols - len(row))
                        elif len(row) > ncols:
                            row = row[:ncols]
                        norm_rows.append(row)

                    # Include headers as row 0 (Camelot-compatible format):
                    # _find_header_row() in enhanced_comparator scans DataFrame rows,
                    # not column names, so headers must be in the data itself.
                    all_rows = [headers] + norm_rows
                    if HAS_PANDAS:
                        df = pd.DataFrame(all_rows)
                        df = df.fillna("")
                    else:
                        df = all_rows

                    results.append({
                        "dataframe": df,
                        "page": page_num + 1,
                        "method": "claude_vision",
                        "accuracy": 0.90,
                        "rows": len(norm_rows),
                        "cols": ncols,
                    })

    except Exception as e:
        logger.warning(f"Claude Vision table extraction failed: {e}")

    if results:
        logger.info(f"Claude Vision extracted {len(results)} table(s) from {pdf_path}")

    return results


def extract_tables_vision(pdf_path: str) -> list[dict]:
    """Public wrapper — forces Vision extraction regardless of other methods."""
    return _extract_tables_claude_vision(pdf_path)


# ─────────────────────────────────────────────────────────────────────────────
# DETEKCJA TYPU DOKUMENTU
# ─────────────────────────────────────────────────────────────────────────────

DOC_SIGNATURES = {
    'PO': [r'purchase\s+order', r'zamówienie\s+zakupu', r'P\.?O\.?\s*No', r'Purchase\s+Order'],
    'PI': [r'proforma\s+invoice', r'pro.?forma', r'Proforma\s+Invoice'],
    'CI': [r'commercial\s+invoice', r'invoice\s+no', r'faktura\s+handlowa'],
    'SAD': [r'zgłoszenie\s+celne', r'sad\s+\d', r'ZC415', r'WinSAD', r'pole\s+N935'],
    'BL':  [r'bill\s+of\s+lading', r'B/L\s+No', r'konosament', r'shipper'],
    'PL':  [r'packing\s+list', r'lista\s+pakowania', r'gross\s+weight', r'net\s+weight.*carton'],
    'WZ':  [r'wydanie\s+zewnętrzne', r'WZ\s*\d', r'nr\s+WZ'],
    'FV':  [r'faktura\s+VAT', r'NIP\s*\d{10}', r'podatek\s+VAT', r'faktura\s+nr'],
    'CMR': [r'CMR', r'list\s+przewozowy', r'nadawca', r'odbiorca.*consignee'],
}


def detect_doc_type(text: str) -> tuple[str, float]:
    """
    Wykrywa typ dokumentu z tekstu PDF.
    Zwraca (code, confidence).
    """
    if not text:
        return 'auto', 0.0

    scores = {}
    text_lower = text.lower()

    for doc_type, patterns in DOC_SIGNATURES.items():
        score = 0
        for pat in patterns:
            if re.search(pat, text, re.IGNORECASE):
                score += 1
        if score > 0:
            scores[doc_type] = score / len(patterns)

    if not scores:
        return 'auto', 0.0

    best = max(scores, key=scores.get)
    return best, scores[best]


# ─────────────────────────────────────────────────────────────────────────────
# UNIFIED EXTRACT (zastępuje extract_pdf_text z comparator.py)
# ─────────────────────────────────────────────────────────────────────────────

def extract_pdf_full(pdf_path: str) -> dict:
    """
    Główna funkcja: pełna ekstrakcja z autodetekcją.
    Zwraca uzbrojony słownik do użycia przez silniki porównania.
    """
    # Ekstrakcja tekstu
    text_result = extract_text(pdf_path)

    # Ekstrakcja tabel
    tables = []
    try:
        tables = extract_tables(pdf_path)
    except Exception as e:
        logger.warning(f"Table extraction failed: {e}")

    # Detekcja typu
    doc_type, type_confidence = detect_doc_type(text_result['text'])

    return {
        'text': text_result['text'],
        'pages': text_result['pages'],
        'tables': tables,
        'doc_type': doc_type,
        'doc_type_confidence': type_confidence,
        'method': text_result['method'],
        'is_scan': text_result['is_scan'],
        'extraction_confidence': text_result['confidence'],
        'page_count': text_result['page_count'],
    }
