"""
artwork_naming.py — parser nazw plików masterów artworków ACME.

Nazwa pliku ACME koduje: REF produktu + typ opakowania + (czasem) rewizję i EAN,
np. ``GS1710123-SS_carton_sticker.pdf`` → REF ``GS1710123-SS``, typ ``carton sticker``.
``805016_pouch_2 Rev.00_5900010800810.pdf`` → REF ``805016``, typ ``pouch``,
rewizja ``Rev.00``, EAN ``5900010800810``.

Moduł jest czysty (bez zależności) i pokryty testami — używany przez bulk-upload
masterów oraz przez lokalny uploader.
"""

import os
import re

# Słownik typów opakowań (kontrolowany). Kolejność = od najdłuższych dopasowań,
# żeby "carton sticker" złapało się przed "carton": przy równym indeksie wygrywa
# pierwszy wpis z listy (porównanie `idx < cut` jest ostre).
#
# Warianty z myślnikiem ("carton-sticker") to zapis z docelowej konwencji nazw —
# bez nich taka nazwa czytała się jako zwykły "carton", po cichu i bez błędu.
# Wszystkie warianty sprowadzamy do jednej formy kanonicznej ze spacją
# ("carton sticker"), żeby nie rozbić grup REF+opakowanie w bibliotece.
PACKAGING_TYPES = [
    "carton sticker", "carton_sticker", "carton-sticker",
    "carton print", "carton_print", "carton-print",
    "box print", "box_print", "box-print",
    "pouch", "bag", "box", "carton", "sticker", "print",
    "label", "etykieta", "tyvek", "blister", "insert", "ulotka",
    "instrukcja", "ifu",
]

# Zapis pola EAN dla elementów bez własnego kodu (ulotka, tyvek, insert).
NO_EAN_TOKEN = "noEAN"

# Poziomy opakowania dopuszczone w nazwie pliku wg konwencji — forma kanoniczna
# używana w nazwach to zapis z myślnikiem, bez spacji.
ALLOWED_LEVELS = (
    "pouch", "bag", "box", "carton",
    "carton-sticker", "carton-print", "box-print",
    "sticker", "label", "ifu", "insert", "tyvek", "blister",
)

# "bag" to zbyt pospolite słowo, żeby ufać mu w środku opisu produktu — w bazie
# są nazwy w rodzaju "Vomit Bag label" czy "Silicone Reservoir Bag Pouch Medium",
# gdzie Bag jest częścią nazwy wyrobu, a poziomem opakowania jest label/pouch.
# Dlatego ten token uznajemy tylko na granicy pola: na początku nazwy albo po
# podkreśleniu. Pozostałe tokeny szukane są zwykłym podciągiem (jak dotąd).
_STRICT_BOUNDARY_TYPES = frozenset({"bag"})
_BOUNDARY_TPL = r'(?:^|_)\s*({})(?=[_\s.]|$)'

_EAN_RE = re.compile(r'(\d{13})')


def _find_packaging_token(low: str, token: str) -> int:
    """Pozycja tokenu typu opakowania w nazwie (małe litery) albo -1."""
    if token in _STRICT_BOUNDARY_TYPES:
        m = re.search(_BOUNDARY_TPL.format(re.escape(token)), low)
        return m.start(1) if m else -1
    return low.find(token)


def _ean13_ok(code: str) -> bool:
    """Sprawdza sumę kontrolną EAN-13 — odróżnia prawdziwy EAN od przypadkowego
    13-cyfrowego ciągu (np. numeru zamówienia/telefonu) w nazwie pliku."""
    if not (code.isdigit() and len(code) == 13):
        return False
    d = [int(c) for c in code]
    chk = (10 - (sum(d[i] * (1 if i % 2 == 0 else 3) for i in range(12)) % 10)) % 10
    return chk == d[12]
# Rewizja: Rev.02 / Rev 2 / v3 / Ed.1 / Wyd.III (rzymskie). Bez końcowego \b,
# bo po numerze często stoi '_' (np. "Rev.00_5900010800810") i \b by zawiódł.
# Uwaga: gałąź samego 'v<n>' wymaga POPRZEDZAJĄCEJ SPACJI (?<=\s), a nie tylko
# granicy słowa \b — inaczej wariant kodu produktu po myślniku (np. "NL753-V40")
# zostałby błędnie zinterpretowany jako rewizja "V40", korumpując REF i ranking.
_REV_RE = re.compile(
    r'(?:(?:(?<=[\s_])|^)rev\.?\s*(\d+)'
    r'|(?<=\s)v\.?\s*(\d+)'
    r'|(?:(?<=[\s_])|^)ed\.?\s*(\d+)'
    r'|(?:(?<=[\s_])|^)wyd\.?\s*([ivxlcdm]+))',
    re.IGNORECASE,
)
# Pozostałe „śmieci" do wycięcia z REF: liczniki kopii "(2)", luźne wersje
_NOISE_RE = re.compile(r'\(\d+\)|_\d+\s*$')


def _roman_to_int(s: str) -> int:
    vals = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    s = s.lower()
    total, prev = 0, 0
    for ch in reversed(s):
        v = vals.get(ch, 0)
        if v < prev:
            total -= v
        else:
            total += v
            prev = v
    return total


def parse_master_filename(filename: str) -> dict:
    """Rozkłada nazwę pliku mastera na składniki.

    Zwraca dict: ref, packaging_type, revision (surowy token albo ''),
    revision_rank (int do wyboru najnowszej; 0 gdy brak), ean.
    """
    stem = os.path.splitext(os.path.basename(filename or ""))[0]

    # EAN (13 cyfr z poprawną sumą kontrolną) — wytnij z dalszego przetwarzania.
    # Bierzemy pierwszy ciąg, który faktycznie jest EAN-em, a nie dowolne 13 cyfr
    # (numer zamówienia/telefonu nie zostanie błędnie potraktowany jako EAN/REF).
    ean = ""
    for m in _EAN_RE.finditer(stem):
        if _ean13_ok(m.group(1)):
            ean = m.group(1)
            stem = stem[:m.start(1)] + " " + stem[m.end(1):]
            break

    # Rewizja
    revision = ""
    revision_rank = 0
    mr = _REV_RE.search(stem)
    if mr:
        revision = mr.group(0).strip()
        num = mr.group(1) or mr.group(2) or mr.group(3)
        if num:
            revision_rank = int(num)
        elif mr.group(4):
            revision_rank = _roman_to_int(mr.group(4))
        stem = stem[:mr.start()] + " " + stem[mr.end():]

    # Typ opakowania — REF to wszystko PRZED tokenem typu
    low = stem.lower()
    packaging_type = ""
    cut = len(stem)
    for t in PACKAGING_TYPES:
        idx = _find_packaging_token(low, t)
        if idx != -1 and idx < cut:
            # Forma kanoniczna: "carton_sticker" i "carton-sticker" → "carton sticker".
            packaging_type = t.replace("_", " ").replace("-", " ")
            cut = idx
    if packaging_type:
        stem = stem[:cut]

    # Czyszczenie REF
    stem = _NOISE_RE.sub(" ", stem)
    ref = re.sub(r'[_\s]+', " ", stem).strip(" _-")

    return {
        "ref": ref,
        "packaging_type": packaging_type,
        "revision": revision,
        "revision_rank": revision_rank,
        "ean": ean,
    }


def revision_sort_key(parsed: dict, last_write_time: str = "") -> tuple:
    """Klucz sortowania do wyboru AKTUALNEJ rewizji w grupie REF+opakowanie.

    Wybrana decyzja: najwyższy znacznik Rev w nazwie, a przy braku — najnowsza
    data modyfikacji pliku (fallback). Większy klucz = nowsza rewizja.
    """
    return (parsed.get("revision_rank", 0), last_write_time or "")


def format_master_label(filename: str) -> str:
    """Czytelna etykieta modelu: 'REF – opakowanie – Rev.NN'. Rewizję pomija, gdy
    brak jawnego znacznika w nazwie; opakowanie pomija, gdy nierozpoznane.
    Przykłady: '700001_carton Rev.02.pdf' → '700001 – carton – Rev.02';
    'QY17050820-SS_box.pdf' → 'QY17050820-SS – box'."""
    p = parse_master_filename(filename)
    parts = [p["ref"] or os.path.splitext(os.path.basename(filename or ""))[0]]
    if p["packaging_type"]:
        parts.append(p["packaging_type"])
    if p["revision"]:
        parts.append(p["revision"])
    return " – ".join(parts)


def pick_current_revisions(files):
    """Z listy plików biblioteki wybiera AKTUALNĄ rewizję per (REF, opakowanie).

    files: iterowalne dictów z kluczami co najmniej: 'filename', 'modified_at'
    (dowolne inne klucze — np. 'id', 'z_path' — są przenoszone bez zmian).

    Zwraca listę zwycięzców (po jednym na grupę REF+opakowanie), wybranych przez
    revision_sort_key = (revision_rank z nazwy, modified_at). Pliki bez rozpoznanego
    REF-u są pomijane (szum: foldery robocze, podglądy bez kodu). Do każdego zwycięzcy
    dokleja pola 'ref', 'packaging_type', 'revision_rank' z parsera.
    """
    best = {}
    for f in files:
        parsed = parse_master_filename(f.get("filename", ""))
        ref = parsed["ref"]
        if not ref:
            continue
        group = (ref.lower(), parsed["packaging_type"].lower())
        key = revision_sort_key(parsed, f.get("modified_at", "") or "")
        cur = best.get(group)
        if cur is None or key > cur[0]:
            enriched = dict(f)
            enriched.update(ref=ref, packaging_type=parsed["packaging_type"],
                            revision_rank=parsed["revision_rank"])
            best[group] = (key, enriched)
    return [v[1] for v in best.values()]


# ---------------------------------------------------------------------------
# Walidator konwencji nazw (doradczy)
#
# Konwencja docelowa:  REF_poziom_EAN_Rev.NN_RRRR-MM-DD.pdf
# np.                  805016_pouch_5900010800810_Rev.02_2026-05-14.pdf
#
# Pól jest zawsze pięć; dostawca może dopisać własne oznaczenie po piątym polu
# (kolejne podkreślenie). Element bez własnego kodu ma w polu EAN "noEAN".
#
# Walidator NIE blokuje wgrania pliku — nazywa odstępstwa, żeby dało się je
# wyłapać przy wgraniu zamiast pół roku później w bibliotece.
# ---------------------------------------------------------------------------

# Dozwolone znaki w nazwie (bez rozszerzenia): litery ASCII, cyfry, _ - .
_ALLOWED_CHARS_RE = re.compile(r'[^A-Za-z0-9_.-]')
_REV_STRICT_RE = re.compile(r'^Rev\.\d{2}$')
_DATE_STRICT_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})$')

CONVENTION_TEMPLATE = "REF_poziom_EAN_Rev.NN_RRRR-MM-DD.pdf"
CONVENTION_EXAMPLE = "805016_pouch_5900010800810_Rev.02_2026-05-14.pdf"


def _describe_char(ch: str) -> str:
    """Opis znaku do komunikatu — z kodem, bo część z nich jest niewidoczna."""
    import unicodedata
    name = unicodedata.name(ch, "")
    shown = ch if ch.isprintable() and not ch.isspace() else " "
    label = f"{shown!r} U+{ord(ch):04X}"
    return f"{label} ({name.lower()})" if name else label


def validate_master_filename(filename: str) -> dict:
    """Sprawdza nazwę pliku mastera względem konwencji nazewniczej ACME.

    Zwraca dict:
      ok       — bool, True gdy nazwa jest w pełni zgodna z konwencją,
      problems — lista dictów {'code', 'msg'} (pusta gdy ok),
      fields   — odczytane pola {'ref','level','ean','revision','date','suffix'}.

    Funkcja jest czysta i nie rzuca wyjątków — dowolny śmieć na wejściu daje
    listę problemów, nigdy błędu.
    """
    problems = []
    add = lambda code, msg: problems.append({"code": code, "msg": msg})  # noqa: E731

    base = os.path.basename(filename or "")
    stem, ext = os.path.splitext(base)
    fields = {"ref": "", "level": "", "ean": "", "revision": "", "date": "", "suffix": ""}

    if not stem:
        add("empty", "Pusta nazwa pliku.")
        return {"ok": False, "problems": problems, "fields": fields}

    if ext.lower() != ".pdf":
        add("ext", f"Rozszerzenie {ext or '(brak)'} — oczekiwane .pdf")

    bad = sorted(set(_ALLOWED_CHARS_RE.findall(stem)))
    if bad:
        add("chars", "Niedozwolone znaki w nazwie: "
                     + ", ".join(_describe_char(c) for c in bad)
                     + ". Dozwolone są tylko litery A-Z, cyfry, _ oraz -")

    parts = stem.split("_")
    if len(parts) < 5:
        add("fields", f"Nazwa ma {len(parts)} pól, konwencja wymaga 5: {CONVENTION_TEMPLATE}")
        return {"ok": False, "problems": problems, "fields": fields}

    ref, level, ean, revision, date = parts[:5]
    suffix = "_".join(parts[5:])
    fields.update(ref=ref, level=level, ean=ean, revision=revision, date=date, suffix=suffix)

    if not ref.strip():
        add("ref", "Pole 1 (REF) jest puste.")

    if level not in ALLOWED_LEVELS:
        add("level", f"Pole 2 (poziom) = {level!r} — poza zamkniętą listą: "
                     + ", ".join(ALLOWED_LEVELS))

    if ean != NO_EAN_TOKEN:
        if not (len(ean) == 13 and ean.isdigit()):
            add("ean", f"Pole 3 (EAN) = {ean!r} — oczekiwane 13 cyfr albo {NO_EAN_TOKEN}")
        elif not _ean13_ok(ean):
            add("ean_checksum", f"Pole 3 (EAN) = {ean} — błędna suma kontrolna EAN-13. "
                                "Kod przepisz z tabelki wewnątrz artworku.")

    if not _REV_STRICT_RE.match(revision):
        add("revision", f"Pole 4 (rewizja) = {revision!r} — oczekiwane Rev.NN (dwie cyfry)")

    m = _DATE_STRICT_RE.match(date)
    if not m:
        add("date", f"Pole 5 (data) = {date!r} — oczekiwany format RRRR-MM-DD")
    else:
        import datetime as _dt
        try:
            _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            add("date", f"Pole 5 (data) = {date!r} — nie jest poprawną datą")

    return {"ok": not problems, "problems": problems, "fields": fields}
