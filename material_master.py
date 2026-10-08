"""
material_master.py — master data materiałów ACME.

Pola: ref_code, opis_pl, opis_en, ean, rodzina, base_uom (podstawowa jednostka
miary), producer_code (kod producenta), oraz per-poziom opakowania (sztuka/op/
opz/karton/paz/ppa): wymiar, ean, artwork_ref, qty_base (ile podstawowych JM
mieści poziom = przelicznik).

Import jest TOLERANCYJNY na format:
- jednowierszowy nagłówek z nazwami maszynowymi (stary szablon), albo
- dwuwierszowy: wiersz 1 = banner poziomów (SZT/OP/OPZ/KAR/PAZ/PPA, scalony),
  wiersz 2 = nazwy pól (ref_code, „Podstawowa jednostka miary”, „Ilość
  podstawowej jednostki miary”, sztuka_wymiar, ...).

Przeliczniki (qty_base per poziom) są automatycznie zapisywane do tabeli
uom_conversion (#4 auto-przeliczanie ilości).

Moduł „czysty” — operuje na połączeniu DB przekazanym z app.py.
"""
from __future__ import annotations

import json
import re

# Poziomy mające wymiar/ean/artwork.
LEVELS = ("sztuka", "op", "opz", "karton")
# Wszystkie poziomy mogące mieć przelicznik (qty_base) — w tym PAZ i PPA (półpaleta).
CONV_LEVELS = ("sztuka", "op", "opz", "karton", "paz", "ppa")

# Kod poziomu (banner / jednostka opakowania) → klucz wewnętrzny.
_BANNER_LEVEL = {"SZT": "sztuka", "OP": "op", "OPZ": "opz", "KAR": "karton",
                 "PAZ": "paz", "PPA": "ppa"}
# Jednostka UOM reprezentująca dany poziom (do uom_conversion).
_LEVEL_UNIT = {"sztuka": "SZT", "op": "OP", "opz": "OPZ", "karton": "KAR",
               "paz": "PAZ", "ppa": "PPA"}


def ensure_table(db) -> None:
    """Tworzy/uzupełnia tabelę material_master (idempotentnie).

    Memoizacja per-połączenie: na PostgreSQL ciężkie DDL (CREATE + 5× ALTER z
    rollbackiem przy istniejącej kolumnie + commity) wykonują się raz na żądanie,
    nie przy każdym get_material w pętli. Na SQLite setattr się nie powiedzie
    (połączenie nie przyjmuje atrybutów) → zachowanie bez zmian (ensure za każdym
    razem, ale tanie lokalnie)."""
    if getattr(db, "_mm_ensured", False):
        return
    db.execute("""CREATE TABLE IF NOT EXISTS material_master (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ref_code TEXT UNIQUE NOT NULL,
        ref_norm TEXT DEFAULT '',
        opis_pl TEXT DEFAULT '',
        opis_en TEXT DEFAULT '',
        ean TEXT DEFAULT '',
        rodzina TEXT DEFAULT '',
        grupa TEXT DEFAULT '',
        podgrupa TEXT DEFAULT '',
        base_uom TEXT DEFAULT '',
        producer_code TEXT DEFAULT '',
        tariff_cn TEXT DEFAULT '',
        supplier_codes TEXT DEFAULT '',
        txt_short_pl TEXT DEFAULT '',
        customs_code TEXT DEFAULT '',
        vat_rate TEXT DEFAULT '',
        sent INTEGER DEFAULT 0,
        levels_json TEXT DEFAULT '{}',
        active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    )""")
    db.commit()
    # Dla istniejących tabel — dodaj nowe kolumny (ignoruj jeśli już są).
    # WAŻNE (PostgreSQL): nieudany ALTER psuje transakcję → po błędzie rollback,
    # po sukcesie commit, żeby kolejne polecenia nie padały na 'transaction aborted'.
    for _col in ("base_uom TEXT DEFAULT ''", "producer_code TEXT DEFAULT ''",
                 "tariff_cn TEXT DEFAULT ''", "supplier_codes TEXT DEFAULT ''",
                 "txt_short_pl TEXT DEFAULT ''", "active INTEGER DEFAULT 1",
                 "customs_code TEXT DEFAULT ''",
                 "vat_rate TEXT DEFAULT ''", "sent INTEGER DEFAULT 0",
                 "grupa TEXT DEFAULT ''", "podgrupa TEXT DEFAULT ''"):
        try:
            db.execute(f"ALTER TABLE material_master ADD COLUMN {_col}")
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
    db.execute("CREATE INDEX IF NOT EXISTS idx_material_master_norm ON material_master(ref_norm)")
    # Grupa/podgrupa produktowa — po nich uogólniamy wiedzę zebraną na pojedynczych REF-ach
    # (np. wyuczoną orientację 3D) na cały asortyment o tym samym charakterze.
    try:
        db.execute("CREATE INDEX IF NOT EXISTS idx_material_master_grupa "
                   "ON material_master(grupa, podgrupa)")
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    # Gwarancja UNIQUE na ref_code także dla starych tabel — bez tego ON CONFLICT(ref_code)
    # przy upsert rzuca na PG „no unique or exclusion constraint matching".
    try:
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_material_master_ref ON material_master(ref_code)")
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    db.commit()
    try:
        db._mm_ensured = True
    except (AttributeError, TypeError):
        pass


def normalize_ref(raw) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(raw or "").upper())


def _norm_hdr(s) -> str:
    """Normalizuje nazwę nagłówka: lower, zwinięte spacje, bez kropek."""
    return re.sub(r"\s+", " ", str(s or "").strip().lower()).replace(".", "")


def _classify(field_hdr: str, banner: str):
    """Zwraca ('global', nazwa) | ('level', (poziom, podpole)) | (None, None)."""
    f = _norm_hdr(field_hdr)
    b = str(banner or "").strip().upper()
    lvl_banner = _BANNER_LEVEL.get(b)
    if not f:
        return (None, None)
    # Pola globalne
    if f in ("ref_code", "ref", "indeks", "kod", "kod_produktu"):
        return ("global", "ref_code")
    if f in ("opis_pl", "opis polski", "opis pl"):
        return ("global", "opis_pl")
    if f in ("opis_en", "opis angielski", "opis en"):
        return ("global", "opis_en")
    if f in ("rodzina", "grupa", "rodzina/grupa"):
        return ("global", "rodzina")
    # Grupa/podgrupa PRODUKTOWA — osobno od 'rodzina'. Gołe "grupa" celowo zostaje przy
    # rodzinie (tak działały dotychczasowe importy); nowe pola wymagają jawnego nagłówka.
    if f in ("grupa produktowa", "grupa_produktowa", "grupa produktu", "product group"):
        return ("global", "grupa")
    if f in ("podgrupa", "podgrupa produktowa", "podgrupa_produktowa", "podgrupa produktu",
             "product subgroup", "subgrupa"):
        return ("global", "podgrupa")
    if f == "ean" and not lvl_banner:
        return ("global", "ean")
    if f in ("podstawowa jednostka miary", "jm", "jednostka", "jednostka miary", "podstawowa jm"):
        return ("global", "base_uom")
    if "kod producenta" in f or f in ("producent", "manufacturer", "kod_producenta", "mfr"):
        return ("global", "producer_code")
    if f in ("tariff_cn", "taryfa cn", "cn", "taryfa", "kod cn", "tariff"):
        return ("global", "tariff_cn")
    if f in ("customs_code", "kod celny", "kod celny materialu", "kod celny materiału",
             "kod taryfy celnej", "customs code", "customs"):
        return ("global", "customs_code")
    if f in ("supplier_codes", "dostawcy", "kody dostawcow", "kody dostawców", "dostawca"):
        return ("global", "supplier_codes")
    if f in ("txt_short_pl", "txt short pl", "nazwa artworka", "nazwa skrocona", "txt_short"):
        return ("global", "txt_short_pl")
    if f in ("vat", "vat_rate", "stawka vat", "stawka_vat", "vat %", "vat%", "stawka podatku vat", "podatek vat"):
        return ("global", "vat_rate")
    if f in ("sent", "czy sent", "sent t/n", "sent tak/nie", "nadzor sent", "nadzór sent",
             "monitorowanie sent", "podlega sent"):
        return ("global", "sent")
    # Pola z prefiksem poziomu (sztuka_wymiar, op_ean, karton_artwork_ref ...)
    m = re.match(r"(sztuka|szt|op|opz|karton|kar|paz|ppa)[_ ](wymiar|ean|artwork_ref|artwork)\b", f)
    if m:
        lv = {"szt": "sztuka", "kar": "karton"}.get(m.group(1), m.group(1))
        sub = "artwork_ref" if "artwork" in m.group(2) else m.group(2)
        return ("level", (lv, sub))
    # Przelicznik („Ilość podstawowej jednostki miary [PAZ/PPA]”)
    if "ilosc podstawowej jednostki miary" in f or "ilość podstawowej jednostki miary" in f \
            or f.startswith("przelicznik"):
        if f.endswith("paz") or b == "PAZ":
            return ("level", ("paz", "qty_base"))
        if f.endswith("ppa") or b == "PPA":
            return ("level", ("ppa", "qty_base"))
        if lvl_banner:
            return ("level", (lvl_banner, "qty_base"))
    # Generyczne pola z bannera (wymiar/ean/artwork bez prefiksu)
    if lvl_banner and f in ("wymiar", "wymiary"):
        return ("level", (lvl_banner, "wymiar"))
    if lvl_banner and f == "ean":
        return ("level", (lvl_banner, "ean"))
    if lvl_banner and "artwork" in f:
        return ("level", (lvl_banner, "artwork_ref"))
    return (None, None)


def sent_truthy(v) -> int:
    """Normalizuje wartość SENT z importu/UI do 0/1 (TAK/T/1/X/SENT → 1)."""
    return 1 if str(v or "").strip().lower() in (
        "1", "tak", "t", "yes", "y", "x", "true", "sent", "prawda") else 0


def clean_ean(raw) -> str:
    """Zwraca same cyfry EAN tylko gdy długość jest prawidłowa (8/13/14 = EAN-8/
    EAN-13/GTIN-14). Inaczej zwraca '' — lepiej puste pole niż obcięty, sklejony
    ciąg (np. dwa EAN-y w jednej komórce), który udawałby poprawny kod."""
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits if len(digits) in (8, 13, 14) else ""


def norm_vat(v) -> str:
    """Normalizuje stawkę VAT: '23%'→'23', '8,0'→'8', 'zw'/'np' zachowane."""
    s = str(v or "").strip().lower().replace("%", "").replace(",", ".").strip()
    if s in ("zw", "zw.", "zwolniony", "np", "n/p", "np."):
        return s.replace(".", "")[:10]
    m = re.search(r"\d+(?:\.\d+)?", s)
    if m:
        n = m.group()
        return (n[:-2] if n.endswith(".0") else n)[:10]
    return s[:10]


def _ffill(seq):
    """Forward-fill pustych komórek (scalone bannery)."""
    out, last = [], ""
    for v in seq:
        s = str(v or "").strip()
        if s:
            last = s
        out.append(last)
    return out


def parse_workbook_rows(rows: list) -> list:
    """rows = lista wierszy (list komórek), z nagłówkiem 1- lub 2-wierszowym.
    Zwraca listę rekordów materiału (dict)."""
    # Tylko wiersze będące listą/krotką — str/inny typ rozbiłby się na znaki (list("abc")).
    rows = [list(r) for r in rows if isinstance(r, (list, tuple))]
    if not rows:
        return []
    r0 = [str(c or "").strip() for c in rows[0]]
    banner = None
    hdr_idx = 0
    if any(x.upper().strip() in _BANNER_LEVEL for x in r0):
        banner = _ffill(rows[0])
        hdr_idx = 1
    if hdr_idx >= len(rows):
        return []
    headers = [str(c or "").strip() for c in rows[hdr_idx]]
    colmap = []
    last_level = None  # ostatni poziom widziany w kolumnach (do pozycyjnego qty_base)
    for ci, h in enumerate(headers):
        b = banner[ci] if (banner and ci < len(banner)) else ""
        kind, key = _classify(h, b)
        if kind == "level":
            last_level = key[0]
        elif kind is None and last_level:
            # Gołe „Ilość podstawowej jednostki miary" (bez sufiksu poziomu i bez
            # bannera) → przelicznik qty_base poziomu z poprzedzających kolumn.
            if "podstawowej jednostki miary" in _norm_hdr(h):
                kind, key = "level", (last_level, "qty_base")
        colmap.append((kind, key))

    records = []
    for row in rows[hdr_idx + 1:]:
        glob = {}
        levels = {}
        for ci, (kind, key) in enumerate(colmap):
            if kind is None or ci >= len(row):
                continue
            val = str(row[ci]).strip() if row[ci] is not None else ""
            if not val:
                continue
            if kind == "global":
                glob[key] = val
            else:
                lv, sub = key
                # Ochrona: przelicznik (qty_base) wyglądający jak EAN/SSCC (≥12 cyfr)
                # to błąd w danych (np. EAN wpisany w kolumnie PAZ) — nie zapisuj go,
                # by nie psuł widoku poziomu ani przelicznika palet.
                if sub == "qty_base" and len(re.sub(r"\D", "", val)) >= 12:
                    continue
                levels.setdefault(lv, {})[sub] = val
        ref = glob.get("ref_code", "")
        if not ref:
            continue
        records.append({
            "ref_code": ref[:100],
            "ref_norm": normalize_ref(ref),
            "opis_pl": glob.get("opis_pl", "")[:500],
            "opis_en": glob.get("opis_en", "")[:500],
            "ean": clean_ean(glob.get("ean", "")),
            "rodzina": glob.get("rodzina", "")[:160],
            "grupa": glob.get("grupa", "")[:160],
            "podgrupa": glob.get("podgrupa", "")[:160],
            "base_uom": glob.get("base_uom", "")[:20],
            "producer_code": glob.get("producer_code", "")[:60],
            "tariff_cn": glob.get("tariff_cn", "")[:30],
            "customs_code": glob.get("customs_code", "")[:30],
            "vat_rate": norm_vat(glob.get("vat_rate", "")),
            # None gdy kolumna SENT nieobecna/pusta — żeby import nie zerował ani nie
            # ustawiał na stałe istniejącej flagi; aktualizujemy tylko gdy podano wartość.
            "sent": (sent_truthy(glob["sent"]) if "sent" in glob else None),
            "supplier_codes": glob.get("supplier_codes", "")[:500],
            "txt_short_pl": glob.get("txt_short_pl", "")[:200],
            "levels": levels,
        })
    return records


def parse_row(d: dict):
    """Back-compat: pojedynczy wiersz jako dict {nazwa_kolumny: wartość}."""
    recs = parse_workbook_rows([list(d.keys()), list(d.values())])
    return recs[0] if recs else None


def _save_record(db, rec) -> None:
    db.execute(
        "INSERT INTO material_master(ref_code, ref_norm, opis_pl, opis_en, ean, rodzina, "
        "grupa, podgrupa, "
        "base_uom, producer_code, tariff_cn, supplier_codes, txt_short_pl, customs_code, "
        "vat_rate, sent, levels_json, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now')) "
        "ON CONFLICT(ref_code) DO UPDATE SET ref_norm=excluded.ref_norm, "
        "opis_pl=excluded.opis_pl, opis_en=excluded.opis_en, ean=excluded.ean, "
        "rodzina=excluded.rodzina, "
        # Grupa/podgrupa: nie zeruj istniejącej klasyfikacji importem, który jej nie zawiera.
        "grupa=CASE WHEN excluded.grupa<>'' THEN excluded.grupa ELSE material_master.grupa END, "
        "podgrupa=CASE WHEN excluded.podgrupa<>'' THEN excluded.podgrupa "
        "ELSE material_master.podgrupa END, "
        "base_uom=excluded.base_uom, "
        "producer_code=excluded.producer_code, tariff_cn=excluded.tariff_cn, "
        "supplier_codes=excluded.supplier_codes, txt_short_pl=excluded.txt_short_pl, "
        "customs_code=excluded.customs_code, "
        # VAT/SENT: nadpisuj tylko gdy w imporcie podano wartość (nie zeruj istniejących).
        # Wartość istniejącą referujemy nazwą tabeli (material_master.x) — bez tego
        # PostgreSQL rzuca AmbiguousColumn; działa też na SQLite.
        "vat_rate=CASE WHEN excluded.vat_rate<>'' THEN excluded.vat_rate ELSE material_master.vat_rate END, "
        # SENT: aktualizuj tylko gdy import podał wartość (NULL = nieobecna w źródle);
        # nie nadpisuj ani nie utrwalaj na stałe istniejącej flagi.
        "sent=CASE WHEN excluded.sent IS NOT NULL THEN excluded.sent ELSE material_master.sent END, "
        "levels_json=excluded.levels_json, updated_at=datetime('now')",
        (rec["ref_code"], rec["ref_norm"], rec["opis_pl"], rec["opis_en"], rec["ean"],
         rec["rodzina"], rec.get("grupa", ""), rec.get("podgrupa", ""),
         rec.get("base_uom", ""), rec.get("producer_code", ""),
         rec.get("tariff_cn", ""), rec.get("supplier_codes", ""), rec.get("txt_short_pl", ""),
         rec.get("customs_code", ""), rec.get("vat_rate", ""),
         (int(rec["sent"]) if rec.get("sent") is not None else None),
         json.dumps(rec.get("levels", {}), ensure_ascii=False)),
    )


def _save_conversions(db, rec) -> int:
    """Z przeliczników qty_base tworzy reguły uom_conversion (poziom → base_uom)."""
    base = (rec.get("base_uom") or "").strip()
    if not base:
        return 0
    try:
        import uom
        uom.ensure_table(db)
    except Exception:
        return 0
    n = 0
    for lv, data in (rec.get("levels") or {}).items():
        q = data.get("qty_base")
        if not q:
            continue
        # EAN/SSCC (≥12 cyfr) nie jest przelicznikiem — nie twórz z niego reguły UOM.
        if len(re.sub(r"\D", "", str(q))) >= 12:
            continue
        try:
            factor = float(str(q).replace(",", "."))
        except (TypeError, ValueError):
            continue
        if factor <= 0:
            continue
        unit_from = _LEVEL_UNIT.get(lv, lv.upper())
        # Pomiń regułę poziomu bazowego (np. SZT→SZT factor=1) — to no-op zaśmiecający tabelę.
        if uom.canonical_unit(unit_from) == uom.canonical_unit(base):
            continue
        db.execute(
            "INSERT INTO uom_conversion(ref_norm, unit_from, unit_to, factor) "
            "VALUES(?,?,?,?) ON CONFLICT(ref_norm, unit_from, unit_to) "
            "DO UPDATE SET factor=excluded.factor",
            (rec["ref_norm"], uom.canonical_unit(unit_from), uom.canonical_unit(base), factor),
        )
        n += 1
    return n


def migrate_from_products(db) -> dict:
    """Jednorazowo wchłania tabelę products do material_master (scalenie rejestrów).
    Nie nadpisuje istniejących pól master, gdy źródło puste."""
    ensure_table(db)
    try:
        rows = db.execute(
            "SELECT ref_code, product_name, ean, unit, tariff_cn, description, supplier_codes "
            "FROM products WHERE COALESCE(active,1)=1"
        ).fetchall()
    except Exception:
        return {"migrated": 0, "error": "Brak tabeli products"}
    n = 0
    for r in rows:
        ref = str(r["ref_code"] or "").strip()
        if not ref:
            continue
        ex = get_material(db, ref) or {}
        levels = {}
        if ex.get("levels_json"):
            try:
                levels = json.loads(ex["levels_json"])
            except (ValueError, TypeError):
                levels = {}
        rec = {
            "ref_code": ref[:100],
            "ref_norm": normalize_ref(ref),
            "opis_pl": (ex.get("opis_pl") or str(r["product_name"] or ""))[:500],
            "opis_en": (ex.get("opis_en") or str(r["description"] or ""))[:500],
            "ean": re.sub(r"\D", "", (str(r["ean"] or "") or ex.get("ean", "")))[:14],
            "rodzina": (ex.get("rodzina") or "")[:160],
            "base_uom": (ex.get("base_uom") or str(r["unit"] or ""))[:20],
            "producer_code": (ex.get("producer_code") or "")[:60],
            "tariff_cn": (str(r["tariff_cn"] or "") or ex.get("tariff_cn", ""))[:30],
            "supplier_codes": (str(r["supplier_codes"] or "") or ex.get("supplier_codes", ""))[:500],
            "levels": levels,
        }
        _save_record(db, rec)
        n += 1
    db.commit()
    return {"migrated": n}


def import_workbook(db, rows: list) -> dict:
    """Import z surowych wierszy (1/2-wierszowy nagłówek). Zapisuje materiały +
    przeliczniki UOM. Zwraca {imported, skipped, conversions}."""
    ensure_table(db)
    records = parse_workbook_rows(rows)
    imported = conv = 0
    for rec in records:
        _save_record(db, rec)
        conv += _save_conversions(db, rec)
        imported += 1
    db.commit()
    return {"imported": imported, "skipped": 0, "conversions": conv}


def upsert_materials(db, rows: list) -> dict:
    """Back-compat: rows = lista dict-ów (jednowierszowy format maszynowy)."""
    ensure_table(db)
    imported = skipped = 0
    for d in rows:
        rec = parse_row(d)
        if not rec:
            skipped += 1
            continue
        _save_record(db, rec)
        _save_conversions(db, rec)
        imported += 1
    db.commit()
    return {"imported": imported, "skipped": skipped}


def get_material(db, ref):
    ensure_table(db)
    row = db.execute("SELECT * FROM material_master WHERE ref_code=?", (str(ref),)).fetchone()
    if not row:
        norm = normalize_ref(ref)
        if norm:
            row = db.execute(
                "SELECT * FROM material_master WHERE ref_norm=? LIMIT 1", (norm,)
            ).fetchone()
    return dict(row) if row else None


def validate_items(db, items: list, ref_key: str = "ref", ean_key: str = "ean_a") -> list:
    ensure_table(db)
    warns = []
    for it in items or []:
        ref = str(it.get(ref_key) or "").strip()
        if not ref:
            continue
        mat = get_material(db, ref)
        if not mat:
            warns.append(f"REF {ref} — brak w bazie master (uzupełnij dane materiału)")
            continue
        it_ean = re.sub(r"\D", "", str(it.get(ean_key) or ""))
        if it_ean and mat.get("ean") and it_ean != str(mat["ean"]):
            warns.append(f"REF {ref} — EAN {it_ean} ≠ master {mat['ean']}")
    return warns
