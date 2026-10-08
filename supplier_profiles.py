"""
supplier_profiles.py — rozszerzony system profili dostawców z mapowaniem kolumn.

Każdy profil zawiera:
- Dane identyfikacyjne (nazwa, kraj, waluta, słowa kluczowe)
- Mapowanie kolumn per typ dokumentu (PI, CI, PO, PL, SAD, BL)
- Reguły tolerancji cenowych
- Mapowanie warunków płatności
- Słownik synonimów produktów
- Historia zmian (audit log)
- Weryfikacja na próbce danych
"""

import json
import re
from typing import Optional
from datetime import datetime
from db import get_db


def _to_int(v, default: int) -> int:
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


def _to_float(v, default: float) -> float:
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


# ─────────────────────────────────────────────────────────────────────────────
# ROLE KOLUMN
# ─────────────────────────────────────────────────────────────────────────────

COLUMN_ROLES = {
    'ref':          {'label': 'REF / Kod produktu',    'icon': '🔑', 'required': True},
    'description':  {'label': 'Opis produktu',          'icon': '📝', 'required': False},
    'qty':          {'label': 'Ilość',                  'icon': '🔢', 'required': True},
    'price':        {'label': 'Cena jednostkowa',       'icon': '💰', 'required': True},
    'net':          {'label': 'Wartość netto',          'icon': '💵', 'required': False},
    'unit':         {'label': 'Jednostka miary',        'icon': '📦', 'required': False},
    'lot':          {'label': 'Numer LOT/partii',       'icon': '🏭', 'required': False},
    'skip':         {'label': 'POMIŃ',                  'icon': '⛔', 'required': False},
    # SAD-specific roles
    'tariff_code':  {'label': 'Kod taryfy [18 09]',        'icon': '🛃', 'required': False, 'sad_only': True},
    'gross_weight': {'label': 'Masa brutto [18 04]',        'icon': '⚖️', 'required': False, 'sad_only': True},
    'net_weight':   {'label': 'Masa netto [18 01]',         'icon': '⚖️', 'required': False, 'sad_only': True},
    'customs_value':{'label': 'Wartość celna [14 06/08]',   'icon': '💶', 'required': False, 'sad_only': True},
    'stat_value':   {'label': 'Wartość statystyczna [99 06]','icon':'📊', 'required': False, 'sad_only': True},
    'customs_duty': {'label': 'Cło A00 [14 03]',            'icon': '🏦', 'required': False, 'sad_only': True},
    'vat_amount':   {'label': 'VAT B00 [14 03]',            'icon': '🏦', 'required': False, 'sad_only': True},
    'mrn':          {'label': 'MRN zgłoszenia',             'icon': '🔖', 'required': False, 'sad_only': True},
    'procedure':    {'label': 'Procedura celna [11 09]',    'icon': '📋', 'required': False, 'sad_only': True},
    'origin':       {'label': 'Kraj pochodzenia [16 08]',   'icon': '🌍', 'required': False, 'sad_only': True},
    'incoterms':    {'label': 'Warunki dostawy [14 01]',    'icon': '🚢', 'required': False, 'sad_only': True},
    'packages':     {'label': 'Opakowania [18 06]',         'icon': '📦', 'required': False, 'sad_only': True},
}

# Pola dostępne tylko dla dokumentów SAD
SAD_COLUMN_ROLES = {k: v for k, v in COLUMN_ROLES.items() if v.get('sad_only')}

REF_FORMATS = {
    'standard':     'Standardowy (np. AT-NFA-S_1)',
    'sno_prefix':   'Z prefiksem numerycznym (np. 1.AT-NFA-S 1)',
    'numeric_only': 'Tylko cyfry (np. 12345)',
    'alphanumeric': 'Alfanumeryczny z separatorami',
}

# ─────────────────────────────────────────────────────────────────────────────
# DOMYŚLNE PROFILE DOSTAWCÓW
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_PROFILES = [
    {
        "code": "SHIELDCO",
        "name": "Eastport Shieldco Medical Products Co.,Ltd",
        "country": "CN", "currency": "USD", "language": "EN",
        "price_tolerance_pct": 1.0, "qty_tolerance_pct": 0.0,
        "po_rounding": 2, "price_rounding": 4,
        "detect_keywords": ["Shieldco", "SHIELDCO", "Eastport Shieldco", "SHSY"],
        "payment_terms_map": {"IZ31": "30% in advance, 70% before shipment"},
        "date_format": "%d/%m/%Y",
        "known_issues": ["Ceny w PI z 4 miejscami, PO zaokrągla do 2", "Opis w kolumnie Description, nie Product Name"],
        "notes": "Dostawca chiński - chusty operacyjne, opatrunki",
        "active": 1,
        # Mapowania kolumn per typ dokumentu
        "column_mappings": {
            "PI": {
                "S.No": "ref", "s.no": "ref",
                "Product Name": "skip", "product name": "skip",
                "Description": "description", "description": "description",
                "Unit price\n(USD)": "price", "unit price\n(usd)": "price",
                "Qty(pcs)": "qty", "qty(pcs)": "qty",
                "Amount\n(USD)": "net", "amount\n(usd)": "net",
                "Photos for \nreference": "skip",
            },
            "PO": {
                "REF / Description": "ref",
                "Order unit": "unit",
                "Order qty \n(per order \nunit)": "qty",
                "Price \n(per order \nunit)": "price",
                "Net value": "net",
                "Cartons \nqty": "skip",
                "Basic \nunit qty": "skip",
                "Base unit": "skip",
            },
        },
        # ref_format at profile root so get_ref_format() finds ref_format_pi / ref_format_po
        "ref_format_pi": "sno_prefix",
        "ref_format_po": "standard",
        "synonyms": [
            {"a": "Adhesive drape", "b": "ALPHAtex 2ply drape"},
            {"a": "Plain drape", "b": "ALPHAtex 2ply drape"},
            {"a": "Drape with Fenestration", "b": "ALPHAtex 2ply drape"},
            {"a": "Plain Drape", "b": "ALPHAtex 3ply drape"},
        ],
    },
    {
        "code": "ALPHAMED",
        "name": "Alphamed Medical Products",
        "country": "CN", "currency": "USD", "language": "EN",
        "price_tolerance_pct": 0.5, "qty_tolerance_pct": 0.0,
        "po_rounding": 2, "price_rounding": 4,
        "detect_keywords": ["ALPHAMED", "Alphamed Medical", "alphamed"],
        "payment_terms_map": {"IW04": "30 days", "IW30": "30 days after BL"},
        "date_format": "%Y/%m/%d",
        "known_issues": [],
        "notes": "",
        "active": 1,
        "column_mappings": {},
        "synonyms": [],
    },
    {
        "code": "OMEGAMEDICAL",
        "name": "Omega Medical Ltd.",
        "country": "IN", "currency": "EUR", "language": "EN",
        "price_tolerance_pct": 0.5, "qty_tolerance_pct": 0.0,
        "po_rounding": 2, "price_rounding": 4,
        "detect_keywords": ["Omega Medical", "OMEGA MEDICAL", "OML"],
        "payment_terms_map": {"IW04": "30 days"},
        "date_format": "%d/%m/%Y",
        "known_issues": [],
        "notes": "",
        "active": 1,
        "column_mappings": {},
        "synonyms": [],
    },
    {
        "code": "CHANGZHOU_NORTHBRIDGE",
        "name": "Lakeside northbridge Medical Device Co., Ltd.",
        "country": "CN", "currency": "USD", "language": "EN",
        "price_tolerance_pct": 0.5, "qty_tolerance_pct": 0.0,
        "po_rounding": 2, "price_rounding": 4,
        "detect_keywords": [
            "Northbridge", "NORTHBRIDGE", "Lakeside northbridge",
            "LAKESIDE NORTHBRIDGE MEDICAL", "LKNB",
        ],
        "payment_terms_map": {
            "IZ31": "30% in advance, 70% before shipment",
            "IW30": "30 days after BL",
            "IW04": "30 days",
        },
        "date_format": "%Y/%m/%d",
        "known_issues": [
            "Faktura CI wystawiana w USD; kurs przeliczenia PLN wg SAD pole [14 09]",
            "Nr faktury w CI i SAD musi się zgadzać dokładnie (N325-LKNB…)",
        ],
        "notes": "Dostawca chiński – worki do zbiórki moczu i inne wyroby medyczne jednorazowe. "
                 "Zgłoszenia celne obsługuje Delta Brokers Logistics SA. UC: PL999999.",
        "active": 1,
        "column_mappings": {
            # Kolumny faktury handlowej (CI)
            "CI": {
                "No.": "ref",           "no.": "ref",
                "Item No.": "ref",      "Item no.": "ref",
                "Description": "description",
                "Goods Description": "description",
                "Item Description": "description",
                "Quantity": "qty",      "quantity": "qty",
                "Qty": "qty",           "qty": "qty",
                "Pcs": "qty",           "pcs": "qty",
                "Unit Price": "price",  "Unit price": "price",
                "Unit Price (USD)": "price",
                "Amount": "net",        "amount": "net",
                "Amount (USD)": "net",
                "Total": "net",         "Total Amount": "net",
                "Unit": "unit",         "unit": "unit",
                "Lot No.": "lot",       "Batch No.": "lot",
                "Lot": "lot",           "Batch": "lot",
            },
            # Kolumny proformy (PI)
            "PI": {
                "No.": "ref",           "Item No.": "ref",
                "Description": "description",
                "Quantity": "qty",      "Qty": "qty",
                "Unit Price": "price",  "Unit Price (USD)": "price",
                "Amount": "net",        "Amount (USD)": "net",
                "Unit": "unit",
            },
            # Kolumny zamówienia ACME (PO)
            "PO": {
                "REF / Description": "ref",
                "Order unit": "unit",
                "Order qty \n(per order \nunit)": "qty",
                "Price \n(per order \nunit)": "price",
                "Net value": "net",
                "Cartons \nqty": "skip",
                "Basic \nunit qty": "skip",
                "Base unit": "skip",
            },
            # Pola SAD (Jednolity Dokument Administracyjny / AIS PL)
            # Klucze odpowiadają etykietom pól w raporcie z SAD_1148522.pdf
            "SAD": {
                # Sekcja A – identyfikacja
                "MRN zgłoszenia":             "mrn",
                "Numer zgłoszenia":           "ref",
                "LRN":                        "skip",
                "Nr ref. EACJC":              "skip",
                "Typ zgłoszenia":             "skip",
                "Stan AIS":                   "skip",
                "Data zgłoszenia":            "skip",
                "Data przyjęcia":             "skip",
                "UC zgłoszenia":              "skip",
                "Procedura celna":            "procedure",
                # Sekcja D – opis towaru
                "Opis towarów":               "description",
                "Opis":                       "description",
                "Ilość":                      "qty",
                "Kod CN":                     "tariff_code",
                "TARIC":                      "skip",
                "Kody krajowe":               "skip",
                "Masa brutto":                "gross_weight",
                "Masa netto":                 "net_weight",
                "Opakowania":                 "packages",
                # Sekcja E – wartości
                "Wartość faktur":             "customs_value",
                "Kurs":                       "skip",
                "Wartość statystyczna":       "stat_value",
                "Metoda wyceny":              "skip",
                "Doliczenia AK-1":            "skip",
                "Doliczenia AK-2":            "skip",
                "Odliczenia CA":              "skip",
                # Sekcja F – należności
                "Cło A00":                    "customs_duty",
                "VAT B00":                    "vat_amount",
                # Sekcja C – transport
                "Kraj wysyłki":               "origin",
                "Kraj przeznaczenia":         "skip",
                "Warunki dostawy":            "incoterms",
            },
        },
        # ref_format at profile root so get_ref_format() finds ref_format_ci / _pi / _po
        "ref_format_ci": "standard",
        "ref_format_pi": "standard",
        "ref_format_po": "standard",
        # Pary porównawcze CI↔SAD — które pole CI odpowiada jakiemu polu SAD
        # Używane przez moduł porównania celnego (planned: ci_sad comparator)
        "ci_sad_map": {
            "ref":         {"sad_field": "ref",          "label": "Nr faktury / Nr SAD",        "critical": True},
            "exporter":    {"sad_field": "skip",         "label": "Eksporter",                  "critical": True},
            "description": {"sad_field": "description",  "label": "Opis towaru",                "critical": True},
            "qty":         {"sad_field": "qty",           "label": "Ilość",                      "critical": True},
            "net":         {"sad_field": "customs_value", "label": "Wartość netto / Wartość celna [14 06]", "critical": True},
            "incoterms":   {"sad_field": "incoterms",    "label": "Warunki dostawy [14 01]",    "critical": False},
            "origin":      {"sad_field": "origin",       "label": "Kraj pochodzenia [16 08]",   "critical": False},
            "gross_weight":{"sad_field": "gross_weight", "label": "Masa brutto [18 04]",        "critical": False},
            "net_weight":  {"sad_field": "net_weight",   "label": "Masa netto [18 01]",         "critical": False},
            "packages":    {"sad_field": "packages",     "label": "Liczba opakowań [18 06]",    "critical": False},
            "tariff_code": {"sad_field": "tariff_code",  "label": "Kod CN/TARIC [18 09]",       "critical": False},
        },
        # Znane stałe wartości SAD dla tego dostawcy (do walidacji/porównania)
        "sad_constants": {
            "importer":           "ACME SP. Z O.O.",
            "importer_nip":       "0000000000",
            "declarant":          "DELTA BROKERS LOGISTICS SA",
            "declarant_nip":      "0000000000",
            "customs_authority":  "PL999999",
            "procedure":          "4000",
            "origin":             "CN",
            "tariff_code":        "3926909790",
            "incoterms":          "FOB",
            "incoterms_place":    "SHANGHAI",
        },
        "synonyms": [
            {"a": "WOREK DO ZBIÓRKI MOCZU",  "b": "Urine Collection Bag"},
            {"a": "WOREK DO ZBIÓRKI MOCZU",  "b": "Urine Bag"},
            {"a": "WOREK DO ZBIÓRKI MOCZU",  "b": "Urine Drainage Bag"},
        ],
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# INICJALIZACJA BAZY
# ─────────────────────────────────────────────────────────────────────────────

def init_suppliers_table(db_path: str = "instance/doccompare.db"):
    db = get_db()
    try:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS suppliers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                country TEXT DEFAULT 'XX',
                currency TEXT DEFAULT 'USD',
                language TEXT DEFAULT 'EN',
                price_rounding INTEGER DEFAULT 4,
                po_rounding INTEGER DEFAULT 2,
                payment_terms_json TEXT DEFAULT '{}',
                date_format TEXT DEFAULT '%Y/%m/%d',
                known_issues_json TEXT DEFAULT '[]',
                price_tolerance_pct REAL DEFAULT 0.5,
                qty_tolerance_pct REAL DEFAULT 0.0,
                detect_keywords_json TEXT DEFAULT '[]',
                notes TEXT DEFAULT '',
                active INTEGER DEFAULT 1,
                column_mapping_json TEXT DEFAULT '{}',
                synonyms_json TEXT DEFAULT '[]',
                profile_version INTEGER DEFAULT 1,
                -- Kolumny używane przez save_supplier — muszą być w bazowym CREATE,
                -- inaczej świeża instalacja (init bez migrate_db) pada na „no such column".
                profile_updated_at TEXT DEFAULT '',
                wizard_completed INTEGER DEFAULT 0,
                pi_column_mapping_json TEXT DEFAULT '{}',
                po_column_mapping_json TEXT DEFAULT '{}',
                sad_column_mapping_json TEXT DEFAULT '{}',
                ref_format_pi TEXT DEFAULT 'standard',
                ref_format_po TEXT DEFAULT 'standard',
                ignore_fields_json TEXT DEFAULT '[]',
                custom_rules_json TEXT DEFAULT '[]',
                header_patterns_json TEXT DEFAULT '{}',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS supplier_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_id INTEGER NOT NULL,
                supplier_code TEXT NOT NULL,
                action TEXT NOT NULL,
                changed_by TEXT,
                changed_at TEXT DEFAULT (datetime('now')),
                diff_json TEXT DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                UNIQUE(category, key)
            );
        """)
        for p in DEFAULT_PROFILES:
            db.execute("""
                INSERT OR IGNORE INTO suppliers
                (code,name,country,currency,language,price_rounding,po_rounding,
                 payment_terms_json,date_format,known_issues_json,price_tolerance_pct,
                 qty_tolerance_pct,detect_keywords_json,notes,active,
                 column_mapping_json,synonyms_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                p["code"], p["name"], p["country"], p["currency"], p["language"],
                p["price_rounding"], p["po_rounding"],
                json.dumps(p["payment_terms_map"]), p["date_format"],
                json.dumps(p["known_issues"]),
                p["price_tolerance_pct"], p["qty_tolerance_pct"],
                json.dumps(p["detect_keywords"]), p["notes"], p["active"],
                json.dumps(p.get("column_mappings", {})),
                json.dumps(p.get("synonyms", [])),
            ))
        db.commit()
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# CRUD
# ─────────────────────────────────────────────────────────────────────────────

def _row_to_profile(row) -> dict:
    if not row:
        return {}
    d = dict(row)
    # Deserializuj wszystkie pola JSON
    json_fields = [
        ('payment_terms_json',     {}),
        ('known_issues_json',      []),
        ('detect_keywords_json',   []),
        ('column_mapping_json',    {}),
        ('synonyms_json',          []),
        ('pi_column_mapping_json',  {}),
        ('po_column_mapping_json',  {}),
        ('sad_column_mapping_json', {}),
        ('header_patterns_json',   {}),
        ('ignore_fields_json',     []),
        ('custom_rules_json',      []),
    ]
    for field, default in json_fields:
        raw = d.get(field)
        if isinstance(raw, str) and raw:
            try:
                d[field] = json.loads(raw)
            except Exception:
                d[field] = default
        elif not raw:
            d[field] = default
    return d


def get_all_suppliers(db_path="instance/doccompare.db") -> list:
    db = get_db()
    try:
        rows = db.execute("SELECT * FROM suppliers ORDER BY active DESC, name").fetchall()
    finally:
        db.close()
    return [_row_to_profile(r) for r in rows]


def get_supplier(code: str, db_path="instance/doccompare.db",
                 include_inactive: bool = False) -> Optional[dict]:
    # FIX 17: include_inactive parameter to allow fetching inactive profiles
    where = "WHERE code=?" if include_inactive else "WHERE code=? AND active=1"
    db = get_db()
    try:
        # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
        row = db.execute(f"SELECT * FROM suppliers {where}", (code,)).fetchone()  # nosec B608
    finally:
        db.close()
    return _row_to_profile(row) if row else None


def get_supplier_by_id(sid: int, db_path="instance/doccompare.db") -> Optional[dict]:
    db = get_db()
    try:
        row = db.execute("SELECT * FROM suppliers WHERE id=?", (sid,)).fetchone()
    finally:
        db.close()
    return _row_to_profile(row) if row else None


def save_supplier(profile: dict, changed_by: str = "system",
                  db_path="instance/doccompare.db") -> dict:
    db = get_db()
    try:
        return _save_supplier_inner(db, profile, changed_by)
    finally:
        db.close()


def _save_supplier_inner(db, profile: dict, changed_by: str) -> dict:
    code = (profile.get('code') or '').upper().strip()[:20]
    if not code:
        raise ValueError("Brak kodu dostawcy (pole 'code' jest wymagane)")
    existing = db.execute("SELECT * FROM suppliers WHERE code=?", (code,)).fetchone()

    fields = {
        'name':               str(profile.get('name', code) or code)[:200],
        'country':            (profile.get('country') or 'XX').upper()[:2],
        'currency':           (profile.get('currency') or 'USD').upper()[:3],
        'language':           (profile.get('language') or 'EN').upper()[:2],
        'price_rounding':     max(0, min(10, _to_int(profile.get('price_rounding', 4), 4))),
        'po_rounding':        max(0, min(10, _to_int(profile.get('po_rounding', 2), 2))),
        'date_format':        str(profile.get('date_format', '%Y/%m/%d') or '%Y/%m/%d')[:50],
        'known_issues_json':  json.dumps(profile.get('known_issues', [])),
        'price_tolerance_pct':max(0.0, min(100.0, _to_float(profile.get('price_tolerance_pct', 0.5), 0.5))),
        'qty_tolerance_pct':  max(0.0, min(100.0, _to_float(profile.get('qty_tolerance_pct', 0.0), 0.0))),
        'detect_keywords_json':json.dumps(profile.get('detect_keywords', [])),
        'notes':              str(profile.get('notes', '') or '')[:2000],
        'active':             1 if profile.get('active', True) else 0,
        'column_mapping_json':json.dumps(profile.get('column_mappings', {})),
        'synonyms_json':           json.dumps(profile.get('synonyms', [])),
        'profile_updated_at':      datetime.now().isoformat(),
        'wizard_completed':        (1 if profile.get('wizard_completed') else 0),
        'pi_column_mapping_json':   json.dumps(profile.get('pi_column_mapping_json', {})),
        'po_column_mapping_json':   json.dumps(profile.get('po_column_mapping_json', {})),
        'sad_column_mapping_json':  json.dumps(profile.get('sad_column_mapping_json', {})),
        'ref_format_pi':           str(profile.get('ref_format_pi', 'standard') or 'standard')[:50],
        'ref_format_po':           str(profile.get('ref_format_po', 'standard') or 'standard')[:50],
        'payment_terms_json':      json.dumps(
            profile['payment_terms_json'] if profile.get('payment_terms_json') is not None
            else (profile.get('payment_terms_map') or {})
        ),
        'ignore_fields_json':      json.dumps(profile.get('ignore_fields_json', [])),
        'custom_rules_json':       json.dumps(profile.get('custom_rules_json', [])),
        'header_patterns_json':    json.dumps(profile.get('header_patterns_json', {})),
    }
    if existing:
        sets = ', '.join(f'{k}=?' for k in fields)
        # Refresh updated_at on every profile update (column is not in `fields`).
        # Bandit B608: nazwy kolumn z twardej białej listy w kodzie; wartości jako parametry ?.
        db.execute(f"UPDATE suppliers SET {sets}, updated_at=datetime('now') WHERE code=?",  # nosec B608
                   list(fields.values()) + [code])
        # FIX 16: Atomic increment of profile_version to avoid race conditions
        db.execute("UPDATE suppliers SET profile_version = profile_version + 1 WHERE code=?",
                   (code,))
        action = 'update'
    else:
        fields['code'] = code
        cols = ', '.join(fields.keys())
        placeholders = ', '.join('?' * len(fields))
        # Bandit B608: nazwy kolumn z twardej białej listy w kodzie; wartości jako parametry ?.
        db.execute(f'INSERT INTO suppliers ({cols}) VALUES ({placeholders})',  # nosec B608
                   list(fields.values()))
        action = 'create'

    # Audit log
    old_data = dict(existing) if existing else {}
    diff = {k: {'old': old_data.get(k), 'new': v}
            for k, v in fields.items()
            if str(old_data.get(k, '')) != str(v)}
    _sid_row = db.execute("SELECT id FROM suppliers WHERE code=?", (code,)).fetchone()
    sid = _sid_row['id'] if _sid_row else None
    db.execute(
        "INSERT INTO supplier_audit_log(supplier_id,supplier_code,action,changed_by,diff_json) VALUES(?,?,?,?,?)",
        (sid, code, action, changed_by, json.dumps(diff))
    )
    db.commit()
    row = db.execute("SELECT * FROM suppliers WHERE code=?", (code,)).fetchone()
    return _row_to_profile(row)


def delete_supplier(supplier_id: int, db_path="instance/doccompare.db") -> bool:
    """Usuwa profil dostawcy. Nie można usunąć tylko profili z id<=0 (nie istnieją)."""
    db = get_db()
    try:
        row = db.execute("SELECT code, id FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
        if not row:
            return False
        # Usuń — wszystkie profile stworzone przez użytkownika można usunąć
        db.execute("DELETE FROM suppliers WHERE id=?", (supplier_id,))
        # Usuń też powiązane logi
        db.execute("DELETE FROM supplier_audit_log WHERE supplier_id=?", (supplier_id,))
        try:
            db.execute("DELETE FROM supplier_analysis_log WHERE supplier_id=?", (supplier_id,))
        except Exception:
            pass  # table may not exist on fresh installs that skipped migrate_db.py
        db.commit()
        return True
    finally:
        db.close()


def get_audit_log(supplier_id: int, db_path="instance/doccompare.db") -> list:
    db = get_db()
    try:
        rows = db.execute(
            "SELECT * FROM supplier_audit_log WHERE supplier_id=? ORDER BY changed_at DESC LIMIT 50",
            (supplier_id,)
        ).fetchall()
    finally:
        db.close()
    result = []
    for r in rows:
        d = dict(r)
        try: d['diff_json'] = json.loads(d['diff_json'])
        except Exception: d["diff_json"] = {}
        result.append(d)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# DETEKCJA DOSTAWCY
# ─────────────────────────────────────────────────────────────────────────────

def detect_supplier(text: str, db_path="instance/doccompare.db") -> Optional[dict]:
    if not text:
        return None
    # FIX 14: Case-insensitive substring matching for all keywords
    text_lower = text.lower()
    suppliers = get_all_suppliers(db_path)
    best, best_score = None, 0
    for s in suppliers:
        kws = s.get('detect_keywords_json', [])
        if isinstance(kws, str):
            try: kws = json.loads(kws)
            except Exception: kws = []
        # Dopasowanie po granicy słowa (a nie naiwny substring) — krótki keyword
        # jak „OML"/„LKNB" nie trafia już w środek niepowiązanego słowa.
        score = sum(
            1 for kw in kws
            if isinstance(kw, str) and kw.strip()
            and re.search(r'\b' + re.escape(kw.lower()) + r'\b', text_lower)
        )
        if score > best_score:
            best_score, best = score, s
    return best if best_score > 0 else None


# ─────────────────────────────────────────────────────────────────────────────
# EKSTRAKCJA TABELI DO WIZARDA
# ─────────────────────────────────────────────────────────────────────────────

def extract_table_preview(pdf_path: str, doc_type: str = 'auto') -> dict:
    """
    Wyciąga podgląd tabeli z PDF do wizarda mapowania kolumn.
    Zwraca: {headers, rows(max 5), detected_mappings, table_found}
    """
    result = {
        'table_found': False, 'headers': [], 'rows': [],
        'detected_mappings': {}, 'doc_type': doc_type,
        'extraction_method': 'none',
    }
    try:
        import camelot
        tbls = camelot.read_pdf(pdf_path, pages='1', flavor='lattice',
                                 suppress_stdout=True)
        if not tbls or len(tbls) == 0:
            tbls = camelot.read_pdf(pdf_path, pages='1', flavor='stream',
                                     suppress_stdout=True)
        if tbls and len(tbls) > 0:
            # Znajdź najlepszą tabelę (z nagłówkami kolumnowymi)
            best_tbl = None
            for tbl in tbls:
                df = tbl.df
                if df.shape[0] >= 2 and df.shape[1] >= 3:
                    header_text = ' '.join(str(c) for c in df.iloc[0]).lower()
                    if any(w in header_text for w in ['qty', 'price', 'amount', 'ilość', 'cena', 'ref', 'description', 's.no', 'no']):
                        best_tbl = df
                        break
            if best_tbl is not None:
                headers = [str(h).strip().replace('\n', ' ') for h in best_tbl.iloc[0]]
                rows = []
                for _, row in best_tbl.iloc[1:6].iterrows():
                    r = [str(v).strip().replace('\n', ' ')[:60] for v in row]
                    if any(c for c in r):
                        rows.append(r)
                result.update({
                    'table_found': True,
                    'headers': headers,
                    'rows': rows,
                    'extraction_method': 'camelot',
                })
                result['detected_mappings'] = _auto_detect_column_roles(headers)
    except Exception as e:
        pass

    # Fallback: pdfplumber
    if not result['table_found']:
        try:
            import pdfplumber
            with pdfplumber.open(pdf_path) as pdf:
                for page in pdf.pages[:2]:
                    tables = page.extract_tables()
                    for tbl in (tables or []):
                        if tbl and len(tbl) >= 2 and len(tbl[0]) >= 3:
                            header_text = ' '.join(str(c or '') for c in tbl[0]).lower()
                            if any(w in header_text for w in ['qty', 'price', 'ref', 'description', 'no', 's.no']):
                                headers = [str(h or '').strip()[:40] for h in tbl[0]]
                                rows = [[str(v or '').strip()[:60] for v in r] for r in tbl[1:6]]
                                result.update({
                                    'table_found': True,
                                    'headers': headers,
                                    'rows': rows,
                                    'extraction_method': 'pdfplumber',
                                })
                                result['detected_mappings'] = _auto_detect_column_roles(headers)
                                break
                    if result['table_found']:
                        break
        except Exception:
            pass
    return result


def _auto_detect_column_roles(headers: list) -> dict:
    """Auto-sugestie mapowania kolumn na podstawie nazw nagłówków."""
    mappings = {}
    header_lower = [h.lower().strip() for h in headers]

    role_keywords = {
        'ref':          ['ref', 'code', 'item', 'article', 'sku', 'indeks', 'kod', 's.no', 'sno', 'no.', 'product code', 'catalogue'],
        'description':  ['description', 'opis', 'nazwa', 'goods', 'product description', 'item description', 'opis towarów'],
        'qty':          ['qty', 'quantity', 'ilość', 'pcs', 'szt', 'menge', 'nos', 'nos.'],
        'price':        ['price', 'cena', 'unit price', 'unit-price', 'rate', 'preis'],
        'net':          ['amount', 'net', 'value', 'total', 'wartość', 'kwota'],
        'unit':         ['unit', 'uom', 'jm', 'order unit'],
        'lot':          ['lot', 'batch', 'seria', 'lot no', 'batch no'],
        'tariff_code':  ['tariff', 'kod taryfy', 'taryfa', 'ncm', 'hs code', 'hs-code', 'commodity code', 'kod towaru', 'kod cn', 'taric', '33'],
        'gross_weight': ['gross', 'brutto', 'gross weight', 'masa brutto', 'gross mass', '35'],
        'net_weight':   ['net weight', 'masa netto', 'net mass', '38'],
        'customs_value':['customs value', 'wartość celna', 'wartość faktur', '46'],
        'stat_value':   ['stat_value', 'wartość statystyczna', 'statistical value', '99'],
        'customs_duty': ['cło', 'customs duty', 'duty', 'a00'],
        'vat_amount':   ['vat', 'b00', 'podatek vat'],
        'mrn':          ['mrn', 'mrn zgłoszenia', 'movement reference'],
        'procedure':    ['procedura', 'procedure', 'procedura celna', '11 09'],
        'origin':       ['origin', 'kraj pochodzenia', 'country of origin', 'kraj wysyłki', '16'],
        'incoterms':    ['incoterms', 'warunki dostawy', 'delivery terms', '14 01', 'fob', 'cif', 'exw'],
        'packages':     ['packages', 'opakowania', 'cartons', 'pkgs', '18 06'],
    }

    assigned = set()
    assigned_roles = set()   # role już przypisane (zamiast O(n²) skanu mappings)
    # Priorytetowa kolejność — ref i qty pierwsze; SAD-specific last
    for role in ['ref', 'qty', 'price', 'net', 'description', 'unit', 'lot',
                 'tariff_code', 'gross_weight', 'net_weight', 'customs_value',
                 'stat_value', 'customs_duty', 'vat_amount', 'mrn', 'procedure',
                 'origin', 'incoterms', 'packages']:
        keywords = role_keywords[role]
        for idx, h in enumerate(header_lower):
            if idx in assigned:
                continue
            for kw in keywords:
                if kw == h or (len(kw) > 3 and kw in h):
                    if role not in assigned_roles:
                        mappings[headers[idx]] = role
                        assigned.add(idx)
                        assigned_roles.add(role)
                        break

    # Reszta → skip
    for idx, h in enumerate(headers):
        if h not in mappings:
            mappings[h] = 'skip'
    return mappings


# ─────────────────────────────────────────────────────────────────────────────
# ZASTOSOWANIE PROFILU DO PORÓWNANIA
# ─────────────────────────────────────────────────────────────────────────────

def apply_column_mapping(df, doc_type: str, supplier: dict) -> dict:
    """
    Stosuje mapowanie kolumn z profilu dostawcy do DataFrame tabeli.
    Zwraca dict: {ref, description, qty, price, net, unit, lot}
    """
    mappings = supplier.get('column_mapping_json', {})
    if isinstance(mappings, str):
        try: mappings = json.loads(mappings)
        except Exception: mappings = {}

    doc_mapping = mappings.get(doc_type, {})
    if not doc_mapping:
        return {}

    # Normalizuj klucze (lowercase + whitespace collapse)
    doc_mapping_lower = {re.sub(r'\s+', ' ', k.strip()).lower(): v for k, v in doc_mapping.items()}

    if hasattr(df, 'columns'):
        result = {}
        for col in df.columns:
            col_str = str(col).strip()
            # FIX 15: Normalize whitespace in header before lookup
            header_normalized = re.sub(r'\s+', ' ', col_str.strip()).lower()
            role = doc_mapping_lower.get(header_normalized) or doc_mapping.get(col_str)
            if role and role != 'skip':
                result[role] = col
        return result
    return {}


def apply_supplier_rules(report: dict, supplier: dict) -> dict:
    """Stosuje reguły dostawcy do wyników porównania (tolerancje, mapowania płatności)."""
    if not supplier:
        return report
    adjustments = []
    modules = report.get('modules', {})
    price_tol = _to_float(supplier.get('price_tolerance_pct'), 0.5) / 100.0
    payment_map = supplier.get('payment_terms_json', {})
    if isinstance(payment_map, str):
        try: payment_map = json.loads(payment_map)
        except Exception: payment_map = {}

    tbl = modules.get('table', {})
    if tbl.get('ok') and tbl.get('items'):
        for item in tbl['items']:
            if item.get('status') != 'roznica': continue
            try:
                from normalizer import normalize_number
                pa = normalize_number(str(item.get('price_a', '') or ''))
                pb = normalize_number(str(item.get('price_b', '') or ''))
                if pa and pb and pa != 0:
                    diff_pct = abs(pa - pb) / abs(pa)
                    if diff_pct <= price_tol:
                        item['status'] = 'format'
                        item['supplier_note'] = f"Δ {diff_pct*100:.3f}% w tolerancji ±{supplier.get('price_tolerance_pct',0.5)}%"
                        adjustments.append(f"Cena {item.get('ref','')}: {diff_pct*100:.2f}% w tolerancji")
            except Exception as _e:
                import logging
                logging.getLogger("supplier_profiles").debug("price-tolerance check failed: %s", _e)

    # Mapowanie warunków płatności
    for m_key in ['table', 'enhanced']:
        m = modules.get(m_key, {})
        if not m.get('ok'): continue
        for h in m.get('headers', []):
            if h.get('status') == 'roznica' and 'płatno' in h.get('key','').lower():
                va = str(h.get('val_a', '') or '')
                vb = str(h.get('val_b', '') or '')
                mapped_a = payment_map.get(va, va)
                # Równość znormalizowana lub dopasowanie po granicy słowa — „30 days"
                # nie jest już mylnie uznawane za zgodne z „130 days".
                _ma = re.sub(r'\s+', ' ', mapped_a.strip().lower())
                _vb = re.sub(r'\s+', ' ', vb.strip().lower())
                if _ma and _vb and (
                    _ma == _vb
                    or re.search(r'\b' + re.escape(_ma) + r'\b', _vb)
                    or re.search(r'\b' + re.escape(_vb) + r'\b', _ma)
                ):
                    h['status'] = 'ok'
                    h['comment'] = f'SAP {va} = "{mapped_a}"'
                    adjustments.append(f"Warunki: {va}={mapped_a}")

    report['supplier_applied'] = supplier.get('code', '')
    report['supplier_name'] = supplier.get('name', '')
    report['supplier_adjustments'] = adjustments
    return report


# ─────────────────────────────────────────────────────────────────────────────
# NOWA FUNKCJA: budowanie column_overrides z profilu dostawcy
# Używana przez enhanced_comparator i table_extractor
# ─────────────────────────────────────────────────────────────────────────────

def get_column_overrides(supplier: dict, doc_type: str) -> dict:
    """
    Zwraca mapowanie {header_name: role} dla danego typu dokumentu.
    Używane przez silniki do nadpisania auto-detekcji kolumn.
    
    doc_type: 'PI', 'PO', 'CI'
    Zwraca: {'S.No': 'ref', 'Product Name': 'skip', 'Description': 'description', ...}
    """
    if not supplier:
        return {}
    
    key = f"{(doc_type or '').lower()}_column_mapping_json"
    mapping = supplier.get(key, {})
    if isinstance(mapping, str):
        try:
            mapping = json.loads(mapping)
        except Exception:
            mapping = {}
    
    if not mapping:
        # Fallback do ogólnego column_mapping_json
        general = supplier.get('column_mapping_json', {})
        if isinstance(general, str):
            try:
                general = json.loads(general)
            except Exception:
                general = {}
        mapping = general.get(doc_type, {}) or general.get((doc_type or '').lower(), {})
    
    return mapping or {}


def get_ref_format(supplier: dict, doc_type: str) -> str:
    """Zwraca format REF dla danego dostawcy i typu dokumentu."""
    if not supplier:
        return 'standard'
    key = f"ref_format_{(doc_type or '').lower()}"
    return supplier.get(key, 'standard') or 'standard'


def get_ignore_fields(supplier: dict) -> list:
    """Zwraca listę pól do pominięcia w porównaniu."""
    if not supplier:
        return []
    ig = supplier.get('ignore_fields_json', [])
    if isinstance(ig, str):
        try:
            ig = json.loads(ig)
        except Exception:
            ig = [s.strip() for s in ig.split(',') if s.strip()]
    return ig if isinstance(ig, list) else []


def log_analysis(supplier_id: int, comparison_id: int, had_errors: bool,
                 db_path: str = "instance/doccompare.db"):
    """Zapisuje informację o użyciu profilu dostawcy do logów."""
    try:
        from datetime import datetime
        db = get_db()
        try:
            db.execute("""
                INSERT INTO supplier_analysis_log
                (supplier_id, comparison_id, created_at)
                VALUES (?, ?, ?)
            """, (supplier_id, comparison_id, datetime.utcnow().isoformat()))
            db.commit()
        finally:
            db.close()
    except Exception:
        pass
