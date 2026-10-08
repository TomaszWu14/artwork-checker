"""
ai_learning.py — samouczący się system profili dostawców.

Claude analizuje dokumenty PDF i automatycznie:
  1. Wykrywa dostawcę i typ dokumentu
  2. Mapuje kolumny tabeli (ref/qty/price/lot/net)
  3. Wyciąga wzorce nagłówkowe
  4. Identyfikuje normalne rozbieżności tego dostawcy
  5. Proponuje aktualizację profilu (NIE zapisuje bez zatwierdzenia)

Przepływ:
  PDF → learn_from_document() → propozycja → zatwierdź → apply_learned_profile()
  Wynik porównania → learn_from_comparison() → sugestie → apply_comparison_suggestions()
"""

import json, os, random, re, time, urllib.request, urllib.error
from datetime import datetime
from typing import Optional
import pdfplumber
from db import get_db
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL   = os.environ.get("AI_LEARNING_MODEL", "claude-sonnet-4-6")
DB_PATH        = "instance/doccompare.db"

_SYSTEM = """Jesteś ekspertem ds. dokumentacji handlowej ACME.
Analizujesz strukturę dokumentów i tworzysz profile dostawców.
Odpowiadaj WYŁĄCZNIE w JSON — bez markdown, bez komentarzy."""

# ── Klucz API ────────────────────────────────────────────────────────────────

def _key() -> str:
    k = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if k: return k
    try:
        db = get_db()
        try:
            r = db.execute("SELECT value FROM settings WHERE category='ai' AND key='anthropic_api_key'").fetchone()
        finally:
            db.close()
        if r and r[0]:
            os.environ["ANTHROPIC_API_KEY"] = r[0].strip()
            return r[0].strip()
    except Exception as _e:
        import logging
        logging.getLogger("ai_learning").debug("API key DB lookup failed: %s", _e)
    return ""

def _call(prompt: str, max_tokens: int = 3000) -> dict:
    key = _key()
    if not key: return {"error": "Brak ANTHROPIC_API_KEY"}
    payload = json.dumps({"model": CLAUDE_MODEL, "max_tokens": max_tokens,
                          "system": _SYSTEM,
                          "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(CLAUDE_API_URL, data=payload,
        headers={"Content-Type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"}, method="POST")
    # Up to 3 attempts with exponential backoff on transient errors (429/529,
    # network blips) so a single hiccup doesn't silently drop a learning result.
    last_error = ""
    for attempt in range(3):
        text = ""
        try:
            # Bandit B310: URL stały w kodzie (https://api.anthropic.com), schemat nie pochodzi z wejścia.
            with urllib.request.urlopen(req, timeout=60) as r:  # nosec B310
                data = json.loads(r.read())
                try:
                    from api_usage_tracker import record_usage
                    record_usage(CLAUDE_MODEL, data.get("usage", {}), call_type="ai_learning")
                except Exception:
                    pass
                _content = data.get("content") or []
                if not _content:
                    return {"error": "Empty API response content"}
                text = ""
                for _blk in _content:
                    if _blk.get("type") == "text" and _blk.get("text"):
                        text = _blk["text"]
                        break
                if not text:
                    return {"error": "Empty text in API response"}
                # Zdejmij ogradzający blok kodu TYLKO z początku/końca — globalny
                # sub kasował sekwencje ``` występujące wewnątrz wartości JSON.
                text = text.strip()
                text = re.sub(r"^```(?:json)?\s*", "", text)
                text = re.sub(r"\s*```$", "", text).strip()
                _parsed = json.loads(text)
                if not isinstance(_parsed, dict):
                    return {"error": "Nieoczekiwany format odpowiedzi AI (oczekiwano obiektu JSON)"}
                return _parsed
        except urllib.error.HTTPError as e:
            last_error = f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:300]}"
            if (e.code in (429, 529) or e.code >= 500) and attempt < 2:  # transient: rate limit / overloaded / 5xx
                _d = 2 * (2 ** attempt)
                time.sleep(_d + random.uniform(0, _d * 0.1))
                continue
            return {"error": last_error}
        except json.JSONDecodeError:
            m = re.search(r"\{[\s\S]+\}", text)
            if m:
                try: return json.loads(m.group())
                except Exception: pass
            return {"error": f"JSON parse error: {text[:200]}"}
        except Exception as e:
            last_error = str(e)[:200]
            if attempt < 2:                                  # transient network error
                _d = 2 * (2 ** attempt)
                time.sleep(_d + random.uniform(0, _d * 0.1))
                continue
            return {"error": last_error}
    return {"error": last_error or "AI call failed"}

# ── Ekstrakcja próbki z PDF ──────────────────────────────────────────────────

def _sample(path: str) -> dict:
    header_text, table_header, table_rows, total_row = "", [], [], []
    SIGS = {"qty","quantity","price","amount","net","lot","description","product","ref","s.no","code"}
    try:
        with pdfplumber.open(path) as pdf:
            header_text = (pdf.pages[0].extract_text() or "")[:4000]
            found = False
            for page in pdf.pages:
                if found: break
                for tbl in page.extract_tables():
                    if not tbl or len(tbl) < 3: continue
                    for ri, row in enumerate(tbl[:9]):
                        flat = " ".join(str(c or "").lower() for c in row)
                        if sum(1 for s in SIGS if s in flat) >= 2:
                            table_header = [str(c or "").replace("\n"," ").strip() for c in row]
                            for dr in tbl[ri+1:ri+7]:
                                if not dr or not any(c for c in dr): continue
                                rc = [str(c or "").replace("\n"," ").strip() for c in dr]
                                if any(t in " ".join(rc).lower() for t in ["total:","razem:"]):
                                    total_row = rc
                                elif len(table_rows) < 5:
                                    table_rows.append(rc)
                            found = True; break
    except Exception as e:
        header_text = header_text or f"[Błąd: {e}]"
    return {"header_text": header_text, "table_header": table_header,
            "table_rows": table_rows, "total_row": total_row}

# ── Nauka z dokumentu ────────────────────────────────────────────────────────

_DOC_PROMPT = """Przeanalizuj dokument handlowy ACME i zwróć profil dostawcy.

TEKST NAGŁÓWKOWY:
{header}

NAGŁÓWEK TABELI ({n} kolumn): {th}
PRZYKŁADOWE WIERSZE: {tr}
TOTAL: {tot}

ZASADY MAPOWANIA KOLUMN:
- "Quantity/Unit" lub "Qty" → "qty"  (NIE "unit")
- "Unit-price" lub "Unit price" → "price"  (NIE samo "unit")
- "LOT numbers" → "lot"
- "Amount" bez "unit" → "net"
- Pomiń: numery porządkowe, kody kreskowe → "skip"

Odpowiedz WYŁĄCZNIE w JSON:
{{
  "supplier": {{
    "code": "MAX12ZNAKOW",
    "name": "Pełna nazwa",
    "country": "CN",
    "currency": "USD",
    "language": "EN",
    "detect_keywords": ["słowo1", "słowo2"],
    "price_rounding": 4,
    "po_rounding": 2,
    "price_tolerance_pct": 0.5,
    "qty_tolerance_pct": 0.0,
    "date_format": "%Y/%m/%d",
    "notes": ""
  }},
  "doc_type": "PI",
  "column_mapping": {{
    "Product Code": "ref",
    "Description of goods": "description",
    "Quantity/Unit\\n(pouch/bag)": "qty",
    "LOT numbers": "lot",
    "Unit-price\\n(pouch/bag)": "price",
    "Amount": "net"
  }},
  "header_patterns": {{
    "payment_terms_raw": "100% T/T 60 days after shipment",
    "payment_terms_acme_code": "IW60",
    "date_format_detected": "%Y/%m/%d",
    "port_of_loading": "",
    "has_samples": false
  }},
  "known_issues": ["Ceny PI 4 miejsca vs PO 2 miejsca — norma"],
  "confidence": 0.90,
  "confidence_reason": "Jasna tabela"
}}"""


def learn_from_document(pdf_path: str, doc_type_hint: str = "auto",
                         db_path: str = DB_PATH) -> dict:
    """Analizuje PDF i zwraca propozycję profilu. NIE zapisuje — czeka na zatwierdzenie."""
    s = _sample(pdf_path)
    if not s["header_text"] and not s["table_header"]:
        return {"status": "error", "message": "Nie udało się wyciągnąć tekstu z PDF"}

    prompt = _DOC_PROMPT.format(
        header=s["header_text"][:3000],
        n=len(s["table_header"]),
        th=json.dumps(s["table_header"], ensure_ascii=False),
        tr=json.dumps(s["table_rows"], ensure_ascii=False),
        tot=json.dumps(s["total_row"], ensure_ascii=False),
    )
    ai = _call(prompt)
    if "error" in ai:
        return {"status": "error", "message": ai["error"]}

    code = ((ai.get("supplier") or {}).get("code") or "").upper().strip()
    if not code:
        return {"status": "error", "message": "Claude nie rozpoznał dostawcy"}

    doc_type = ai.get("doc_type") or doc_type_hint
    existing = _get_sup(code, db_path)
    proposal = _merge(ai.get("supplier") or {}, doc_type, ai.get("column_mapping") or {},
                      ai.get("header_patterns") or {}, ai.get("known_issues") or [], existing)
    changes  = _diff(existing, proposal) if existing else []
    status   = ("known_supplier" if existing and not changes
                else "update_supplier" if existing else "new_supplier")
    try:
        conf = float(ai.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0

    return {
        "status": status, "supplier_code": code, "doc_type": doc_type,
        "proposal": proposal, "existing": existing, "changes": changes,
        "confidence": conf, "confidence_reason": ai.get("confidence_reason",""),
        "message": _msg(status, code, doc_type, changes, conf), "raw_ai": ai,
    }


def _merge(sup: dict, doc_type: str, col_map: dict,
           hdr: dict, issues: list, base: Optional[dict]) -> dict:
    b = dict(base) if base else {}
    code = (sup.get("code") or b.get("code") or "UNKNOWN").upper()

    cm = dict(b.get("column_mappings") or {})
    if col_map and doc_type not in ("unknown","auto",""):
        cm[doc_type] = col_map

    ki, ki_s = list(b.get("known_issues",[])), set(b.get("known_issues",[]))
    for i in issues:
        if i not in ki_s: ki.append(i); ki_s.add(i)

    pt = dict(b.get("payment_terms_map") or {})
    if hdr.get("payment_terms_acme_code") and hdr.get("payment_terms_raw"):
        pt[hdr["payment_terms_acme_code"]] = hdr["payment_terms_raw"]

    kw, kw_s = list(b.get("detect_keywords",[])), {k.lower() for k in b.get("detect_keywords",[])}
    for k in sup.get("detect_keywords",[]):
        if k.lower() not in kw_s: kw.append(k); kw_s.add(k.lower())

    return {
        "code": code,
        "name": sup.get("name") or b.get("name", code),
        "country": sup.get("country") or b.get("country","XX"),
        "currency": sup.get("currency") or b.get("currency","USD"),
        "language": sup.get("language") or b.get("language","EN"),
        "price_rounding": (sup["price_rounding"] if sup.get("price_rounding") is not None
                           else b.get("price_rounding", 4)),
        "po_rounding": (sup["po_rounding"] if sup.get("po_rounding") is not None
                        else b.get("po_rounding", 2)),
        "price_tolerance_pct": (sup["price_tolerance_pct"] if sup.get("price_tolerance_pct") is not None
                                else b.get("price_tolerance_pct", 0.5)),
        "qty_tolerance_pct": (sup["qty_tolerance_pct"] if sup.get("qty_tolerance_pct") is not None
                              else b.get("qty_tolerance_pct", 0.0)),
        "date_format": sup.get("date_format") or b.get("date_format","%Y/%m/%d"),
        "notes": sup.get("notes") or b.get("notes",""),
        "active": 1,
        "detect_keywords": kw,
        "payment_terms_map": pt,
        "known_issues": ki,
        "column_mappings": cm,
        "synonyms": b.get("synonyms",[]),
        "header_patterns_json": hdr,
        "wizard_completed": b.get("wizard_completed",0),
    }


def _diff(old: dict, new: dict) -> list:
    ch = []
    for f in ["name","country","currency","price_rounding","price_tolerance_pct","date_format"]:
        ov,nv = old.get(f), new.get(f)
        if nv is not None and str(ov) != str(nv):
            ch.append({"field":f,"old":ov,"new":nv,"type":"update"})
    ocm = old.get("column_mappings",{}) or {}
    ncm = new.get("column_mappings",{}) or {}
    for dt,m in ncm.items():
        if dt not in ocm: ch.append({"field":f"column_mappings.{dt}","old":None,"new":m,"type":"new_mapping"})
        elif ocm[dt]!=m:  ch.append({"field":f"column_mappings.{dt}","old":ocm[dt],"new":m,"type":"update_mapping"})
    added = set(new.get("known_issues",[])) - set(old.get("known_issues",[]))
    if added: ch.append({"field":"known_issues","old":None,"new":list(added),"type":"new_issues"})
    return ch


def _msg(status,code,doc_type,changes,conf):
    p = int(conf*100)
    if status=="new_supplier": return f"Nowy dostawca: {code} (pewność {p}%). Format: {doc_type}."
    if status=="update_supplier": return f"{code}: {len(changes)} aktualizacji z {doc_type} (pewność {p}%)."
    return f"{code}: brak nowych informacji z {doc_type}."

# ── Nauka z porównania ────────────────────────────────────────────────────────

_COMP_PROMPT = """Przeanalizuj wyniki porównania dokumentów dla dostawcy {code}.

PROFIL:
{profile}

ZNALEZISKA:
{findings}

AI OCENA: {ai_sum}

Oceń czy wzorce znalezisk wskazują na potrzebę aktualizacji profilu.
Odpowiedz WYŁĄCZNIE w JSON:
{{
  "should_update": true,
  "urgency": "high|medium|low|none",
  "suggested_updates": [
    {{"field":"price_rounding","current_value":2,"suggested_value":4,
      "reason":"Dostawca używa 4 miejsc","evidence":"0.0257 zamiast 0.03"}}
  ],
  "new_known_issues": ["Ceny PI 4 miejsca vs PO 2 — norma"],
  "column_mapping_fix": {{"detected":false,"doc_type":"PI","problem":"","correct_mapping":{{}}}},
  "false_alarms_count": 0,
  "summary": "Krótkie podsumowanie"
}}"""


def learn_from_comparison(comparison_result: dict, supplier_code: str,
                           db_path: str = DB_PATH) -> dict:
    """Po porównaniu — AI sugeruje aktualizacje profilu. NIE zapisuje automatycznie."""
    existing = _get_sup(supplier_code, db_path)
    if not existing:
        return {"status":"no_supplier","message":f"Dostawca {supplier_code} nie istnieje"}

    findings = []
    modules = comparison_result.get("modules",{})
    for mod in ("enhanced","table"):
        for it in (modules.get(mod,{}).get("items",[]) or
                   modules.get(mod,{}).get("compared",[]) or [])[:20]:
            if it.get("status") not in ("ok","format"):
                findings.append(f"[{it.get('status','?')}] {it.get('ref','?')}: "
                                 f"{'; '.join(str(i) for i in (it.get('issues') or [])[:3])}")
    for h in (modules.get("enhanced",{}).get("headers",[]) or []):
        if h.get("status") not in ("ok","format"):
            findings.append(f"[{h.get('status')}] {h.get('key','')}: "
                             f"{h.get('val_a','')} vs {h.get('val_b','')}")

    if not findings:
        return {"status":"no_updates","supplier":supplier_code,
                "message":"Brak znalezisk do analizy."}

    ai_sum = (comparison_result.get("ai_validation") or {}).get("overall_summary","")
    prompt = _COMP_PROMPT.format(
        code=supplier_code,
        profile=json.dumps({k:v for k,v in existing.items()
                            if k in ("price_rounding","po_rounding","price_tolerance_pct",
                                     "payment_terms_map","known_issues","column_mappings","date_format")},
                           ensure_ascii=False, indent=2),
        findings="\n".join(findings[:30]),
        ai_sum=ai_sum or "Brak",
    )
    ai = _call(prompt, max_tokens=2000)
    if "error" in ai:
        return {"status":"error","supplier":supplier_code,"message":ai["error"]}
    if ai.get("should_update"):
        _save_pending(supplier_code, ai, db_path)
    return {
        "status": "suggestions_ready" if ai.get("should_update") else "no_updates",
        "supplier": supplier_code, "result": ai,
        "message": ai.get("summary","Analiza zakończona."),
    }

# ── Zatwierdzanie ─────────────────────────────────────────────────────────────

def apply_learned_profile(supplier_code: str, proposal: dict,
                           approved_by: str = "system", db_path: str = DB_PATH) -> dict:
    """Zapisuje zatwierdzony profil dostawcy do bazy."""
    if not supplier_code or not proposal:
        return {"status":"error","message":"Brak kodu lub propozycji"}
    try:
        from supplier_profiles import save_supplier
        saved = save_supplier(proposal, changed_by=approved_by, db_path=db_path)
        v = saved.get("profile_version","?")
        return {"status":"saved","supplier":supplier_code,"version":v,
                "message":f"Profil {supplier_code} zapisany (wersja {v})"}
    except Exception as e:
        return {"status":"error","message":str(e)[:200]}


def apply_comparison_suggestions(supplier_code: str, suggestions: dict,
                                   approved_fields: Optional[list] = None,
                                   approved_by: str = "system",
                                   db_path: str = DB_PATH) -> dict:
    """Stosuje zatwierdzone sugestie. approved_fields=None → zatwierdź wszystko."""
    existing = _get_sup(supplier_code, db_path)
    if not existing:
        return {"status":"error","message":f"Dostawca {supplier_code} nie istnieje"}

    updated = dict(existing)
    applied = []

    _SAFE_FIELDS = frozenset({
        "payment_terms_map", "payment_terms_json",
        "price_tolerance_pct", "qty_tolerance_pct",
        "date_format", "notes",
    })
    for upd in (suggestions.get("suggested_updates") or []):
        f, nv = upd.get("field"), upd.get("suggested_value")
        if not f or nv is None: continue
        if f not in _SAFE_FIELDS: continue
        if approved_fields and f not in approved_fields: continue
        updated[f] = nv; applied.append(f)

    new_ki = suggestions.get("new_known_issues",[])
    if new_ki and (approved_fields is None or "known_issues" in approved_fields):
        current_issues = list(updated.get("known_issues",[]))
        # FIX 7: Deduplicate case-insensitively before appending
        existing_normalized = {i.lower().strip() for i in current_issues if i}
        for issue in new_ki:
            if not issue:
                continue
            if issue.lower().strip() not in existing_normalized:
                current_issues.append(issue)
                existing_normalized.add(issue.lower().strip())
        updated["known_issues"] = current_issues; applied.append("known_issues")

    cmf = suggestions.get("column_mapping_fix") or {}
    if cmf.get("detected") and cmf.get("correct_mapping"):
        if approved_fields is None or "column_mappings" in approved_fields:
            dt = cmf.get("doc_type","PI")
            cm = dict(updated.get("column_mappings") or {})
            cm[dt] = cmf["correct_mapping"]
            updated["column_mappings"] = cm; applied.append(f"column_mappings.{dt}")

    if not applied:
        return {"status":"nothing_applied","message":"Brak zatwierdzonych zmian"}
    r = apply_learned_profile(supplier_code, updated, approved_by, db_path)
    r["applied_fields"] = applied
    return r

# ── Pending updates ───────────────────────────────────────────────────────────

def _save_pending(code: str, data: dict, db_path: str):
    try:
        db = get_db()
        try:
            # Unique key per call — prevents rapid calls from overwriting each other
            key = f"{code}_{int(time.time())}_{random.randint(1000, 9999)}"
            db.execute("INSERT INTO settings(category,key,value) VALUES(?,?,?) "
                       "ON CONFLICT(category,key) DO UPDATE SET value=excluded.value",
                       ("ai_pending", key, json.dumps(data, ensure_ascii=False)))
            db.execute("INSERT INTO settings(category,key,value) VALUES(?,?,?) "
                       "ON CONFLICT(category,key) DO UPDATE SET value=excluded.value",
                       ("ai_learning","last_learned_at",datetime.now().isoformat()))
            # Prune: trzymaj tylko 200 najnowszych sugestii (inaczej tabela settings
            # rośnie bez ograniczeń — sugestie i tak są przeglądane/zatwierdzane).
            db.execute(
                "DELETE FROM settings WHERE category='ai_pending' AND id NOT IN "
                "(SELECT id FROM settings WHERE category='ai_pending' ORDER BY id DESC LIMIT 200)"
            )
            db.commit()
        finally:
            db.close()
    except Exception: pass


def get_pending_updates(db_path: str = "instance/doccompare.db") -> list:
    try:
        db = get_db()
        try:
            rows = db.execute("SELECT key,value FROM settings WHERE category='ai_pending' "
                              "ORDER BY id DESC LIMIT 500").fetchall()
        finally:
            db.close()
    except Exception: return []
    result = []
    for row in rows:
        try:
            d = json.loads(row["value"])
            # Key format: {code}_{timestamp}_{rand} — use rsplit to handle codes with underscores
            parts = row["key"].rsplit("_", 2)
            supplier_code = "_".join(parts[:-2]) if len(parts) > 2 else row["key"]
            # parts[-2] to epoch (sekundy) — formatujemy na czytelną datę,
            # a nie surowy timestamp prezentowany w UI.
            created_at = ""
            if len(parts) > 2:
                try:
                    created_at = datetime.fromtimestamp(int(parts[-2])).strftime("%Y-%m-%d %H:%M")
                except (ValueError, OverflowError, OSError):
                    created_at = parts[-2]
            result.append({"id":row["key"],
                           "supplier_code": supplier_code,
                           "created_at": created_at,
                           "urgency": d.get("urgency","low"),
                           "summary": d.get("summary",""),
                           "updates_count": len(d.get("suggested_updates",[])),
                           "data": d})
        except Exception: continue
    return result


def dismiss_pending(pending_id: str, db_path: str = "instance/doccompare.db") -> bool:
    try:
        db = get_db()
        try:
            db.execute("DELETE FROM settings WHERE category='ai_pending' AND key=?", (pending_id,))
            db.commit()
        finally:
            db.close()
        return True
    except Exception: return False

# ── Statystyki ────────────────────────────────────────────────────────────────

def get_learning_stats(db_path: str = "instance/doccompare.db") -> dict:
    try:
        db = get_db()
        try:
            total = db.execute("SELECT COUNT(*) FROM suppliers WHERE active=1").fetchone()[0]
            with_m = db.execute("SELECT COUNT(*) FROM suppliers WHERE column_mapping_json IS NOT NULL "
                                "AND column_mapping_json != '{}'").fetchone()[0]
            try: pending = db.execute("SELECT COUNT(*) FROM settings WHERE category='ai_pending'").fetchone()[0]
            except Exception: pending = 0
            try:
                lr = db.execute("SELECT value FROM settings WHERE category='ai_learning' AND key='last_learned_at'").fetchone()
                last = lr["value"] if lr else None
            except Exception: last = None
        finally:
            db.close()
        return {"suppliers_total":total,"suppliers_with_mapping":with_m,
                "suppliers_without_mapping":total-with_m,
                "pending_updates":pending,"last_learned_at":last,
                "learning_active":bool(_key())}
    except Exception as e: return {"error":str(e)[:200]}

# ── Helper ────────────────────────────────────────────────────────────────────

def _norm_sup_keys(d: Optional[dict]) -> Optional[dict]:
    """Normalize a supplier dict to the key schema read by _merge/_diff.

    supplier_profiles.get_supplier() returns raw *_json keys
    (column_mapping_json / payment_terms_json / …), but _merge/_diff read
    column_mappings / payment_terms_map / known_issues / detect_keywords /
    synonyms. Without this remap the preferred path silently dropped existing
    mappings."""
    if not d:
        return d
    _aliases = [
        ("column_mapping_json", "column_mappings", {}),
        ("payment_terms_json",  "payment_terms_map", {}),
        ("known_issues_json",   "known_issues", []),
        ("detect_keywords_json", "detect_keywords", []),
        ("synonyms_json",       "synonyms", []),
    ]
    for src, dst, dflt in _aliases:
        if dst not in d:
            d[dst] = d.get(src, dflt)
    return d


def _get_sup(code: str, db_path: str) -> Optional[dict]:
    try:
        from supplier_profiles import get_supplier
        return _norm_sup_keys(get_supplier(code.upper(), db_path))
    except Exception: pass
    try:
        db = get_db()
        try:
            row = db.execute("SELECT * FROM suppliers WHERE code=?", (code.upper(),)).fetchone()
        finally:
            db.close()
        if not row: return None
        d = dict(row)
        for f,dflt in [("payment_terms_json",{}),("known_issues_json",[]),
                       ("detect_keywords_json",[]),("column_mapping_json",{}),("synonyms_json",[])]:
            try: d[f] = json.loads(d.get(f) or "null") or dflt
            except Exception: d[f] = dflt
        d["payment_terms_map"]=d.pop("payment_terms_json",{})
        d["known_issues"]=d.pop("known_issues_json",[])
        d["detect_keywords"]=d.pop("detect_keywords_json",[])
        d["column_mappings"]=d.pop("column_mapping_json",{})
        d["synonyms"]=d.pop("synonyms_json",[])
        return d
    except Exception: return None
