"""
ai_validator.py — walidacja wyników porównania przez Claude API.

WYMAGANIA:
  ANTHROPIC_API_KEY=sk-ant-... (env lub baza settings)

UŻYCIE:
  from ai_validator import validate_comparison_result, check_api_key
"""

import json, logging, os, random, re, time, urllib.request, urllib.error
from typing import Optional
from db import get_db

logger = logging.getLogger(__name__)

CLAUDE_API_URL   = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL      = os.environ.get("AI_HAIKU_MODEL", "claude-haiku-4-5-20251001")   # fast/cheap — pre-screen
CLAUDE_MODEL_PRO  = os.environ.get("AI_SONNET_MODEL", "claude-sonnet-4-6")          # full analysis when needed
CLAUDE_MODEL_OPUS = os.environ.get("AI_OPUS_MODEL",   "claude-opus-4-8")            # najtrudniejsze przypadki
MAX_TOKENS       = 4000
API_TIMEOUT      = 60
HYBRID_THRESHOLD = 25   # risk_score <= this → return Haiku result, skip Sonnet
OPUS_THRESHOLD   = 70   # risk_score >= this → eskalacja do Opus (zamiast Sonnet)

SYSTEM_PROMPT = """Jesteś starszym ekspertem ds. dokumentacji handlowej i celnej \
w firmie ACME sp. z o.o.

Firma porównuje dokumenty: PO (własne SAP) ↔ PI/CI (od dostawcy) ↔ PL ↔ SAD ↔ BL.

Twoje zadanie: WYKRYJ WSZYSTKIE REALNIE BŁĘDNE POLA. Bądź rygorystyczny.

NORMALNE ROZBIEŻNOŚCI (akceptowalne — nie zgłaszaj jako error):
  1. Ceny PO 2 miejsca ≠ PI 4 miejsca gdy NET ≤ 0.01 USD różnicy — to zaokrąglenie
  2. IW04="30 days after departure" ≡ IW04="30 days net", IZ31="30% advance"
  3. Adresy ACME: Przemysłowa 10 = Logistyczna 2 = Magazynowa 20 — wszystkie OK
  4. Próbka z ceną 0.01 USD — pomiń przy sumowaniu (oznacz jako info)
  5. Opis EN = opis PL (Nelaton catheter = Cewnik Nelaton) — semantycznie to samo
  6. Zaokrąglenie ceny PO do 2 miejsc gdy qty×cena≈net do 0.01 USD — norma

BŁĘDY KRYTYCZNE (zawsze error, nigdy nie akceptuj):
  1. Różna ilość qty — nawet 1 sztuka to ERROR
  2. I↔1 lub O↔0 w numerach dokumentów, LOT, REF — ERROR
  3. Różny numer PO między dokumentami — ERROR
  4. Różna waluta (EUR vs USD) — ERROR
  5. NET różny > 0.5% gdy cena też różna (nie zaokrąglenie) — ERROR
  6. Brakująca pozycja w PI/CI której jest w PO — ERROR
  7. Różne warunki płatności advance vs net (np. "30% advance" vs "net 30") — ERROR
  8. Różna data dostawy o >7 dni — WARNING (nie error)
  9. Różny numer kontenera — ERROR jeśli w obu dokumentach wpisany

WAŻNE: Jeśli widzisz rozbieżność której nie możesz wyjaśnić zaokrągleniem ani
synonimem — ZAWSZE zgłoś jako error lub warning. Lepiej zgłosić false positive
niż przeoczyć prawdziwy błąd w dokumentach handlowych.

Odpowiadaj WYŁĄCZNIE w tym JSON (bez markdown):
{
  "overall_risk": "ok|warning|error|critical",
  "risk_score": 0-100,
  "overall_summary": "2-3 zdania po polsku",
  "document_identification": {
    "doc_a_type": "PO", "doc_a_number": "",
    "doc_b_type": "PI", "doc_b_number": "",
    "supplier": "", "po_number": "", "container": "",
    "total_items_checked": 0
  },
  "critical_errors": [
    {"index": 0, "category": "ilosc|cena|wartosc|lot|platnosc|celny",
     "field": "", "val_doc_a": "", "val_doc_b": "",
     "difference": "", "impact": "", "action": "", "severity": "error"}
  ],
  "warnings": [
    {"index": 0, "category": "platnosc|format|zaokraglenie|inne",
     "field": "", "val_doc_a": "", "val_doc_b": "",
     "comment": "", "severity": "warning"}
  ],
  "ok_items": [{"field": "", "value": "", "comment": ""}],
  "financial_summary": {
    "total_doc_a": "", "total_doc_b": "",
    "difference": "", "difference_pct": "",
    "assessment": "ok|minor_diff|significant_diff|critical_diff"
  },
  "validated_findings": [
    {"original_index": 0, "is_real_issue": true,
     "adjusted_severity": "critical|error|warning|info|ok",
     "root_cause": "", "action_required": "", "ai_comment": ""}
  ],
  "checklist": {
    "quantities_match": true, "prices_match": true, "totals_match": true,
    "payment_terms_match": true, "delivery_terms_match": true,
    "lot_numbers_match": true, "expiry_dates_match": true,
    "container_match": true, "po_number_match": true, "addresses_match": true
  },
  "report_pl": "Pełny raport po polsku (5-8 zdań).",
  "recommendation": "APPROVED|APPROVED_WITH_NOTES|HOLD_PENDING_CLARIFICATION|REJECT",
  "recommendation_reason": "Uzasadnienie po polsku"
}"""


# ── Klucz API ────────────────────────────────────────────────────────────────

# FIX 12: Module-level cache for API key (TTL = 300 s)
_api_key_cache = [None, 0.0]  # [key, timestamp]
_API_KEY_CACHE_TTL = 300


def _get_api_key() -> str:
    # Fast path: env var (never stale)
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    # Check TTL cache before hitting DB
    now = time.time()
    if _api_key_cache[0] and (now - _api_key_cache[1]) < _API_KEY_CACHE_TTL:
        return _api_key_cache[0]
    try:
        db = get_db()
        try:
            row = db.execute(
                "SELECT value FROM settings WHERE category='ai' AND key='anthropic_api_key'"
            ).fetchone()
            if row and row[0]:
                key = row[0].strip()
                if key:
                    os.environ["ANTHROPIC_API_KEY"] = key
                    _api_key_cache[0] = key
                    _api_key_cache[1] = time.time()
                    return key
        finally:
            db.close()
    except Exception:
        pass
    return ""


def is_api_key_set() -> dict:
    """Lightweight check — only verifies the key exists in env/DB, no live API call."""
    key = _get_api_key()
    if not key:
        return {"ok": False, "api_key_set": False}
    return {"ok": True, "api_key_set": True, "model": CLAUDE_MODEL, "key_prefix": key[:16] + "..."}


def check_api_key() -> dict:
    key = _get_api_key()
    if not key:
        return {
            "ok": False,
            "error": "Brak klucza ANTHROPIC_API_KEY",
            "hint": "Ustaw env: $env:ANTHROPIC_API_KEY='sk-ant-...' lub skonfiguruj w panelu admin."
        }
    if not key.startswith("sk-ant-"):
        return {"ok": False, "error": "Nieprawidłowy format klucza (powinien zaczynać się od sk-ant-)"}
    result = _call_claude('test', system='Odpowiedz: {"ok":true}', max_tokens=20)
    if isinstance(result, dict) and "error" not in result:
        return {"ok": True, "model": CLAUDE_MODEL, "key_prefix": key[:16] + "..."}
    return {"ok": False, "error": result.get("error", "Błąd API")}


# ── Wywołanie API ─────────────────────────────────────────────────────────────

def _extract_json_object(text: str):
    """Return the first balanced top-level {...} JSON object parsed from text,
    or None. More robust than a greedy `\\{.*\\}` regex, which over-captures
    trailing data and misinterprets text after the object."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    return None
    return None


def _call_claude(prompt: str, system: str = SYSTEM_PROMPT,
                  max_tokens: int = MAX_TOKENS) -> dict:
    api_key = _get_api_key()
    if not api_key:
        return {"error": "Brak klucza API. Ustaw ANTHROPIC_API_KEY."}

    payload = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(
        CLAUDE_API_URL, data=payload,
        headers={
            "Content-Type":      "application/json",
            "x-api-key":         api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    last_error = ""
    for attempt in range(2):
        text = ""
        try:
            # Bandit B310: URL stały w kodzie (https://api.anthropic.com), schemat nie pochodzi z wejścia.
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:  # nosec B310
                data = json.loads(resp.read())
                try:
                    from api_usage_tracker import record_usage
                    record_usage(CLAUDE_MODEL, data.get("usage", {}), call_type="doc_validation")
                except Exception:
                    pass
                _content = data.get("content") or []
                if not _content:
                    last_error = "Empty API response content"
                    break
                text = ""
                for _blk in _content:
                    if _blk.get("type") == "text" and _blk.get("text"):
                        text = _blk["text"]
                        break
                if not text:
                    last_error = "Non-text content block in API response"
                    break
                # Zdejmij ogradzający blok kodu TYLKO z początku/końca — globalny
                # sub kasował sekwencje ``` wewnątrz wartości JSON (np. report_pl),
                # psując parsowanie (jak naprawiono w ai_learning.py).
                text = text.strip()
                text = re.sub(r"^```(?:json)?\s*", "", text)
                text = re.sub(r"\s*```$", "", text).strip()
                return json.loads(text)

        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            last_error = f"HTTP {e.code}: {body[:400]}"
            # Ponawiamy także przy transientnych 5xx (500/503/529), nie tylko 429.
            if (e.code in (429, 529) or e.code >= 500) and attempt == 0:
                # FIX 11: Add jitter to retry backoff
                _delay = 4
                time.sleep(_delay + random.uniform(0, _delay * 0.1))
                continue
            break

        except json.JSONDecodeError:
            _obj = _extract_json_object(text)
            if _obj is not None:
                return _obj
            last_error = f"Błąd parsowania JSON: {text[:200]}"
            break

        except Exception as e:
            last_error = str(e)[:200]
            if attempt == 0:
                # FIX 11: Add jitter to retry backoff
                _delay = 2
                time.sleep(_delay + random.uniform(0, _delay * 0.1))
                continue
            break

    return {"error": last_error, "heuristic": True}


def _call_claude_pro(prompt: str, system: str = SYSTEM_PROMPT,
                     max_tokens: int = MAX_TOKENS, model: str = None) -> dict:
    """Call a more capable model for deep analysis (Sonnet domyślnie, Opus dla
    najtrudniejszych przypadków przez parametr `model`)."""
    api_key = _get_api_key()
    if not api_key:
        return {"error": "Brak klucza API."}
    _model = model or CLAUDE_MODEL_PRO
    payload = json.dumps({
        "model": _model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        CLAUDE_API_URL, data=payload,
        headers={
            "Content-Type":      "application/json",
            "x-api-key":         api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    last_error = ""
    for attempt in range(2):
        text = ""
        try:
            # Bandit B310: URL stały w kodzie (https://api.anthropic.com), schemat nie pochodzi z wejścia.
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:  # nosec B310
                data = json.loads(resp.read())
                try:
                    from api_usage_tracker import record_usage
                    record_usage(_model, data.get("usage", {}), call_type="doc_validation_pro")
                except Exception:
                    pass
                _content = data.get("content") or []
                if not _content:
                    last_error = "Empty API response content"
                    break
                text = ""
                for _blk in _content:
                    if _blk.get("type") == "text" and _blk.get("text"):
                        text = _blk["text"]
                        break
                if not text:
                    last_error = "Empty text in API response"
                    break
                # Zdejmij ogradzający blok kodu TYLKO z początku/końca — globalny
                # sub kasował sekwencje ``` wewnątrz wartości JSON (np. report_pl),
                # psując parsowanie (jak naprawiono w ai_learning.py).
                text = text.strip()
                text = re.sub(r"^```(?:json)?\s*", "", text)
                text = re.sub(r"\s*```$", "", text).strip()
                return json.loads(text)

        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            last_error = f"HTTP {e.code}: {body[:400]}"
            # Ponawiamy 429 oraz transientne 5xx (500/503/529); trwałe 4xx — nie.
            if (e.code in (429, 529) or e.code >= 500) and attempt == 0:
                _delay = 4
                time.sleep(_delay + random.uniform(0, _delay * 0.1))
                continue
            # Do not retry permanent errors (4xx except 429) — they won't resolve on retry
            break

        except json.JSONDecodeError:
            _obj = _extract_json_object(text)
            if _obj is not None:
                return _obj
            last_error = f"Błąd parsowania JSON: {text[:200]}"
            break

        except Exception as e:
            last_error = str(e)[:200]
            if attempt == 0:
                _delay = 2
                time.sleep(_delay + random.uniform(0, _delay * 0.1))
                continue
            break

    return {"error": last_error, "heuristic": True}


HAIKU_SCREEN_SYSTEM = (
    "Jesteś asystentem wstępnej oceny dokumentów handlowych. "
    "Odpowiedz WYŁĄCZNIE w JSON: "
    '{\"risk_score\":0-100, \"needs_deep_analysis\":true|false, \"reason\":\"1 zdanie\"}'
)

def _haiku_prescreen(prompt: str) -> dict:
    """Quick Haiku pre-screen: returns risk_score and whether full Sonnet analysis is needed."""
    screen_prompt = (
        "Oceń ryzyko rozbieżności w dokumentach.\n"
        "risk_score=0 (brak błędów) .. 100 (krytyczne błędy).\n"
        "needs_deep_analysis=true gdy risk_score>" + str(HYBRID_THRESHOLD) + ".\n\n"
        + prompt[:3000]  # truncate for speed
    )
    # 200 (nie 80) — krótki JSON z 1-zdaniowym „reason" mieścił się ledwo, a po
    # ucięciu (stop_reason=max_tokens) parsowanie padało → prescreen domyślnie
    # eskalował wszystko do Sonneta, zawyżając koszt. Haiku jest tani, więc 200 ok.
    result = _call_claude(screen_prompt, system=HAIKU_SCREEN_SYSTEM, max_tokens=200)
    if "error" in result:
        return {"risk_score": 100, "needs_deep_analysis": True, "reason": "screen error"}
    return result


def _run_hybrid(prompt: str) -> dict:
    """Run the Haiku pre-screen, pick Haiku vs Sonnet, and attach an audit trail
    (_ai_decision) so a manager can see *why* a comparison was or wasn't escalated
    to deep analysis. Centralises the previously-duplicated hybrid logic."""
    screen   = _haiku_prescreen(prompt)
    try:
        score = int(float(screen.get("risk_score", 0)))
    except (TypeError, ValueError):
        score = 100   # nieczytelny score → traktuj jako wysokie ryzyko
    reason   = screen.get("reason", "")
    escalate = bool(screen.get("needs_deep_analysis", True))
    # Najtrudniejsze przypadki (score ≥ OPUS_THRESHOLD) → Opus; średnie → Sonnet;
    # niskie → szybkie Haiku.
    if score >= OPUS_THRESHOLD:
        result, model = _call_claude_pro(prompt, model=CLAUDE_MODEL_OPUS), "opus"
        escalate = True
    elif escalate:
        result, model = _call_claude_pro(prompt), "sonnet"
    else:
        result, model = _call_claude(prompt), "haiku"
    if isinstance(result, dict):
        result["_model_used"]  = model
        result["_screen_score"] = score
        result["_ai_decision"] = {
            "model_used":    model,
            "screen_score":  score,
            "screen_reason": reason,
            "threshold":     HYBRID_THRESHOLD,
            "opus_threshold": OPUS_THRESHOLD,
            "escalated":     escalate,
            "why": (f"Eskalacja do Opus: wstępny score {score} ≥ próg {OPUS_THRESHOLD}"
                    if model == "opus" else
                    f"Eskalacja do Sonnet: wstępny score {score} > próg {HYBRID_THRESHOLD}"
                    if escalate else
                    f"Bez eskalacji: wstępny score {score} ≤ próg {HYBRID_THRESHOLD}"),
        }
    return result


# ── Budowanie promptu ─────────────────────────────────────────────────────────

def _trunc(v, n=400) -> str:
    s = str(v or "")
    return s[:n] + ("…" if len(s) > n else "")


# FIX 13: Increased max_items from 50 to 100 for more findings in AI prompt
def _fmt_findings(findings: list, max_items: int = 100) -> str:
    # Sanitise document-derived text before embedding it in the prompt: strip
    # control chars and collapse whitespace so malicious cell content can't break
    # out of the line structure or inject instructions (prompt injection).
    def _san(v):
        return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f]", " ", str(v or ""))).strip()
    lines = []
    for i, f in enumerate(findings[:max_items]):
        if not isinstance(f, dict):
            continue
        lines.append(
            f"[{i}] sev={_san(f.get('severity') or f.get('status','?'))}"
            f" cat={_san(f.get('category') or f.get('type',''))}"
            f" ref={_san(f.get('ref',''))}"
            f" field='{_trunc(_san(f.get('field_name') or f.get('field') or f.get('key','')),80)}'"
            f" A='{_trunc(_san(f.get('val_a') or f.get('doc_a','')),120)}'"
            f" B='{_trunc(_san(f.get('val_b') or f.get('doc_b','')),120)}'"
            f" desc='{_trunc(_san(f.get('description') or f.get('comment','')),200)}'"
        )
    return "\n".join(lines)


def _build_prompt(result_dict: dict, findings: list, mode: str) -> str:
    dt_a  = result_dict.get("doc_type_a") or result_dict.get("doc_type_detected") or "DOK_A"
    dt_b  = result_dict.get("doc_type_b") or "DOK_B"
    # Sanitize filenames: cap length and strip newlines to prevent prompt injection
    fa    = (result_dict.get("file_a", "") or "Dokument A")[:80].replace('\n', ' ').replace('\r', ' ')
    fb    = (result_dict.get("file_b", "") or "Dokument B")[:80].replace('\n', ' ').replace('\r', ' ')
    n    = len(findings)
    shown = min(n, 100)

    return f"""Przeanalizuj wyniki porównania dokumentów ACME.

DOKUMENTY:
  A: [{dt_a}] — {fa},  dostawca: {str(result_dict.get('supplier_detected','') or '')[:40].replace(chr(10),' ')}
  B: [{dt_b}] — {fb}
  Total A: {result_dict.get('total_a','')}  Total B: {result_dict.get('total_b','')}
  PO: {result_dict.get('po_number','')}

WAŻNE: W polach report_pl, overall_summary, critical_errors, warnings — używaj \
nazwy pliku ("{fa}" zamiast "Dokument A", "{fb}" zamiast "Dokument B").

STATYSTYKI:
  ok={result_dict.get('ok_count',0)} format={result_dict.get('format_count',0)} \
warn={result_dict.get('warn_count',0)} diff={result_dict.get('diff_count',0)}
  Znalezisk: {n} | Pokazuję: {shown} | Tryb: {mode}

ZNALEZISKA:
{_fmt_findings(findings)}

Oceń każde znalezisko i wygeneruj raport. Odpowiedz WYŁĄCZNIE w JSON."""


# ── Walidatory ────────────────────────────────────────────────────────────────

def _empty_ok(msg="Dokumenty zgodne.") -> dict:
    return {
        "overall_risk": "ok", "risk_score": 0,
        "overall_summary": msg,
        "document_identification": {}, "critical_errors": [],
        "warnings": [], "ok_items": [],
        "financial_summary": {"assessment": "ok"},
        "validated_findings": [],
        "checklist": {k: True for k in [
            "quantities_match","prices_match","totals_match",
            "payment_terms_match","delivery_terms_match",
        ]},
        "report_pl": msg,
        "recommendation": "APPROVED",
        "recommendation_reason": "Brak rozbieżności.",
    }


def _validate_table(rd: dict) -> dict:
    items   = rd.get("items", []) or []
    headers = rd.get("headers", []) or []

    item_f = [
        {"severity": "error" if it.get("status")=="roznica" else "warning",
         "category": "pozycja", "field_name": f"Pozycja {it.get('ref','?')}",
         "ref": it.get("ref","?"),
         "val_a": f"qty={it.get('qty_a')} price={it.get('price_a')} net={it.get('net_a')}",
         "val_b": f"qty={it.get('qty_b')} price={it.get('price_b')} net={it.get('net_b')}",
         "description": "; ".join(str(x) for x in (it.get("issues") or [])[:3])}
        for it in items if it.get("status") not in ("ok","format")
    ]
    hdr_f = [
        {"severity": "error" if h.get("status")=="roznica" else "warning",
         "category": "nagłówek", "field_name": h.get("key",""),
         "val_a": h.get("val_a",""), "val_b": h.get("val_b",""),
         "description": h.get("comment","")}
        for h in headers if h.get("status") not in ("ok","format")
    ]
    all_f = item_f + hdr_f
    if not all_f:
        return _empty_ok("Brak rozbieżności — dokumenty w pełni zgodne.")
    full_prompt = _build_prompt(rd, all_f, "table")
    return _run_hybrid(full_prompt)


def _validate_regex(rd: dict) -> dict:
    fields = rd.get("fields") or rd.get("errors") or rd.get("headers") or []
    if not fields:
        return _empty_ok("Brak rozbieżności w polach nagłówkowych.")
    full_prompt = _build_prompt(rd, fields, "regex")
    return _run_hybrid(full_prompt)


def _validate_typo(rd: dict) -> dict:
    findings = rd.get("findings", [])
    if not findings:
        return _empty_ok("Brak literówek ani zamian I↔1, O↔0.")
    lines = [
        f"[{i}] sev={f.get('severity','?')} | {_trunc(f.get('description',''),200)}"
        for i, f in enumerate(findings[:40]) if isinstance(f, dict)
    ]
    prompt = (
        f"Oceń wyniki analizy literówek ACME.\n"
        f"A='{rd.get('file_a','')}' B='{rd.get('file_b','')}'\n"
        f"Znalezisk: {len(findings)}\n\n" +
        "\n".join(lines) +
        "\n\nI↔1 lub O↔0 w numerach → KRYTYCZNY BŁĄD. Odpowiedz WYŁĄCZNIE w JSON."
    )
    return _run_hybrid(prompt)


# ── Główna funkcja publiczna ──────────────────────────────────────────────────

def validate_comparison_result(result_dict: dict, mode: str = "auto") -> dict:
    """
    Waliduje wyniki porównania przez Claude API.

    Dodaje do result_dict:
      - ai_validation (dict)
      - ai_adjusted_risk (str, opcjonalnie)
      - ai_recommendation (str, opcjonalnie)
    """
    # Budżet AI: gdy miesięczny limit przekroczony, pomijamy walidację AI
    # (walidacja regułowa pozostaje). Niska/zerowa konfiguracja limitu = bez bramki.
    try:
        from api_usage_tracker import budget_allows_optional_ai
        if not budget_allows_optional_ai():
            result_dict["ai_validation"] = {
                "skipped": True,
                "reason": "Miesięczny limit budżetu AI przekroczony — walidacja AI pominięta.",
            }
            return result_dict
    except Exception:
        pass

    if mode == "auto":
        if "findings" in result_dict and "error_count" in result_dict:
            mode = "typo"
        elif result_dict.get("items"):
            mode = "table"
        else:
            mode = "regex"

    try:
        if mode == "typo":
            ai = _validate_typo(result_dict)
        elif mode == "table":
            ai = _validate_table(result_dict)
        else:
            ai = _validate_regex(result_dict)
    except Exception as e:
        ai = {"error": str(e)[:200]}

    # AI może (wbrew promptowi) zwrócić listę/skalar zamiast obiektu JSON — wtedy
    # dalsze ai.get(...) rzucałoby AttributeError poza blokiem try. Wymuś dict.
    if not isinstance(ai, dict):
        ai = {"error": "Nieoczekiwany format odpowiedzi AI (oczekiwano obiektu JSON)"}

    RISK = {"ok": 0, "warning": 1, "error": 2, "critical": 3}

    if ai and "error" not in ai:
        # Zastąp "Dokument A/B" nazwami plików w polach tekstowych
        fa = result_dict.get("file_a") or ""
        fb = result_dict.get("file_b") or ""
        if fa or fb:
            def _sub_filenames(text: str) -> str:
                if not isinstance(text, str):
                    return text
                if fa:
                    text = re.sub(r'\bDokument\s+A\b', fa, text, flags=re.IGNORECASE)
                    text = re.sub(r'\bdoc(?:ument)?\s+A\b', fa, text, flags=re.IGNORECASE)
                if fb:
                    text = re.sub(r'\bDokument\s+B\b', fb, text, flags=re.IGNORECASE)
                    text = re.sub(r'\bdoc(?:ument)?\s+B\b', fb, text, flags=re.IGNORECASE)
                return text
            for _key in ("overall_summary", "report_pl", "recommendation_reason"):
                if _key in ai:
                    ai[_key] = _sub_filenames(ai[_key])
            for _lst in ("critical_errors", "warnings", "ok_items"):
                for _item in (ai.get(_lst) or []):
                    if not isinstance(_item, dict):
                        continue
                    for _k in ("impact", "action", "comment", "ai_comment", "field"):
                        if _k in _item:
                            _item[_k] = _sub_filenames(_item[_k])

        result_dict["ai_validation"] = ai
        cur_r = result_dict.get("risk_level", "ok")
        ai_r  = ai.get("overall_risk", "ok")
        if ai_r not in RISK:
            # Invalid/missing AI risk: keep the existing computed risk level rather
            # than forcing "ok", which would downgrade a genuinely risky comparison.
            ai_r = cur_r if cur_r in RISK else "ok"
        if RISK.get(ai_r, 0) > RISK.get(cur_r, 0):
            result_dict["ai_adjusted_risk"] = ai_r
            result_dict["ai_risk_changed"]  = True
            result_dict["risk_level"]       = ai_r
        _VALID_RECS = frozenset({"APPROVED", "APPROVED_WITH_NOTES",
                                  "HOLD_PENDING_CLARIFICATION", "REJECT"})
        rec = ai.get("recommendation")
        if rec and rec in _VALID_RECS:
            result_dict["ai_recommendation"]        = rec
            result_dict["ai_recommendation_reason"] = str(ai.get("recommendation_reason", ""))[:500]
    else:
        err = (ai or {}).get("error", "Nieznany błąd")
        result_dict["ai_validation"] = {
            "error": err,
            "overall_risk": result_dict.get("risk_level", "unknown"),
            "overall_summary": f"Walidacja AI niedostępna: {err[:200]}",
            "critical_errors": [], "warnings": [], "validated_findings": [],
            "report_pl": "Walidacja AI nie powiodła się.",
            "recommendation": "HOLD_PENDING_CLARIFICATION",
            "recommendation_reason": f"Błąd AI: {err[:100]}",
            "checklist": {},
        }

    return result_dict
