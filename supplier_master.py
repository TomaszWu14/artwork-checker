"""
supplier_master.py — master data dostawców (rozszerza istniejącą tabelę
`suppliers`, NIE tworzy drugiego rejestru).

Kolumny importu (jednowierszowy nagłówek, nazwy tolerancyjne):
  kod_dostawcy*, nazwa*, kod_producenta, kraj, waluta, incoterms,
  warunki_platnosci, lead_time_dni, slowa_kluczowe (sep. ; lub ,),
  osoba_kontaktowa, email, telefon, uwagi

kod_producenta spina master data dostawców z master data materiałów
(material_master.producer_code) → „który dostawca produkuje dany materiał”.
"""
from __future__ import annotations

import json
import re

# Nowe kolumny dokładane do tabeli suppliers. Dokładamy też notes/active/
# detect_keywords_json — import_workbook ich używa, a stara tabela mogła ich nie mieć.
_EXTRA_COLS = (
    "producer_code TEXT DEFAULT ''",
    "incoterms TEXT DEFAULT ''",
    "lead_time_days INTEGER DEFAULT 0",
    "payment_terms_default TEXT DEFAULT ''",
    "contact_person TEXT DEFAULT ''",
    "email TEXT DEFAULT ''",
    "phone TEXT DEFAULT ''",
    "notes TEXT DEFAULT ''",
    "active INTEGER DEFAULT 1",
    "detect_keywords_json TEXT DEFAULT '[]'",
)


def ensure_columns(db) -> None:
    """Dokłada brakujące kolumny do suppliers (idempotentnie).
    PostgreSQL: nieudany ALTER psuje transakcję → rollback po błędzie, commit po
    sukcesie (inaczej kolejne polecenia padają na 'transaction aborted').

    Memoizacja per-połączenie: na PG 7× ALTER (z rollbackiem przy istniejących
    kolumnach) wykonuje się raz na żądanie, nie przy każdym supplier_for_producer
    w pętli. Na SQLite setattr się nie powiedzie → zachowanie bez zmian."""
    if getattr(db, "_sm_ensured", False):
        return
    for col in _EXTRA_COLS:
        try:
            db.execute(f"ALTER TABLE suppliers ADD COLUMN {col}")
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
    try:
        db._sm_ensured = True
    except (AttributeError, TypeError):
        pass


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower()).replace(".", "")


_HEADER = {
    "code": ("kod_dostawcy", "kod dostawcy", "code", "kod", "symbol"),
    "name": ("nazwa", "name", "nazwa dostawcy"),
    "producer_code": ("kod_producenta", "kod producenta", "producent", "producer", "manufacturer"),
    "country": ("kraj", "country"),
    "currency": ("waluta", "currency"),
    "incoterms": ("incoterms", "warunki dostawy", "warunki_dostawy", "incoterm"),
    "payment_terms_default": ("warunki_platnosci", "warunki platnosci", "warunki płatności",
                              "payment", "payment terms", "platnosc"),
    "lead_time_days": ("lead_time_dni", "lead time", "lead_time", "czas produkcji", "lead time dni"),
    "keywords": ("slowa_kluczowe", "słowa kluczowe", "slowa kluczowe", "keywords", "detect"),
    "contact_person": ("osoba_kontaktowa", "kontakt", "contact", "osoba kontaktowa", "contact person"),
    "email": ("email", "e-mail", "mail"),
    "phone": ("telefon", "phone", "tel", "nr telefonu"),
    "notes": ("uwagi", "notes", "notatki", "komentarz"),
}


def _classify(hdr: str):
    h = _norm(hdr)
    for field, aliases in _HEADER.items():
        if h in aliases:
            return field
    return None


def _build_colmap(header_row):
    return [_classify(h) for h in header_row]


def parse_rows(rows: list) -> list:
    """rows = lista wierszy (list komórek), pierwszy = nagłówek. → lista dict-ów."""
    # Tylko wiersze będące listą/krotką — str/inny typ rozbiłby się na znaki (list("abc")).
    rows = [list(r) for r in rows if isinstance(r, (list, tuple))]
    if not rows:
        return []
    colmap = _build_colmap([str(c or "").strip() for c in rows[0]])
    out = []
    for row in rows[1:]:
        rec = {}
        for ci, field in enumerate(colmap):
            if not field or ci >= len(row):
                continue
            val = str(row[ci]).strip() if row[ci] is not None else ""
            if val:
                rec[field] = val
        if rec.get("code") and rec.get("name"):
            out.append(rec)
    return out


def _kw_json(raw: str) -> str:
    parts = [p.strip() for p in re.split(r"[;,]", str(raw or "")) if p.strip()]
    return json.dumps(parts, ensure_ascii=False)


def import_workbook(db, rows: list) -> dict:
    """Import master daty dostawców do tabeli suppliers (upsert po code)."""
    ensure_columns(db)
    recs = parse_rows(rows)
    imported = 0
    for r in recs:
        code = r["code"][:50]
        # Pierwszy ciąg cyfr (np. "2-3 dni" → 2, "10.5" → 10), nie sklejaj wszystkich cyfr.
        _lead_m = re.search(r"\d+", str(r.get("lead_time_days", "")))
        try:
            lead = int(_lead_m.group(0)) if _lead_m else 0
        except (TypeError, ValueError):
            lead = 0
        kw = _kw_json(r.get("keywords", "")) if r.get("keywords") else None
        # lead_time_days: None gdy kolumna nieobecna w imporcie (żeby przy
        # częściowym re-imporcie nie nadpisać istniejącej wartości zerem).
        lead_val = lead if "lead_time_days" in r else None
        db.execute(
            "INSERT INTO suppliers(code, name, country, currency, producer_code, incoterms, "
            "lead_time_days, payment_terms_default, contact_person, email, phone, notes, "
            "detect_keywords_json) "
            "VALUES(?,?,COALESCE(NULLIF(?,''),'XX'),COALESCE(NULLIF(?,''),'USD'),"
            "?,?,?,?,?,?,?,?,COALESCE(?, '[]')) "
            # ON CONFLICT: zachowaj istniejące niepuste wartości, gdy import podał
            # pustą wartość (COALESCE/NULLIF) — częściowy re-import nie kasuje danych.
            "ON CONFLICT(code) DO UPDATE SET name=excluded.name, "
            "country=COALESCE(NULLIF(excluded.country,''), NULLIF(excluded.country,'XX'), suppliers.country), "
            "currency=COALESCE(NULLIF(excluded.currency,''), NULLIF(excluded.currency,'USD'), suppliers.currency), "
            "producer_code=COALESCE(NULLIF(excluded.producer_code,''), suppliers.producer_code), "
            "incoterms=COALESCE(NULLIF(excluded.incoterms,''), suppliers.incoterms), "
            "lead_time_days=COALESCE(excluded.lead_time_days, suppliers.lead_time_days), "
            "payment_terms_default=COALESCE(NULLIF(excluded.payment_terms_default,''), suppliers.payment_terms_default), "
            "contact_person=COALESCE(NULLIF(excluded.contact_person,''), suppliers.contact_person), "
            "email=COALESCE(NULLIF(excluded.email,''), suppliers.email), "
            "phone=COALESCE(NULLIF(excluded.phone,''), suppliers.phone), "
            "notes=COALESCE(NULLIF(excluded.notes,''), suppliers.notes), "
            "detect_keywords_json=COALESCE(excluded.detect_keywords_json, suppliers.detect_keywords_json)",
            (code, r["name"][:200], r.get("country", "")[:8],
             r.get("currency", "")[:8], r.get("producer_code", "")[:60],
             r.get("incoterms", "")[:60], lead_val, r.get("payment_terms_default", "")[:120],
             r.get("contact_person", "")[:120], r.get("email", "")[:120],
             r.get("phone", "")[:50], r.get("notes", "")[:1000], kw),
        )
        imported += 1
    db.commit()
    return {"imported": imported}


def list_suppliers(db, q: str = "") -> list:
    ensure_columns(db)
    if q:
        # Escape wildcards LIKE (% _ \) — inaczej szukanie po '%'/'_' zwraca śmieci.
        esc = str(q).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{esc}%"
        rows = db.execute(
            "SELECT code, name, country, currency, producer_code, incoterms FROM suppliers "
            "WHERE COALESCE(active,1)=1 AND (code LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' "
            "OR producer_code LIKE ? ESCAPE '\\') "
            "ORDER BY name LIMIT 300", (like, like, like)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT code, name, country, currency, producer_code, incoterms FROM suppliers "
            "WHERE COALESCE(active,1)=1 ORDER BY name LIMIT 300"
        ).fetchall()
    return [dict(r) for r in rows]


def get_supplier(db, code) -> dict | None:
    ensure_columns(db)
    # BUGFIX: kod dostawcy to identyfikator — dopasowanie bez rozróżniania wielkości
    # liter (URL /api/sup-master/<code> jest sterowany przez użytkownika; import nie
    # normalizuje wielkości), więc 'shieldco' nie znajdował 'SHIELDCO'.
    row = db.execute("SELECT * FROM suppliers WHERE UPPER(code)=UPPER(?)",
                     (str(code or "").strip(),)).fetchone()
    return dict(row) if row else None


def update_contact(db, code, email=None, contact_person=None, phone=None) -> dict:
    """Aktualizuje dane kontaktowe dostawcy (e-mail/osoba/telefon). Tylko podane pola."""
    ensure_columns(db)
    code = str(code or "").strip()
    if not code:
        return {"error": "Brak kodu dostawcy"}
    row = db.execute("SELECT code FROM suppliers WHERE code=?", (code,)).fetchone()
    if not row:
        return {"error": "Nie znaleziono dostawcy"}
    sets, vals = [], []
    if email is not None:
        sets.append("email=?"); vals.append(str(email)[:120])
    if contact_person is not None:
        sets.append("contact_person=?"); vals.append(str(contact_person)[:120])
    if phone is not None:
        sets.append("phone=?"); vals.append(str(phone)[:60])
    if not sets:
        return {"error": "Brak pól do aktualizacji"}
    vals.append(code)
    # Bandit B608: nazwy kolumn z twardej białej listy w kodzie; wartości jako parametry ?.
    db.execute(f"UPDATE suppliers SET {', '.join(sets)} WHERE code=?", vals)  # nosec B608
    db.commit()
    return {"ok": True}


def supplier_for_producer(db, producer_code) -> dict | None:
    """Zwraca dostawcę powiązanego z danym kodem producenta (łącznik z materiałami)."""
    pc = str(producer_code or "").strip()
    if not pc:
        return None
    ensure_columns(db)
    # BUGFIX: łącznik materiał→dostawca po kodzie producenta był wrażliwy na wielkość
    # liter, a żaden import nie normalizuje wielkości — 'fg' nie łączył się z 'FG'.
    row = db.execute(
        "SELECT * FROM suppliers WHERE UPPER(producer_code)=UPPER(?) "
        "AND COALESCE(active,1)=1 LIMIT 1", (pc,)
    ).fetchone()
    return dict(row) if row else None
