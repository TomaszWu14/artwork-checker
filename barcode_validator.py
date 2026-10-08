"""
barcode_validator.py — weryfikacja kodów kreskowych EAN-13 i GS1-128 na artworkach medycznych.

Obsługuje:
  - EAN-13: walidacja algorytmem sumy kontrolnej GS1
  - GS1-128 / Code 128: parsowanie Application Identifiers (01, 10, 17, 11, 21...)
  - Odczyt kodów z obrazu (zxingcpp jeśli dostępny, pyzbar jako fallback)
  - Sprawdzenie spójności: wartości w kodzie kreskowym vs tekst OCR
"""

import re
import datetime as _dt
import logging as _logging
from typing import Optional

_logger = _logging.getLogger(__name__)

try:
    import zxingcpp
    HAS_ZXING = True
except ImportError:
    HAS_ZXING = False

try:
    from pyzbar import pyzbar as _pyzbar
    HAS_PYZBAR = True
except ImportError:
    HAS_PYZBAR = False

try:
    from PIL import Image as _PIL_Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    _PIL_Image = None


# ─── EAN NORMALISATION ───────────────────────────────────────────────────────

def _normalize_ean(ean_str: str) -> str:
    """Normalise an EAN string to 13 zero-padded digits for safe comparison.

    Avoids lstrip('0') which would make '0123456789012' compare equal to
    '123456789012' (12 digits — a different code).  Instead, pad to 13 digits
    so leading zeros are always preserved.
    """
    digits = re.sub(r'\D', '', ean_str)
    # GTIN-14 z GS1-128 (01) z indykatorem '0' to ten sam towar co EAN-13 —
    # sprowadź do 13 cyfr, inaczej zfill(13) zostawia 14 i porównanie z EAN-13
    # zawsze fałszywie się różni (false KRYTYCZNY na poprawnych kodach).
    if len(digits) == 14 and digits[0] == '0':
        digits = digits[1:]
    return digits.zfill(13)


# ─── EAN-13 WALIDACJA ─────────────────────────────────────────────────────────

def validate_ean13(ean: str) -> dict:
    """Waliduje EAN-13 algorytmem sumy kontrolnej GS1. Zwraca wynik walidacji."""
    digits = re.sub(r'\D', '', ean)
    # GTIN-14 (z GS1-128 AI 01) — sprowadź do EAN-13 tylko gdy indicator digit == '0'
    if len(digits) == 14:
        if digits[0] != '0':
            return {
                "ean": ean, "digits": digits, "valid": False,
                "check_digit_expected": None, "check_digit_actual": None,
                "error": f"GTIN-14 z indicator digit '{digits[0]}' — nie jest EAN-13",
            }
        digits = digits[1:]
    if len(digits) != 13:
        return {
            "ean": ean, "digits": digits, "valid": False,
            "check_digit_expected": None, "check_digit_actual": None,
            "error": f"Nieprawidłowa długość: {len(digits)} cyfr (oczekiwano 13)",
        }
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits[:12]))
    expected = (10 - (total % 10)) % 10
    actual = int(digits[12])
    return {
        # "ean" preserves the original caller-supplied value (may include
        # spaces, dashes, or leading zeros before stripping); "digits" is
        # the cleaned 13-digit string used for the checksum calculation.
        "ean": ean, "digits": digits, "valid": expected == actual,
        "check_digit_expected": expected, "check_digit_actual": actual,
        "error": None if expected == actual else
            f"Błędna cyfra kontrolna: jest {actual}, powinno być {expected}",
    }


def validate_ean8(ean: str) -> dict:
    """Waliduje EAN-8 algorytmem sumy kontrolnej GS1."""
    digits = re.sub(r'\D', '', ean)
    if len(digits) != 8:
        return {"ean": ean, "digits": digits, "valid": False,
                "check_digit_expected": None, "check_digit_actual": None,
                "error": f"Nieprawidłowa długość: {len(digits)} cyfr (oczekiwano 8)"}
    # Wagi EAN-8: cyfry na pozycjach nieparzystych (0-idx parzystych) ×3, parzystych ×1.
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(digits[:7]))
    expected = (10 - (total % 10)) % 10
    actual = int(digits[7])
    return {"ean": ean, "digits": digits, "valid": expected == actual,
            "check_digit_expected": expected, "check_digit_actual": actual,
            "error": None if expected == actual else
                f"Błędna cyfra kontrolna: jest {actual}, powinno być {expected}"}


def validate_upca(upc: str) -> dict:
    """Waliduje UPC-A (12 cyfr). UPC-A = EAN-13 z wiodącym zerem, więc walidujemy
    przez normalizację do 13 cyfr i ponowne użycie algorytmu EAN-13."""
    digits = re.sub(r'\D', '', upc)
    if len(digits) != 12:
        return {"ean": upc, "digits": digits, "valid": False,
                "check_digit_expected": None, "check_digit_actual": None,
                "error": f"Nieprawidłowa długość: {len(digits)} cyfr (oczekiwano 12)"}
    res = validate_ean13("0" + digits)
    res["ean"] = upc
    res["upca"] = digits
    return res


def validate_barcode_number(code: str) -> dict:
    """Dispatcher walidacji numeru kodu wg długości:
    8 → EAN-8, 12 → UPC-A, 13 → EAN-13, 14 → GTIN-14 (→EAN-13).
    Zwraca wynik walidacji wzbogacony o pole 'type'."""
    digits = re.sub(r'\D', '', code)
    n = len(digits)
    if n == 8:
        r = validate_ean8(code); r["type"] = "EAN-8"; return r
    if n == 12:
        r = validate_upca(code); r["type"] = "UPC-A"; return r
    r = validate_ean13(code)
    r["type"] = "GTIN-14" if n == 14 else "EAN-13"
    return r


# ─── GS1-128 PARSER ──────────────────────────────────────────────────────────

# (AI) → (maks. długość wartości, opis, 'fixed'|'var')
_GS1_AI = {
    "00": (18, "SSCC",              "fixed"),
    "01": (14, "GTIN",              "fixed"),
    "02": (14, "GTIN zawartości",   "fixed"),
    "10": (20, "LOT/Partia",        "var"),
    "11": (6,  "Data produkcji",    "fixed"),
    "13": (6,  "Data pakowania",    "fixed"),
    "15": (6,  "Data best-by",      "fixed"),
    "17": (6,  "Data ważności",     "fixed"),
    "20": (2,  "Wariant produktu",  "fixed"),
    "21": (20, "Nr seryjny",        "var"),
    "22": (29, "Kod konsumenta",    "var"),
    "30": (8,  "Ilość zmienna",     "var"),
    "37": (8,  "Ilość w kartonie",  "var"),
    "240": (30, "Nr dodatkowy",     "var"),
    "241": (30, "Nr klienta",       "var"),
    "310": (6,  "Masa netto [kg]",  "fixed"),
    "320": (6,  "Masa netto [lb]",  "fixed"),
    "330": (6,  "Masa brutto [kg]", "fixed"),
    "400": (30, "Nr zamówienia",    "var"),
    "401": (30, "Nr przesyłki",     "var"),
    "410": (13, "Kod dostawcy",     "fixed"),
    "420": (20, "Kod pocztowy",     "var"),
    "710": (30, "NHRN Germany",     "var"),
    "711": (30, "NHRN France",      "var"),
    "713": (30, "NHRN Poland",      "var"),
}


def _parse_gs1_date(s: str) -> Optional[str]:
    """Parsuje datę GS1 YYMMDD → 'RRRR-MM-DD'. DD=00 → ostatni dzień miesiąca."""
    if len(s) != 6 or not s.isdigit():
        return None
    yy, mm, dd = s[:2], s[2:4], s[4:]
    # Y2K boundary: years within next 20 years are 2000s, otherwise 1900s
    _current_year = _dt.datetime.now().year % 100
    cutoff = (_current_year + 20) % 100
    if int(yy) <= cutoff:
        year = 2000 + int(yy)
    else:
        year = 1900 + int(yy)
    mm_int = int(mm)
    if not (1 <= mm_int <= 12):
        return None
    import calendar
    max_day = calendar.monthrange(year, mm_int)[1]
    if dd == "00":                       # GS1: DD=00 → ostatni dzień miesiąca
        dd = str(max_day)
    elif not (1 <= int(dd) <= max_day):  # odrzuć niemożliwe dni (np. (17)310230 = 30 lutego)
        return None
    return f"{year}-{mm}-{dd.zfill(2)}"


def gtin_check_digit_valid(gtin: str) -> bool:
    """Waliduje cyfrę kontrolną GTIN-8/12/13/14 (algorytm modulo-10 GS1)."""
    g = re.sub(r"\D", "", str(gtin or ""))
    if len(g) not in (8, 12, 13, 14):
        return False
    body, check = g[:-1], int(g[-1])
    total = sum(int(ch) * (3 if i % 2 == 0 else 1)
                for i, ch in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


# AI obowiązkowe na opakowaniach wyrobów medycznych (UDI): GTIN (01),
# data ważności (17), numer partii LOT (10).
_MANDATORY_MEDICAL_AI = ("01", "17", "10")


def _finalize_gs1(result: dict) -> dict:
    """Dolicza walidację cyfry kontrolnej GTIN oraz listę brakujących
    obowiązkowych AI (UDI medyczne) do sparsowanego wyniku GS1-128."""
    if result.get("gtin"):
        result["gtin_valid"] = gtin_check_digit_valid(result["gtin"])
        if result["gtin_valid"] is False:
            result["errors"].append(f"Błędna cyfra kontrolna GTIN: {result['gtin']}")
    present = {f["ai"] for f in result.get("fields", [])}
    result["missing_mandatory_ai"] = [
        _GS1_AI[ai][1] for ai in _MANDATORY_MEDICAL_AI
        if ai not in present and ai in _GS1_AI
    ]
    return result


def parse_gs1_128(raw: str) -> dict:
    """
    Parsuje surowe dane GS1-128 (zdekodowane z kodu kreskowego lub tekstu).

    Obsługuje dwa formaty:
      "(01)05900002608103(17)310510(10)LOT2025"  — z nawiasami
      "010590000260810317310510"                  — bez nawiasów (ciągły)

    Zwraca słownik z nazwanymi polami (gtin, lot, exp_date, prod_date, serial).
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    result = {
        "raw": raw, "fields": [], "errors": [],
        "gtin": None, "lot": None, "exp_date": None,
        "prod_date": None, "serial": None,
        "gtin_valid": None, "missing_mandatory_ai": [],
    }

    # Fast path: parenthesised format (AI)value(AI)value… preserves field boundaries
    # exactly — no need for heuristic backwards scan.  This is the human-readable
    # representation printed on medical packaging and returned by most barcode decoders.
    # GUARD: only use the fast path when there is no trailing unbracketed data after
    # the last parenthesised field.  Some decoders emit mixed formats such as
    # "(01)05900002608103(10)LOT2025310510" where "310510" is an EXP date without
    # its "(17)" wrapper.  The regex [^(]* greedily absorbs the trailing bytes into
    # the last field's value instead of parsing them as a separate AI, silently
    # dropping the expiry date.  Detect this by comparing the total characters
    # consumed by all matches against the stripped input length; if they differ,
    # fall through to the heuristic scanner which handles the mixed case.
    if '(' in raw:
        matches = list(re.finditer(r'\((\d{2,4})\)([^(]*)', raw))
        consumed = sum(len(m.group(0)) for m in matches)
        # Allow for leading whitespace/non-AI prefix before the first '('
        prefix_len = raw.index('(') if '(' in raw else 0
        _use_fast = bool(matches and consumed >= len(raw) - prefix_len)
        # Dodatkowy strażnik: AI o STAŁEJ długości musi mieć wartość dokładnie tej
        # długości. Jeśli regex [^(]* wchłonął doklejony (nieujęty w nawias) kolejny
        # AI — np. "(17)31051010LOT…" — wartość AI daty (6 cyfr) byłaby za długa.
        # Wtedy NIE ufamy fast-path i schodzimy do skanera ciągłego, który tnie po AI.
        if _use_fast:
            for _m in matches:
                _info = _GS1_AI.get(_m.group(1))
                if _info and _info[2] == "fixed" and len(_m.group(2).replace(" ", "")) != _info[0]:
                    _use_fast = False
                    break
        # Strażnik dla pola ZMIENNEJ długości: jeśli jego wartość kończy się
        # doklejonym (nieujętym w nawias) AI o STAŁEJ długości z dokładnie tylu
        # znakami, ile wynosi jego długość — np. "(10)ABC17310510" gdzie "17310510"
        # to data (17)+6 cyfr — fast-path zjadłby datę. Schodzimy do skanera ciągłego.
        if _use_fast:
            for _m in matches:
                _info = _GS1_AI.get(_m.group(1))
                if not _info or _info[2] != "var":
                    continue
                _val = _m.group(2).replace(" ", "")
                _glued = False
                for _i in range(1, len(_val)):
                    for _ai_len in (4, 3, 2):
                        _cand = _val[_i:_i + _ai_len]
                        _ci = _GS1_AI.get(_cand)
                        if _ci and _ci[2] == "fixed" and len(_val) - (_i + _ai_len) == _ci[0]:
                            _glued = True
                            break
                    if _glued:
                        break
                if _glued:
                    _use_fast = False
                    break
        if _use_fast:
            for m in matches:
                ai = m.group(1)
                value = m.group(2).replace(" ", "")
                if ai not in _GS1_AI:
                    result["errors"].append(f"Nierozpoznany AI: '({ai})'")
                    continue
                _name = _GS1_AI[ai][1]
                parsed_val = value
                if ai in ("11", "13", "15", "17"):
                    _d = _parse_gs1_date(value)
                    if _d:
                        parsed_val = _d
                    else:
                        # BUGFIX: niemożliwa data (np. 260230 = 30 lutego) była cicho
                        # przepuszczana jako surowy ciąg — zgłoś błąd walidacji.
                        result["errors"].append(
                            f"Nieprawidłowa data {_name} (AI {ai}): '{value}'")
                result["fields"].append({
                    "ai": ai, "name": _name,
                    "value": value, "value_parsed": parsed_val,
                })
                if ai == "01":
                    result["gtin"] = value
                elif ai == "10":
                    result["lot"] = value
                elif ai == "17":
                    result["exp_date"] = parsed_val
                elif ai == "11":
                    result["prod_date"] = parsed_val
                elif ai == "21":
                    result["serial"] = value
            return _finalize_gs1(result)
        # Mixed/trailing data — fall through to heuristic scanner below,
        # but strip the parens so it can parse the continuous form.
        raw = re.sub(r'[()]', '', raw)

    # Continuous (no-parentheses) format: field boundaries inferred heuristically.
    normalized = raw.replace(" ", "")
    pos, n = 0, len(normalized)
    _max_iterations = n + 1  # guard against infinite loop on malformed input

    while pos < n and _max_iterations > 0:
        _max_iterations -= 1
        # Pomiń separatory FNC1/GS (\x1d) między polami — niektóre kodery wstawiają
        # go też po polach stałej długości.
        if normalized[pos] == "\x1d":
            pos += 1
            continue
        matched_ai = None
        for ai_len in (4, 3, 2):
            candidate = normalized[pos:pos + ai_len]
            if candidate in _GS1_AI:
                matched_ai = candidate
                break
        if not matched_ai:
            result["errors"].append(f"Nierozpoznany AI przy poz. {pos}: '{normalized[pos:pos+4]}'")
            break

        max_len, name, vtype = _GS1_AI[matched_ai]
        pos += len(matched_ai)

        if vtype == "fixed":
            value = normalized[pos:pos + max_len]
            pos += max_len
        elif (_gs := normalized.find("\x1d", pos, pos + max_len)) != -1:
            # FNC1/GS jednoznacznie kończy pole zmiennej długości w realnych kodach
            # GS1-128 — utnij na nim (kanoniczne), zanim sięgniemy po heurystykę.
            value = normalized[pos:_gs]
            pos = _gs + 1
        else:
            # Zmienna długość bez separatora FNC1 — skanuj W PRZÓD i utnij na
            # PIERWSZYM miejscu, gdzie zaczyna się PEŁNY, rozpoznany kolejny AI:
            #  • dokładne dopasowanie klucza (nie 'startswith' — cyfry wewnątrz
            #    wartości fałszywie udają prefiks AI),
            #  • dla AI o stałej długości musi zostać miejsce na całą jego wartość.
            # Poprzednie podejście (największe e + startswith) zjadało za dużo i
            # gubiło/uszkadzało LOT oraz datę ważności.
            limit = min(max_len, n - pos)
            end = limit
            e = 1
            while e <= limit:
                hit = False
                for ai_len in (4, 3, 2):
                    cand = normalized[pos + e:pos + e + ai_len]
                    if cand in _GS1_AI:
                        _ml, _nm, _vt = _GS1_AI[cand]
                        if _vt != "fixed" or (n - (pos + e + ai_len)) >= _ml:
                            end = e
                            hit = True
                            break
                if hit:
                    break
                e += 1
            value = normalized[pos:pos + end]
            # Ensure we always advance; if no data, consume remainder to avoid loop
            pos += len(value) if value else (n - pos)

        parsed_val = value
        if matched_ai in ("11", "13", "15", "17"):
            _d = _parse_gs1_date(value)
            if _d:
                parsed_val = _d
            else:
                # BUGFIX: niemożliwa data → zgłoś błąd zamiast cicho zostawiać surowiec
                result["errors"].append(
                    f"Nieprawidłowa data {name} (AI {matched_ai}): '{value}'")

        result["fields"].append({
            "ai": matched_ai, "name": name,
            "value": value, "value_parsed": parsed_val,
        })
        if matched_ai == "01":
            result["gtin"] = value
        elif matched_ai == "10":
            result["lot"] = value
        elif matched_ai == "17":
            result["exp_date"] = parsed_val
        elif matched_ai == "11":
            result["prod_date"] = parsed_val
        elif matched_ai == "21":
            result["serial"] = value

    return _finalize_gs1(result)


# ─── ODCZYT KODÓW Z OBRAZU ────────────────────────────────────────────────────

def read_barcodes_from_image(img) -> list:
    """
    Odczytuje kody kreskowe z obrazu PIL.
    Zwraca listę {format, raw_data, parsed}.
    Używa zxingcpp (preferowany) lub pyzbar (fallback).
    """
    if img is None:
        return []
    if HAS_ZXING:
        return _read_zxing(img)
    if HAS_PYZBAR:
        return _read_pyzbar(img)
    return []


def _upscale_for_barcode(img):
    """Upscale small/medium renders so a barcode that occupies a tiny fraction of a
    large packaging sheet (e.g. a 20 mm code on a 362 mm dieline downscaled to 3500 px
    ≈ 190 px wide) carries enough bars to decode. Caps output to avoid OOM."""
    try:
        w, h = img.size
        longest = max(w, h)
        if longest < 2600:
            scale = min(3.0, 2600 / max(longest, 1))
            if scale > 1.05:
                from PIL import Image as _Im
                _lz = _Im.LANCZOS if hasattr(_Im, "LANCZOS") else _Im.BICUBIC
                return img.resize((int(w * scale), int(h * scale)), _lz)
    except Exception:
        pass
    return img


def _read_zxing(img) -> list:
    try:
        import numpy as np
        rgb = img.convert("RGB")
        arr = np.array(rgb)

        def _decode(a):
            # try_rotate + try_downscale + try_invert make zxingcpp far more robust on
            # rotated / small / inverted codes. Older zxingcpp lacks these kwargs → fall
            # back to the plain call.
            try:
                return zxingcpp.read_barcodes(a, try_rotate=True, try_downscale=True, try_invert=True)
            except TypeError:
                try:
                    return zxingcpp.read_barcodes(a, try_rotate=True, try_downscale=True)
                except TypeError:
                    return zxingcpp.read_barcodes(a)

        barcodes = _decode(arr)
        if not barcodes:
            # Retry on an upscaled copy — recovers small codes lost in page downscaling.
            up = _upscale_for_barcode(rgb)
            if up is not rgb:
                barcodes = _decode(np.array(up))
        return [{"format": str(bc.format).replace("BarcodeFormat.", ""),
                 "raw_data": bc.text,
                 "parsed": _classify(str(bc.format), bc.text)} for bc in barcodes]
    except Exception as e:
        return [{"format": "error", "raw_data": "", "parsed": {}, "error": str(e)[:200]}]


def _read_pyzbar(img) -> list:
    try:
        import numpy as np
        from PIL import ImageEnhance

        def _decode(pil):
            # Contrast boost helps pyzbar on low-contrast medical print.
            g = ImageEnhance.Contrast(pil.convert("L")).enhance(1.5)
            return _pyzbar.decode(np.array(g))

        barcodes = _decode(img)
        if not barcodes:
            up = _upscale_for_barcode(img)
            if up is not img:
                barcodes = _decode(up)
    except Exception as e:
        return [{"format": "error", "raw_data": "", "parsed": {}, "error": str(e)[:200]}]
    results = []
    for bc in barcodes:
        try:
            raw = bc.data.decode("utf-8", errors="replace")
            results.append({"format": bc.type,
                             "raw_data": raw,
                             "parsed": _classify(bc.type, raw)})
        except Exception as e:
            results.append({"format": str(bc.type), "raw_data": "", "parsed": {}, "error": str(e)[:200]})
    return results


def _classify(fmt: str, raw: str) -> dict:
    """Klasyfikuje i parsuje zawartość kodu wg jego formatu."""
    fu = fmt.upper()
    if "EAN_13" in fu or fu == "EAN13":
        return {"type": "EAN-13", "ean": raw, "validation": validate_ean13(raw)}
    if "EAN_8" in fu or fu == "EAN8":
        return {"type": "EAN-8", "ean": raw, "validation": validate_ean8(raw)}
    if "UPC_A" in fu or fu == "UPCA" or fu == "UPC":
        return {"type": "UPC-A", "ean": raw, "validation": validate_upca(raw)}
    if "CODE_128" in fu or fu == "CODE128":
        return {"type": "Code128/GS1-128", "gs1": parse_gs1_128(raw)}
    if "DATA_MATRIX" in fu or "DATAMATRIX" in fu:
        return {"type": "DataMatrix/GS1", "gs1": parse_gs1_128(raw)}
    if "QR" in fu:
        return {"type": "QR", "data": raw}
    return {"type": fmt, "data": raw}


# ─── WALIDACJA KOMPLETNA ─────────────────────────────────────────────────────

def _norm_ean13(code) -> str:
    """Normalizuje kod numeryczny do postaci EAN-13 na potrzeby porównania A↔B:
    UPC-A (12 cyfr) → dopisz wiodące 0; GTIN-14 z wiodącym 0 → usuń je. Inne
    długości (np. EAN-8) zwracane bez zmian (same cyfry). Dzięki temu kod
    zdekodowany jako UPC-A 12-cyfrowy pasuje do 13-cyfrowego odczytu z OCR."""
    d = re.sub(r"\D", "", str(code or ""))
    if len(d) == 12:
        return "0" + d
    if len(d) == 14 and d[0] == "0":
        return d[1:]
    return d


def _decoded_eans(barcodes: list) -> list:
    """Wyciąga POPRAWNE numery EAN-13 z listy zdekodowanych kodów kreskowych.

    Obejmuje EAN-13/EAN-8/UPC-A oraz GTIN z GS1-128/DataMatrix (GTIN-14 z wiodącym
    0 → EAN-13). Zwraca tylko kody z poprawną sumą kontrolną, bez duplikatów —
    służą jako autorytatywne źródło do porównania A↔B (dokładniejsze niż OCR)."""
    out = []
    for bc in barcodes or []:
        if bc.get("error"):
            continue
        parsed = bc.get("parsed", {})
        ean = None
        if "EAN-13" in parsed.get("type", "") or "EAN-8" in parsed.get("type", "") \
                or "UPC-A" in parsed.get("type", ""):
            v = parsed.get("validation", {})
            if v.get("valid"):
                # validate_upca stores the original 12-digit code in 'ean' → normalize
                # to 13 so it lines up with OCR-read EAN-13 in the A↔B comparison.
                ean = _norm_ean13(v.get("ean") or parsed.get("ean") or bc.get("raw_data", ""))
        else:
            gtin = (parsed.get("gs1", {}) or {}).get("gtin")
            if gtin:
                cand = gtin[1:] if (len(gtin) == 14 and gtin[0] == "0") else gtin
                if len(cand) == 13 and validate_ean13(cand).get("valid"):
                    ean = cand
        if ean and ean not in out:
            out.append(ean)
    return out


def validate_artwork_barcodes(
    ocr_text_a: str,
    ocr_text_b: str,
    images_a: list = None,
    images_b: list = None,
) -> dict:
    """
    Pełna walidacja kodów kreskowych dla pary artworków.

    Sprawdza:
      1. EAN-13 z tekstu OCR — suma kontrolna GS1
      2. EAN A vs B — czy zgodne
      3. Kody z obrazów (jeśli zxingcpp/pyzbar dostępne)
      4. Spójność: EAN/LOT/EXP z kodu kreskowego vs tekst OCR

    Zwraca:
      {messages, has_barcode_lib, eans_a, eans_b}
    """
    messages = []

    # Upewnij się, że images_a/images_b są listami (mogą być None)
    if images_a is None:
        images_a = []
    if images_b is None:
        images_b = []

    has_lib = HAS_ZXING or HAS_PYZBAR

    # 0. Zdekoduj kody kreskowe z obrazów ZANIM porównamy A↔B. Zdekodowany kod jest
    #    dokładniejszy niż OCR (OCR myli 0/O, 1/I, 5/S) — używamy go jako źródła
    #    prawdy do porównania EAN A vs B, OCR pozostaje fallbackiem.
    barcodes_a = read_barcodes_from_image(images_a[0]) if (has_lib and images_a) else []
    barcodes_b = read_barcodes_from_image(images_b[0]) if (has_lib and images_b) else []
    decoded_eans_a = _decoded_eans(barcodes_a)
    decoded_eans_b = _decoded_eans(barcodes_b)

    # 1. Kody numeryczne z OCR: EAN-13, UPC-A (12), EAN-8.
    _CODE_RE = r'\b(\d{13}|\d{12}|\d{8})\b'
    eans_a = list(dict.fromkeys(re.findall(_CODE_RE, ocr_text_a)))
    eans_b = list(dict.fromkeys(re.findall(_CODE_RE, ocr_text_b)))
    all_seen = set()

    for src, eans in (("A", eans_a), ("B", eans_b)):
        for ean in eans:
            if ean in all_seen:
                continue
            all_seen.add(ean)
            v = validate_barcode_number(ean)
            _ct = v.get("type", "EAN")
            if v["valid"]:
                messages.append({
                    "level": "OK", "source": src, "code_type": _ct,
                    "text": (f"{_ct} ({ean}): cyfra kontrolna "
                             f"{v['check_digit_actual']} — poprawna."),
                })
            else:
                messages.append({
                    "level": "KRYTYCZNY", "source": src, "code_type": _ct,
                    "text": f"{_ct} ({ean}): {v['error']}",
                })

    # 2. Zgodność EAN A vs B — zdekodowany kod ma pierwszeństwo nad OCR.
    #    Dla każdej strony bierzemy zdekodowane EAN-y, a OCR tylko gdy kodu nie odczytano.
    # Normalizuj obie strony do EAN-13 (UPC-A 12→13, GTIN-14→13) — inaczej kod
    # zdekodowany jako UPC-A i 13-cyfrowy odczyt OCR dawały fałszywą niezgodność.
    cmp_a = [_norm_ean13(e) for e in (decoded_eans_a or eans_a)]
    cmp_b = [_norm_ean13(e) for e in (decoded_eans_b or eans_b)]
    _src_note = (" (z kodu kreskowego)"
                 if (decoded_eans_a or decoded_eans_b) else " (z tekstu OCR)")
    if cmp_a or cmp_b:
        sa, sb = set(cmp_a), set(cmp_b)
        if sa and sb:
            if sa == sb:
                messages.append({
                    "level": "OK", "source": "A+B", "code_type": "EAN-13",
                    "text": f"EAN-13 zgodny między artworkami{_src_note}: {', '.join(sorted(sa))}",
                })
            else:
                for ean in sorted(sa - sb):
                    messages.append({
                        "level": "KRYTYCZNY", "source": "A+B", "code_type": "EAN-13",
                        "text": f"EAN {ean} obecny tylko w wersji A — brak w wersji B.",
                    })
                for ean in sorted(sb - sa):
                    messages.append({
                        "level": "KRYTYCZNY", "source": "A+B", "code_type": "EAN-13",
                        "text": f"EAN {ean} obecny tylko w wersji B — brak w wersji A.",
                    })
    else:
        messages.append({
            "level": "INFO", "source": "A+B", "code_type": "EAN-13",
            "text": ("Nie wykryto EAN-13 (ani w kodzie kreskowym, ani w tekście OCR) — "
                     "sprawdź kod kreskowy wizualnie."),
        })

    # 3. Spójność kod kreskowy ↔ tekst OCR (reużywa kodów zdekodowanych w sekcji 0)
    if not has_lib:
        messages.append({
            "level": "INFO", "source": "system", "code_type": "Odczyt",
            "text": ("Biblioteka odczytu kodów (zxingcpp/pyzbar) nie jest zainstalowana. "
                     "EAN-13 sprawdzono wyłącznie na podstawie tekstu OCR. "
                     "Zainstaluj: pip install zxingcpp"),
        })
    else:
        for src, barcodes, txt in (("A", barcodes_a, ocr_text_a), ("B", barcodes_b, ocr_text_b)):
            if not barcodes:
                continue
            messages.extend(_check_barcodes_vs_text(barcodes, txt, src))

    lot_a, gs1_a = _lot_and_gs1(barcodes_a)
    lot_b, gs1_b = _lot_and_gs1(barcodes_b)
    return {
        "messages": messages,
        "has_barcode_lib": has_lib,
        "eans_a": eans_a,
        "eans_b": eans_b,
        "lot_a": lot_a,
        "lot_b": lot_b,
        "barcode_gs1_a": gs1_a,
        "barcode_gs1_b": gs1_b,
    }


def _lot_and_gs1(barcodes: list):
    """Z listy zdekodowanych kodów wyciąga pierwszy LOT i zwięzły opis GS1
    ('(01)... (10)... (17)...') — zasila podsumowanie raportu artworku, które
    wcześniej miało te pola zawsze puste."""
    lot = gs1 = None
    for bc in barcodes or []:
        if bc.get("error"):
            continue
        g = (bc.get("parsed") or {}).get("gs1")
        if not g:
            continue
        if not lot and g.get("lot"):
            lot = g.get("lot")
        if not gs1 and g.get("fields"):
            gs1 = " ".join(f"({f['ai']}){f['value']}" for f in g["fields"])
    return lot, gs1


def _check_barcodes_vs_text(barcodes: list, ocr_text: str, source: str) -> list:
    """Sprawdza spójność odczytanych kodów kreskowych z tekstem OCR."""
    messages = []
    eans_in_text = re.findall(r'\b(\d{13}|\d{12}|\d{8})\b', ocr_text)

    for bc in barcodes:
        if bc.get("error"):
            continue
        parsed = bc.get("parsed", {})
        bc_type = parsed.get("type", bc.get("format", ""))

        if "EAN-13" in bc_type:
            v = parsed.get("validation", {})
            ean = v.get("ean", bc.get("raw_data", ""))
            if not v.get("valid"):
                messages.append({
                    "level": "KRYTYCZNY", "source": source, "code_type": "EAN-13",
                    "text": f"EAN-13 z kodu ({ean}): {v.get('error', 'błąd')}",
                })
                continue
            if ean in eans_in_text:
                messages.append({
                    "level": "OK", "source": source, "code_type": "EAN-13",
                    "text": f"EAN-13 z kodu ({ean}) zgodny z tekstem na opakowaniu.",
                })
            else:
                messages.append({
                    "level": "KRYTYCZNY", "source": source, "code_type": "EAN-13",
                    "text": (f"EAN-13 z kodu ({ean}) NIE ZGADZA SIĘ z tekstem OCR! "
                             f"Tekst zawiera: {', '.join(eans_in_text) or 'brak EAN'}"),
                })

        elif "EAN-8" in bc_type or "UPC-A" in bc_type:
            code_type = "EAN-8" if "EAN-8" in bc_type else "UPC-A"
            v = parsed.get("validation", {})
            code = v.get("ean", bc.get("raw_data", ""))
            if not v.get("valid"):
                messages.append({
                    "level": "KRYTYCZNY", "source": source, "code_type": code_type,
                    "text": f"{code_type} z kodu ({code}): {v.get('error', 'błąd')}",
                })
                continue
            # UPC-A (12) bywa drukowane jako EAN-13 z wiodącym zerem — porównaj obie formy.
            variants = {code}
            if code_type == "UPC-A" and len(code) == 12:
                variants.add("0" + code)
            if variants & set(eans_in_text):
                messages.append({
                    "level": "OK", "source": source, "code_type": code_type,
                    "text": f"{code_type} z kodu ({code}) zgodny z tekstem na opakowaniu.",
                })
            else:
                messages.append({
                    "level": "KRYTYCZNY", "source": source, "code_type": code_type,
                    "text": (f"{code_type} z kodu ({code}) NIE ZGADZA SIĘ z tekstem OCR! "
                             f"Tekst zawiera: {', '.join(eans_in_text) or 'brak kodu'}"),
                })

        elif any(k in bc_type for k in ("Code128", "GS1", "DataMatrix")):
            gs1 = parsed.get("gs1", {})
            if not gs1:
                continue
            gtin = gs1.get("gtin")
            lot  = gs1.get("lot")
            exp  = gs1.get("exp_date")
            prod = gs1.get("prod_date")

            if gtin:
                if len(gtin) == 14:
                    if gtin[0] == '0':
                        ean13 = gtin[1:]
                    else:
                        # Non-retail GTIN-14 — validate as-is but skip false EAN-13 check
                        messages.append({"level": "INFO", "source": source, "code_type": bc_type,
                                         "text": f"GS1-128 GTIN-14 (01){gtin} — indykator={gtin[0]}, pomijam weryfikację EAN-13."})
                        ean13 = None
                else:
                    ean13 = gtin
                if ean13 is not None:
                    v = validate_ean13(ean13)
                    level = "OK" if v["valid"] else "KRYTYCZNY"
                    msg = (f"GS1-128 GTIN (01){gtin}: EAN-13={ean13} — poprawny."
                           if v["valid"] else
                           f"GS1-128 GTIN (01){gtin}: {v['error']}")
                    messages.append({"level": level, "source": source, "code_type": bc_type, "text": msg})

                if ean13 is not None and eans_in_text and ean13 not in eans_in_text:
                    messages.append({
                        "level": "KRYTYCZNY", "source": source, "code_type": bc_type,
                        "text": (f"GTIN z GS1-128 ({ean13}) NIE ZGADZA SIĘ "
                                 f"z EAN w tekście: {', '.join(eans_in_text)}"),
                    })

            if lot:
                m = re.search(
                    r'(?:LOT|BATCH|SERIA)[:\s#]+([A-Za-z0-9][A-Za-z0-9\-]{2,20})',
                    ocr_text, re.IGNORECASE)
                lot_text = m.group(1) if m else None
                if lot_text:
                    if lot.upper() == lot_text.upper():
                        messages.append({
                            "level": "OK", "source": source, "code_type": bc_type,
                            "text": f"GS1-128 LOT (10){lot} zgodny z tekstem na opakowaniu.",
                        })
                    else:
                        messages.append({
                            "level": "KRYTYCZNY", "source": source, "code_type": bc_type,
                            "text": f"LOT w kodzie (10){lot} ≠ LOT w tekście '{lot_text}'.",
                        })
                else:
                    messages.append({
                        "level": "INFO", "source": source, "code_type": bc_type,
                        "text": (f"GS1-128 LOT (10){lot} — "
                                 "brak tekstu LOT do porównania (może być placeholder)."),
                    })

            if exp:
                messages.append({
                    "level": "OK", "source": source, "code_type": bc_type,
                    "text": f"GS1-128 data ważności (17): {exp}",
                })

            if prod:
                messages.append({
                    "level": "OK", "source": source, "code_type": bc_type,
                    "text": f"GS1-128 data produkcji (11): {prod}",
                })

    return messages


# ─── DEEP VALIDATE PRINTED DATA ───────────────────────────────────────────────

def deep_validate_printed_data(ocr_text: str, img_b64: Optional[str] = None) -> dict:
    """
    Sprawdza spójność nadrukowanych danych na artworku dostawcy:
    - odczytuje LOT, EAN, datę ważności z tekstu OCR
    - odczytuje kody kreskowe z obrazu (jeśli dostępny)
    - porównuje: czy nadrukowane dane = zakodowane w barcode
    Zwraca dict z: {ok, issues, details, lot, ean, exp_date, barcode_gs1}
    """
    import re, base64
    from io import BytesIO

    result = {
        "ok": True,
        "issues": [],
        "details": [],
        "lot":        None,
        "ean":        None,
        "exp_date":   None,
        "barcode_gs1": None,
    }

    # ── 1. Wyciągnij dane z tekstu OCR ────────────────────────────────────────
    # LOT
    m = re.search(r"(?:LOT|Lot|Nr\s+serii|Batch)[:\s#/]*([A-Z0-9\-]{4,20})", ocr_text, re.I)
    ocr_lot = m.group(1).strip() if m else None

    # EAN-13 / UPC-A / EAN-8
    eans = re.findall(r"\b(\d{13}|\d{12}|\d{8})\b", ocr_text)
    ocr_ean = eans[0] if eans else None

    # Data ważności — różne formaty
    exp_patterns = [
        r"(?:EXP|Exp|Expiry|data\s+ważności|use\s+by|BB)[:\s./]*(\d{4}[-./]\d{2}[-./]\d{2})",
        r"(?:EXP|Exp|Expiry|BB)[:\s./]*(\d{2}[-./]\d{2}[-./]\d{4})",
        r"\(17\)(\d{6})",
    ]
    ocr_exp = None
    for pat in exp_patterns:
        m = re.search(pat, ocr_text, re.I)
        if m:
            ocr_exp = m.group(1).strip()
            break

    result["lot"]      = ocr_lot
    result["ean"]      = ocr_ean
    result["exp_date"] = ocr_exp

    # ── 2. Odczytaj kody kreskowe z obrazu ───────────────────────────────────
    barcodes_read = []
    if img_b64 and HAS_PIL:
        try:
            raw = base64.b64decode(img_b64)
            buf = BytesIO(raw)
            img = _PIL_Image.open(buf)
            if img.width * img.height > 50_000_000:  # 50 megapixels max
                img.close()
                _logger.warning(
                    "Barcode image too large (%dx%d), skipping", img.width, img.height
                )
                return {"ok": False, "issues": ["Obraz zbyt duży do analizy kodów kreskowych"],
                        "details": [], "lot": None, "ean": None, "exp_date": None,
                        "barcode_gs1": None, "scan_error": "Image too large"}
            img_rgb = img.convert("RGB")
            img.close()
            barcodes_read = read_barcodes_from_image(img_rgb)
            img_rgb.close()
        except Exception as _e:
            import logging as _log
            _log.getLogger(__name__).warning("deep_validate: image decode failed: %s", _e)
            return {"ok": False, "issues": [f"Nie można odczytać obrazu kodu kreskowego: {str(_e)[:200]}"],
                    "details": [], "lot": None, "ean": None, "exp_date": None,
                    "barcode_gs1": None, "scan_error": str(_e)[:200]}

    # Parsuj GS1 z odczytanych kodów
    gs1_data = None
    for bc in barcodes_read:
        parsed = bc.get("parsed", {})
        if parsed.get("gs1"):
            gs1_data = parsed["gs1"]
            break
        if parsed.get("type") == "EAN-13":
            gs1_data = {"gtin": parsed.get("ean") or bc.get("raw_data"), "lot": None, "exp_date": None}
            break

    result["barcode_gs1"] = gs1_data

    # ── 3. Walidacja EAN-13 z OCR ─────────────────────────────────────────────
    if ocr_ean:
        v = validate_ean13(ocr_ean)
        if v["valid"]:
            result["details"].append({
                "level": "OK",
                "text": f"EAN-13 ({ocr_ean}) — cyfra kontrolna poprawna"
            })
        else:
            result["ok"] = False
            result["issues"].append(f"EAN-13 ({ocr_ean}): {v['error']}")
            result["details"].append({
                "level": "KRYTYCZNY",
                "text": f"EAN-13 ({ocr_ean}): {v['error']}"
            })
    else:
        result["details"].append({"level": "INFO", "text": "Nie znaleziono EAN-13 w tekście OCR"})

    # ── 4. Porównanie OCR vs barcode ──────────────────────────────────────────
    if gs1_data:
        # EAN vs GTIN — compare with zero-padding to 13 digits to avoid
        # false matches from lstrip('0') (e.g. '0123456789012' ≠ '123456789012')
        bc_gtin = _normalize_ean(gs1_data.get("gtin") or "")
        oc_ean  = _normalize_ean(ocr_ean or "")
        if bc_gtin and oc_ean:
            if bc_gtin == oc_ean:
                result["details"].append({
                    "level": "OK",
                    "text": f"EAN barcode ({gs1_data['gtin']}) zgodny z tekstem OCR ({ocr_ean})"
                })
            else:
                result["ok"] = False
                result["issues"].append(f"EAN w barcode ({gs1_data['gtin']}) ≠ EAN w tekście ({ocr_ean})")
                result["details"].append({
                    "level": "KRYTYCZNY",
                    "text": f"EAN w barcode ({gs1_data['gtin']}) ≠ EAN w tekście OCR ({ocr_ean})"
                })

        # LOT
        bc_lot = gs1_data.get("lot")
        if bc_lot and ocr_lot:
            if bc_lot.upper() == ocr_lot.upper():
                result["details"].append({
                    "level": "OK",
                    "text": f"LOT w barcode ({bc_lot}) zgodny z tekstem OCR ({ocr_lot})"
                })
            else:
                result["ok"] = False
                result["issues"].append(f"LOT w barcode ({bc_lot}) ≠ LOT w tekście ({ocr_lot})")
                result["details"].append({
                    "level": "KRYTYCZNY",
                    "text": f"LOT w barcode ({bc_lot}) ≠ LOT w tekście OCR ({ocr_lot})"
                })

        # Data ważności
        bc_exp = gs1_data.get("exp_date")
        if bc_exp and ocr_exp:
            # Porównaj znormalizowane 6 cyfr YYMMDD (ostatnie 6 z ciągu samych cyfr),
            # a nie podciąg w obie strony — ten ostatni dawał fałszywe trafienia
            # (np. "0510" mieści się w wielu datach).
            bc_exp_n = re.sub(r"\D", "", bc_exp)[-6:]
            oc_exp_n = re.sub(r"\D", "", ocr_exp)[-6:]
            if bc_exp_n and oc_exp_n and bc_exp_n == oc_exp_n:
                result["details"].append({
                    "level": "OK",
                    "text": f"Data ważności w barcode ({bc_exp}) zgodna z tekstem OCR ({ocr_exp})"
                })
            else:
                result["ok"] = False
                result["issues"].append(f"Data ważności w barcode ({bc_exp}) ≠ tekst ({ocr_exp})")
                result["details"].append({
                    "level": "KRYTYCZNY",
                    "text": f"Data ważności w barcode ({bc_exp}) ≠ tekst OCR ({ocr_exp})"
                })
    elif barcodes_read:
        result["details"].append({"level": "INFO", "text": "Odczytano kody kreskowe, brak danych GS1-128"})
    else:
        result["details"].append({"level": "INFO", "text": "Nie odczytano kodów kreskowych z obrazu (brak biblioteki zxingcpp/pyzbar)"})

    return result

