"""
normalizer.py — normalizacja liczb, dat i tekstów przed porównaniem.

Rozwiązuje problem: '13,500.00' vs '13500,00' vs '13.500,00' — to ta sama liczba.
Oraz: '30/06/2025' vs '2025-06-30' vs 'June 30, 2025' — ta sama data.
"""

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional
from datetime import date, datetime


# ─── UNICODE NORMALIZATION ───────────────────────────────────────────────────

_UNICODE_DASH_MAP = str.maketrans({'−': '-', '–': '-', '—': '-', '‒': '-', '―': '-'})

def normalize_unicode(s: str) -> str:
    """Replace Unicode minus/dash variants with ASCII hyphen."""
    return s.translate(_UNICODE_DASH_MAP) if s else s


# ─── LICZBY ──────────────────────────────────────────────────────────────────

def normalize_number(s: str) -> Optional[Decimal]:
    """
    Parsuje liczbę z dowolnego formatu do Decimal.
    Obsługuje: 13,500.00 / 13.500,00 / 13 500,00 / 13500.00 / 1.234.567,89
    Zwraca None jeśli nie można sparsować.
    """
    if not s:
        return None
    s = normalize_unicode(str(s).strip())
    if not s:
        return None

    # FIX 1: Handle scientific notation early
    s_stripped = s.strip()
    if re.match(r'^[-+]?\d+\.?\d*[eE][-+]?\d+$', s_stripped):
        try:
            return Decimal(s_stripped)
        except (ValueError, InvalidOperation):
            pass

    # Guard: clearly not a number (too long or looks like a price range)
    if len(s) > 50:
        return None
    # Strip price ranges like "1.234,56-2.345,67"; use [\d.,]* instead of .*
    # so "13500.00 lot-2" is not blocked by the hyphen in trailing metadata.
    if re.search(r'\d[,.]\d[\d.,]*-\d', s):
        return None
    # Usuń symbole walut i spacje. UWAGA: NIE wycinaj 'PLN' jako klasy znaków
    # [PLN] — to usuwało pojedyncze litery P/L/N z liczb/OCR. Tokeny walut (PLN/
    # USD/EUR) zgodnie z kontraktem rozbiera wywołujący (tu zostają → None).
    s = re.sub(r'[€$£¥\s]', '', s)
    if not s or s in ('-', '—', 'n/a', 'N/A'):
        return None

    # Wykryj format: europejski (1.234,56) vs angielski (1,234.56)
    # Liczymy przecinki i kropki
    dots = s.count('.')
    commas = s.count(',')

    # Guard: ambiguous separator counts (e.g. 1.2.3,4.5)
    if dots > 3 or commas > 3:
        return None

    try:
        if dots == 0 and commas == 0:
            # Prosta liczba całkowita
            return Decimal(s)
        elif dots == 1 and commas == 0:
            # Rozróżnienie: 13500.00 (angielski decimal) vs 1.234 (europejski tysiąc)
            # Heurystyka: dokładnie 3 cyfry po kropce + niezerowa część całkowita → tysiące
            _int_p, _frac_p = s.split('.', 1)
            _int_digits = _int_p.lstrip('-')
            if (_int_digits.isdigit() and _int_digits != '0'
                    and len(_frac_p) == 3 and _frac_p.isdigit()):
                return Decimal(s.replace('.', ''))
            return Decimal(s)
        elif dots == 0 and commas == 1:
            # Distinguish thousands separator from decimal comma:
            # '2,500' or '1,234' → integer part non-zero + exactly 3 digits → thousands
            # '0,500' or '13500,00' → decimal comma
            before_comma, after_comma = s.split(',', 1)
            int_part = before_comma.lstrip('-')
            if (int_part.isdigit() and int_part != '0'
                    and len(after_comma) == 3 and after_comma.isdigit()):
                return Decimal(s.replace(',', ''))
            return Decimal(s.replace(',', '.'))
        elif dots > 1 and commas == 0:
            # 1.234.567 — europejski separator tysięcy, brak dziesiętnych
            return Decimal(s.replace('.', ''))
        elif dots == 0 and commas > 1:
            # 1,234,567 — angielski separator tysięcy
            return Decimal(s.replace(',', ''))
        elif dots == 1 and commas >= 1:
            # Sprawdź pozycję: jeśli kropka po ostatnim przecinku → angielski
            if s.rindex('.') > s.rindex(','):
                # 1,234.56 — angielski
                return Decimal(s.replace(',', ''))
            else:
                # 1.234,56 — europejski
                return Decimal(s.replace('.', '').replace(',', '.'))
        elif dots >= 1 and commas == 1:
            # 1.234.567,89 — europejski
            return Decimal(s.replace('.', '').replace(',', '.'))
    except InvalidOperation:
        pass
    return None


def numbers_equal(a: str, b: str, tolerance_pct: float = 0.0) -> Optional[bool]:
    """
    Porównuje dwie liczby z opcjonalną tolerancją procentową.
    Zwraca True/False/None (None = nie można porównać).
    """
    na = normalize_number(a)
    nb = normalize_number(b)
    if na is None or nb is None:
        return None
    if na == nb:
        return True
    # FIX 2: Guard for near-zero values to avoid division-by-zero or misleading ratios
    if na == 0 and nb == 0:
        return True
    if na == 0 or nb == 0:
        # Wąski epsilon: tylko szum zaokrąglenia/parsowania traktujemy jak zero.
        # Szerokie 0,01 zrównywało realne ceny sub-centowe (0,005–0,03 USD) z zerem
        # i maskowało brakującą/zerową cenę jako zgodną z faktyczną wartością.
        return abs(na - nb) <= Decimal('0.0001')  # absolute tolerance for near-zero
    if tolerance_pct > 0:
        diff_pct = abs(na - nb) / max(abs(na), abs(nb)) * 100
        return diff_pct <= tolerance_pct
    return False


def numbers_differ_only_by_rounding(a: str, b: str, decimal_places: int = 2) -> bool:
    """
    True jeśli a i b to ta sama liczba zaokrąglona do różnej liczby miejsc.
    Np. 0.0257 vs 0.03 — różnica tylko zaokrąglenia do 2 miejsc.
    """
    na = normalize_number(a)
    nb = normalize_number(b)
    if na is None or nb is None:
        return False
    decimal_places = max(0, min(10, decimal_places))
    # Zaokrągl obie do decimal_places i porównaj. ROUND_HALF_UP (jak w księgowości/
    # na dokumentach), nie bankierskie round() — inaczej 0.125 vs 0.13 wychodzi „różne".
    factor = Decimal(10) ** decimal_places
    qa = (na * factor).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    qb = (nb * factor).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return qa == qb


def format_number_diff(a: str, b: str) -> str:
    """Zwraca opis różnicy liczbowej: '+300 szt' lub '+5.73 USD'"""
    na = normalize_number(a)
    nb = normalize_number(b)
    if na is None or nb is None:
        return f"{a} → {b}"
    diff = nb - na
    sign = '+' if diff > 0 else ('-' if diff < 0 else '')
    if na == 0:
        return f"{sign}{abs(diff)}"
    pct = float(abs(diff) / abs(na) * 100)
    return f"{sign}{abs(diff)} ({sign}{pct:.1f}%)"


# ─── DATY ────────────────────────────────────────────────────────────────────

# Wzorce dat do sparsowania bez zewnętrznej biblioteki
_DATE_PATTERNS = [
    # ISO i warianty rok-pierwszy — też z jednocyfrowym miesiącem/dniem
    # (2024-1-5) oraz zapis kropkowy rok-pierwszy (2024.01.15), częsty w dokumentach
    # azjatyckich. BUGFIX: wcześniej wymagane było dokładnie \d{2}, więc 2024-1-5,
    # 2024/1/5 i 2024.01.15 zwracały None (gubiona data).
    (r'(\d{4})-(\d{1,2})-(\d{1,2})', lambda m: date(int(m[1]), int(m[2]), int(m[3]))),
    (r'(\d{4})/(\d{1,2})/(\d{1,2})', lambda m: date(int(m[1]), int(m[2]), int(m[3]))),
    (r'(\d{4})\.(\d{1,2})\.(\d{1,2})', lambda m: date(int(m[1]), int(m[2]), int(m[3]))),
    # europejski (dzień-pierwszy): DD.MM.YYYY
    (r'(\d{2})\.(\d{2})\.(\d{4})', lambda m: date(int(m[3]), int(m[2]), int(m[1]))),
    # Ambiguous short form D/M/YYYY or M/D/YYYY (/ or - separator) — disambiguate:
    # • if first group > 12 → must be DD/MM/YYYY (day cannot be a month)
    # • if second group > 12 → must be MM/DD/YYYY (month cannot exceed 12)
    # • if both ≤ 12 → prefer DD/MM/YYYY (European context; ACME documents
    #   are EU-origin trade docs where day-first is the dominant convention)
    (r'(\d{1,2})/(\d{1,2})/(\d{4})', lambda m: (
        _try_date(int(m[3]), int(m[2]), int(m[1]))  # DD/MM/YYYY
        if int(m[1]) > 12
        else (
            _try_date(int(m[3]), int(m[1]), int(m[2]))  # MM/DD/YYYY
            if int(m[2]) > 12
            else _try_date(int(m[3]), int(m[2]), int(m[1]))  # prefer DD/MM/YYYY
        )
    )),
    (r'(\d{1,2})-(\d{1,2})-(\d{4})', lambda m: (
        _try_date(int(m[3]), int(m[2]), int(m[1]))  # DD-MM-YYYY
        if int(m[1]) > 12
        else (
            _try_date(int(m[3]), int(m[1]), int(m[2]))  # MM-DD-YYYY
            if int(m[2]) > 12
            else _try_date(int(m[3]), int(m[2]), int(m[1]))  # prefer DD-MM-YYYY
        )
    )),
    # z nazwą miesiąca
    (r'(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{4})',
     lambda m: _try_date(int(m[3]), _MONTHS_EN.get(m[2][:3].title(), 1), int(m[1]))),
    (r'(\d{1,2})\s+(Sty|Lut|Mar|Kwi|Maj|Cze|Lip|Sie|Wrz|Paź|Lis|Gru)[a-z]*\s+(\d{4})',
     lambda m: _try_date(int(m[3]), _MONTHS_PL.get(m[2][:3].title(), 1), int(m[1]))),
]

_MONTHS_EN = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,
              'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}
_MONTHS_PL = {'Sty':1,'Lut':2,'Mar':3,'Kwi':4,'Maj':5,'Cze':6,
              'Lip':7,'Sie':8,'Wrz':9,'Paź':10,'Lis':11,'Gru':12}

def _try_date(y, m, d):
    try:
        y, m, d = int(y), int(m), int(d)
        if not (1 <= m <= 12) or not (1 <= d <= 31):
            return None
        return date(y, m, d)
    except (ValueError, TypeError, OverflowError):
        return None


def normalize_date(s: str) -> Optional[date]:
    """Parsuje datę z dowolnego formatu do date."""
    if not s:
        return None
    s = str(s).strip()

    # Spróbuj wzorców wbudowanych
    for pattern, parser in _DATE_PATTERNS:
        m = re.search(pattern, s, re.IGNORECASE)
        if m:
            try:
                result = parser(m)
                # FIX 3: Reject obviously invalid dates
                if result:
                    if not (1990 <= result.year <= 2100):
                        continue
                    if not (1 <= result.month <= 12):
                        continue
                    if not (1 <= result.day <= 31):
                        continue
                    return result
            except Exception:
                continue

    # Fallback: dateparser (jeśli dostępny)
    try:
        import dateparser
        result = dateparser.parse(s, languages=['pl', 'en', 'zh'],
                                   settings={'PREFER_DAY_OF_MONTH': 'first',
                                             'RETURN_AS_TIMEZONE_AWARE': False})
        if result:
            d = result.date()
            # FIX 3: Validate dateparser result too
            if 1990 <= d.year <= 2100:
                return d
    except ImportError:
        # Log once — silent fallback hid a missing optional dependency that
        # otherwise widens supported date formats.
        if not getattr(normalize_date, "_dp_warned", False):
            normalize_date._dp_warned = True
            import logging
            logging.getLogger("normalizer").warning(
                "Pakiet 'dateparser' niezainstalowany — fallback parsowania "
                "nietypowych dat wyłączony; zainstaluj 'dateparser' dla pełnej obsługi."
            )

    return None


def date_candidates(s: str) -> set:
    """Zbiór możliwych dat dla zapisu — uwzględnia dwuznaczność D/M vs M/D
    przy formacie liczbowym ze '/' lub '-' (gdy obie grupy ≤ 12).
    Dla zapisów jednoznacznych (ISO, DD.MM.RRRR, z nazwą miesiąca) zwraca
    pojedynczą datę."""
    out = set()
    if not s:
        return out
    primary = normalize_date(s)
    if primary:
        out.add(primary)
    # Dla liczbowego D/M/RRRR lub D-M-RRRR dołóż interpretację z zamianą dzień↔miesiąc
    m = re.match(r'^\s*(\d{1,2})[/-](\d{1,2})[/-](\d{4})\s*$', str(s).strip())
    if m:
        g1, g2, y = int(m[1]), int(m[2]), int(m[3])
        if 1990 <= y <= 2100:
            for mo, da in ((g2, g1), (g1, g2)):
                try:
                    out.add(date(y, mo, da))
                except ValueError:
                    pass
    return out


def dates_possibly_equal(a: str, b: str) -> bool:
    """True, gdy a i b MOGĄ oznaczać tę samą datę — uwzględnia dwuznaczność
    D/M vs M/D w zapisie liczbowym. Pozwala odróżnić realną różnicę dat od
    rozbieżności samego formatu (np. dostawcy US/CN z zapisem MM/DD)."""
    ca, cb = date_candidates(a), date_candidates(b)
    return bool(ca and cb and (ca & cb))


# ─── TEKST / REF KODY ────────────────────────────────────────────────────────

def normalize_ref(s: str) -> str:
    """
    Normalizuje numer referencyjny produktu: usuwa separatory i ujednolica
    wielkość liter. NIE modyfikuje cyfr — zera są ZNACZĄCE na każdej pozycji
    (S040 ≠ S40, QX0510 ≠ QX510, AB012CD ≠ AB12CD), bo rozróżniają
    produkty/rozmiary.
    NL753-S-40 = NL753S40 = nl753 s 40
    """
    if not s:
        return ''
    # Normalize all Unicode dashes/hyphens to ASCII hyphen before stripping
    s = normalize_unicode(str(s))
    # Usuń separatory, zamień na wielkie litery (cyfr nie ruszamy)
    return re.sub(r'[\s\-_/]', '', s.upper().strip())


def refs_match(a: str, b: str, fuzzy_threshold: int = 85) -> tuple[bool, float]:
    """
    Porównuje dwa kody REF z normalizacją i fuzzy matching.
    Zwraca (match: bool, confidence: float 0-1)
    """
    if not a or not b:
        return False, 0.0

    # Dokładne dopasowanie po normalizacji
    na = normalize_ref(a)
    nb = normalize_ref(b)
    if na == nb:
        return True, 1.0

    # Zera są ZNACZĄCE: jeśli kody różnią się WYŁĄCZNIE obecnością/liczbą zer
    # (na początku, w środku lub na końcu), to RÓŻNE kody — blokujemy zanim fuzzy
    # je zrówna (S040 vs S40, QX0510 vs QX510, AB012CD vs AB12CD). na != nb jest
    # tu zagwarantowane, więc równość po usunięciu zer ⇒ różnica to tylko zera.
    if na.replace('0', '') == nb.replace('0', ''):
        return False, 0.0

    # Warianty ROZMIARU: jeśli kody mają identyczny „korpus", a różnią się tylko
    # końcowym segmentem numerycznym (np. NL753S40 vs NL753S45), to RÓŻNE produkty
    # (różne rozmiary) — nie wolno ich łączyć fuzzy mimo wysokiego podobieństwa.
    _ma = re.search(r'(\d+)$', na)
    _mb = re.search(r'(\d+)$', nb)
    if _ma and _mb and _ma.group(1) != _mb.group(1) \
            and na[:_ma.start()] == nb[:_mb.start()]:
        return False, 0.0

    # Fuzzy matching
    try:
        from rapidfuzz import fuzz
        score = fuzz.token_sort_ratio(na, nb)
        if score >= fuzzy_threshold:
            return True, score / 100.0
    except ImportError:
        pass

    return False, 0.0


def normalize_company_name(s: str) -> str:
    """
    Normalizuje nazwę firmy.
    'ACME SP. Z O.O.' = 'ACME SP Z O O'
    """
    if not s:
        return ''
    s = str(s).upper().strip()
    # FIX 4: Normalize multiple spaces before doing anything else
    s = re.sub(r'\s+', ' ', s)
    # Usuń interpunkcję
    s = re.sub(r'[.,;:\-]', ' ', s)
    # Usuń wielokrotne spacje (again after punctuation removal)
    s = re.sub(r'\s+', ' ', s).strip()
    # Normalizuj formy prawne
    # Uwaga: interpunkcja jest już usunięta powyżej, więc klucze z kropkami
    # (np. 'SP. Z O.O.') nigdy by nie pasowały — używamy wersji bez kropek.
    replacements = {
        'SP Z O O': 'SPZOO', 'SP ZO O': 'SPZOO',
        'S A': 'SA',
        'LIMITED': 'LTD',
        'CO KG': 'COKG',
    }
    for old, new in replacements.items():
        s = s.replace(old, new)
    return s


# ─── WALIDACJA SUM KONTROLNYCH ───────────────────────────────────────────────

def validate_checksum(items: list[dict], declared_total: str,
                       qty_key: str = 'qty', price_key: str = 'price',
                       total_key: str = 'net', tolerance_pct: float = 0.1) -> dict:
    """
    Waliduje sumę kontrolną: sum(qty × price) == total.
    Zwraca słownik z wynikiem walidacji.
    """
    result = {
        'valid': None,
        'calculated': None,
        'declared': None,
        'diff': None,
        'diff_pct': None,
        'error': None,
    }

    # Sparsuj total deklarowany
    declared = normalize_number(declared_total)
    if declared is None:
        result['error'] = f"Nie można sparsować sumy deklarowanej: '{declared_total}'"
        return result
    result['declared'] = float(declared)

    # Oblicz sumę z pozycji
    calculated = Decimal('0')
    problematic_items = []

    for item in items:
        # Spróbuj qty × price
        qty_str = str(item.get(qty_key, '') or '')
        price_str = str(item.get(price_key, '') or '')
        net_str = str(item.get(total_key, '') or '')

        qty = normalize_number(qty_str)
        price = normalize_number(price_str)
        net = normalize_number(net_str)

        if net is not None:
            # Użyj pola net bezpośrednio
            calculated += net
        elif qty is not None and price is not None:
            calculated += qty * price
        else:
            problematic_items.append(item.get('ref', '?'))

    result['calculated'] = float(calculated)
    diff = abs(calculated - declared)
    result['diff'] = float(diff)

    # FIX 5: ZeroDivisionError guard — use abs() check before dividing
    if declared and abs(declared) > Decimal('0.001'):
        result['diff_pct'] = float(diff / abs(declared) * 100)
        # Tolerancja domyślnie 0.1% (zaokrąglenia kursowe); konfigurowalna per dostawca.
        result['valid'] = result['diff_pct'] <= tolerance_pct
    else:
        # declared == 0: percentage difference is undefined; set to 0 when sums
        # match, None otherwise so callers can always key-access diff_pct safely.
        result['diff_pct'] = 0.0 if calculated == declared else None
        result['valid'] = calculated == declared

    if problematic_items:
        result['error'] = f"Nie można obliczyć dla: {', '.join(problematic_items[:5])}"

    return result


# ─── PŁATNOŚCI / INCOTERMS ───────────────────────────────────────────────────

# Mapowanie wewnętrznych kodów SAP na czytelne wartości
PAYMENT_TERMS_MAP = {
    'IW04': '30 days', 'IW07': '60 days', 'IW30': '30 days after BL',
    'IW60': '60 days after BL', 'IW90': '90 days',
    'NT30': '30 days net', 'NT60': '60 days net', 'NT90': '90 days net',
    'N030': '30 days', 'N060': '60 days',
    '0001': '14 days 2% / 30 days net',
    'ZB30': '30 days', 'ZB60': '60 days',
}

def normalize_payment_terms(s: str, extra_map: dict = None) -> str:
    """Normalizuje warunki płatności."""
    if not s:
        return ''
    s = str(s).strip()
    combined = {**PAYMENT_TERMS_MAP, **(extra_map or {})}
    # Bezpośrednie dopasowanie
    if s in combined:
        return combined[s]
    s_upper = s.upper()
    if s_upper in combined:
        return combined[s_upper]
    # Wyciągnij liczbę dni z tekstu
    m = re.search(r'(\d+)\s*days?', s, re.IGNORECASE)
    if m:
        return f"{m.group(1)} days"
    return s


def payment_terms_equal(a: str, b: str, extra_map: dict = None) -> bool:
    """Porównuje warunki płatności po normalizacji."""
    na = normalize_payment_terms(a, extra_map)
    nb = normalize_payment_terms(b, extra_map)
    # Porównaj pełne znormalizowane łańcuchy — nie wystarczy zgodność samej
    # liczby dni, bo '30 days' i '30 days after BL' / '30 net' to inne terminy.
    return na.strip().lower() == nb.strip().lower()


def payment_text_to_codes(text: str, extra_map: dict = None) -> list:
    """Odwrotne mapowanie terminu płatności → kody SAP.

    Dla tekstowego terminu (np. '30 days') zwraca listę kodów SAP, których
    znormalizowana wartość jest identyczna (np. ['IW04', 'N030', 'ZB30']).
    Uzupełnia jednokierunkowe PAYMENT_TERMS_MAP (kod → tekst). Zwraca [] gdy
    brak dopasowania.
    """
    if not text:
        return []
    combined = {**PAYMENT_TERMS_MAP, **(extra_map or {})}
    t = str(text).strip().lower()
    # 1. Dokładne (case-insensitive) dopasowanie do wartości mapy — zachowuje
    #    kwalifikatory ("30 days after BL" → IW30, nie kody "30 days").
    exact = {code for code, val in combined.items() if str(val).strip().lower() == t}
    if exact:
        return sorted(exact)
    # 2. Wejście jest już kodem SAP.
    if str(text).strip().upper() in combined:
        return [str(text).strip().upper()]
    # 3. Fallback po liczbie dni (gdy brak dokładnego dopasowania).
    norm = normalize_payment_terms(text, extra_map).strip().lower()
    return sorted({code for code, val in combined.items()
                   if str(val).strip().lower() == norm})
