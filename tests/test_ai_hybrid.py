"""
tests/test_ai_hybrid.py — decyzja hybrydowa Haiku/Sonnet + audyt (_ai_decision).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import ai_validator as av


def test_no_escalation_low_score(monkeypatch):
    monkeypatch.setattr(av, "_haiku_prescreen",
                        lambda p: {"risk_score": 10, "needs_deep_analysis": False, "reason": "ok"})
    monkeypatch.setattr(av, "_call_claude", lambda p: {"overall_risk": "ok"})
    monkeypatch.setattr(av, "_call_claude_pro", lambda p: {"overall_risk": "SHOULD_NOT_RUN"})
    r = av._run_hybrid("prompt")
    assert r["_model_used"] == "haiku"
    assert r["_ai_decision"]["escalated"] is False
    assert r["_ai_decision"]["screen_score"] == 10
    assert r["overall_risk"] == "ok"


def test_escalation_mid_score_sonnet(monkeypatch):
    # score między HYBRID_THRESHOLD a OPUS_THRESHOLD → Sonnet
    monkeypatch.setattr(av, "_haiku_prescreen",
                        lambda p: {"risk_score": 40, "needs_deep_analysis": True, "reason": "bad"})
    monkeypatch.setattr(av, "_call_claude", lambda p: {"x": 1})
    monkeypatch.setattr(av, "_call_claude_pro", lambda p, model=None: {"overall_risk": "warning"})
    r = av._run_hybrid("prompt")
    assert r["_model_used"] == "sonnet"
    assert r["_ai_decision"]["escalated"] is True
    assert r["_ai_decision"]["screen_reason"] == "bad"
    assert r["_ai_decision"]["threshold"] == av.HYBRID_THRESHOLD


def test_escalation_high_score_opus(monkeypatch):
    # score >= OPUS_THRESHOLD → Opus
    monkeypatch.setattr(av, "_haiku_prescreen",
                        lambda p: {"risk_score": 80, "needs_deep_analysis": True, "reason": "bad"})
    monkeypatch.setattr(av, "_call_claude", lambda p: {"x": 1})
    monkeypatch.setattr(av, "_call_claude_pro",
                        lambda p, model=None: {"overall_risk": "critical", "_m": model})
    r = av._run_hybrid("prompt")
    assert r["_model_used"] == "opus"
    assert r["_ai_decision"]["escalated"] is True
    assert r["overall_risk"] == "critical"
