"""
api_usage_tracker.py — rejestruje użycie i koszty wywołań Claude API.

Użycie w kodzie po każdym wywołaniu API:
    from api_usage_tracker import record_usage
    record_usage(model, data["usage"], call_type="artwork_ai")
"""

# Ceny za 1 milion tokenów (USD) — aktualne dla modeli Claude 4.x
_PRICING = {
    "claude-haiku-4-5-20251001": {"input": 1.00,  "output": 5.00},
    "claude-haiku-4-5":          {"input": 1.00,  "output": 5.00},
    "claude-sonnet-4-6":         {"input": 3.00,  "output": 15.00},
    "claude-sonnet-4-20250514":  {"input": 3.00,  "output": 15.00},
    "claude-opus-4-8":           {"input": 5.00,  "output": 25.00},
    "claude-opus-4-7":           {"input": 5.00,  "output": 25.00},
    "claude-opus-4-5":           {"input": 5.00,  "output": 25.00},
}
_DEFAULT_PRICE = {"input": 3.00, "output": 15.00}


def _cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    p = _PRICING.get(model, _DEFAULT_PRICE)
    return (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000


# ── Budżet AI (miesięczny limit + status) ─────────────────────────────────────

def _month_start_str(db=None) -> str:
    """Początek bieżącego miesiąca. Gdy podano połączenie — liczony zegarem BAZY
    (datetime('now') → SQLite UTC / PG NOW()), żeby był w tej samej strefie co
    created_at; inaczej granica miesiąca rozjeżdżała się z danymi przy przełomie."""
    if db is not None:
        try:
            row = db.execute("SELECT datetime('now')").fetchone()
            now = str(row[0])              # 'YYYY-MM-DD HH:MM:SS'
            if len(now) >= 7 and now[4] == "-":
                return now[:7] + "-01 00:00:00"
        except Exception:
            pass
    import datetime as _dt
    # Fallback spójny ze sposobem zapisu created_at (zegar UTC, jak SQLite
    # datetime('now')); używamy świadomego strefy UTC zamiast utcnow().
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-01 00:00:00")


def month_cost_usd() -> float:
    """Suma kosztów AI w bieżącym miesiącu kalendarzowym (zegar bazy)."""
    try:
        from db import get_db
        db = get_db()
        try:
            row = db.execute(
                "SELECT COALESCE(SUM(cost_usd),0) FROM api_usage WHERE created_at >= ?",
                (_month_start_str(db),)
            ).fetchone()
            return float(row[0] or 0.0)
        finally:
            db.close()
    except Exception:
        return 0.0


def get_budget_limit() -> float:
    """Miesięczny limit USD z settings (0 = brak limitu)."""
    try:
        from db import get_db
        db = get_db()
        try:
            row = db.execute(
                "SELECT value FROM settings WHERE category='ai' AND key='budget_monthly_usd'"
            ).fetchone()
            return float(row[0]) if row and row[0] else 0.0
        finally:
            db.close()
    except Exception:
        return 0.0


def set_budget_limit(usd: float) -> None:
    from db import get_db
    db = get_db()
    try:
        db.execute(
            "INSERT INTO settings(category,key,value) VALUES('ai','budget_monthly_usd',?) "
            "ON CONFLICT(category,key) DO UPDATE SET value=excluded.value",
            (str(float(usd)),)
        )
        db.commit()
    finally:
        db.close()


def get_budget_status() -> dict:
    """Status budżetu: ile wydano, limit, %, czy blisko (≥80%) / przekroczony."""
    limit = get_budget_limit()
    # Wyznaczamy near/over/pct z TEJ SAMEJ zaokrąglonej wartości, którą pokazujemy,
    # żeby próg %/przekroczenia nie rozjeżdżał się z prezentowaną kwotą.
    spent = round(month_cost_usd(), 4)
    pct = round((spent / limit * 100.0) if limit > 0 else 0.0, 1)
    return {
        "spent_usd": spent,
        "limit_usd": round(limit, 2),
        "pct": pct,
        "near": bool(limit > 0 and pct >= 80.0),
        "over": bool(limit > 0 and spent >= limit),
        "enabled": bool(limit > 0),
    }


def budget_allows_optional_ai() -> bool:
    """False, gdy miesięczny limit jest przekroczony — wtedy pomijamy nieobowiązkowe
    wywołania AI (walidacja regułowa zostaje)."""
    try:
        return not get_budget_status()["over"]
    except Exception:
        return True


def record_usage(model: str, usage: dict, call_type: str = "unknown", user_id=None):
    """
    Zapisuje jeden wpis do tabeli api_usage.
    usage — słownik z polami input_tokens i output_tokens zwrócony przez API Anthropic.
    Bezpieczne do wywołania z dowolnego wątku; błędy są ciche.
    """
    try:
        inp = int(usage.get("input_tokens") or 0)
        out = int(usage.get("output_tokens") or 0)
        cost = _cost_usd(model, inp, out)
        from db import get_db
        db = get_db()
        try:
            db.execute(
                """INSERT INTO api_usage(model, input_tokens, output_tokens, cost_usd, call_type, user_id)
                   VALUES(?,?,?,?,?,?)""",
                (model, inp, out, cost, call_type, user_id)
            )
            db.commit()
        finally:
            db.close()
    except Exception:
        pass
