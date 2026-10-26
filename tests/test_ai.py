import asyncio
from decimal import Decimal

import pytest

import ai


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def no_sleep(monkeypatch):
    async def instant(_):
        return None
    monkeypatch.setattr(ai.asyncio, "sleep", instant)


def test_parse_json_accepts_fences_and_prose():
    assert ai.parse_json('Sure:\n```json\n{"a": 1}\n```\nDone.') == {"a": 1}


@pytest.mark.parametrize("text", ["no json here", '{"a": }', "[1, 2]"])
def test_parse_json_rejects_garbage(text):
    with pytest.raises(ai.AIError):
        ai.parse_json(text)


def test_scope_amounts_always_add_up_to_the_budget():
    data = {"project_title": "P", "milestones": [
        {"title": "a", "amount": "33.33", "criteria": "c"}, {"title": "b", "amount": "33.33", "criteria": "c"},
        {"title": "c", "amount": "1.00", "criteria": "c"}]}
    out = ai.normalize_scope(data, "100.00")
    assert sum(Decimal(m["amount"]) for m in out["milestones"]) == Decimal("100.00")
    assert out["milestones"][-1]["amount"] == "33.34"


@pytest.mark.parametrize("data", [
    {"milestones": []},
    {"milestones": [{"title": "a", "amount": "5", "criteria": ""}]},
    {"milestones": [{"title": "a", "amount": "60", "criteria": "c"}, {"title": "b", "amount": "60", "criteria": "c"}, {"title": "c", "amount": "1", "criteria": "c"}]},
    {"milestones": [{"title": "a", "amount": "x", "criteria": "c"}, {"title": "b", "amount": "1", "criteria": "c"}]},
])
def test_scope_rejects_unusable_plans(data):
    with pytest.raises(ai.AIError):
        ai.normalize_scope(data, "100.00")


def _checks(*met):
    return [{"criterion": f"c{i}", "met": m, "evidence": "e"} for i, m in enumerate(met)]


def test_review_passes_only_when_every_check_is_met():
    assert ai.normalize_review({"verdict": "pass", "checks": _checks(True, True)})["verdict"] == "pass"
    assert ai.normalize_review({"verdict": "pass", "checks": _checks(True, False)})["verdict"] == "fail"
    assert ai.normalize_review({"verdict": "pass", "checks": []})["verdict"] == "fail"
    assert ai.normalize_review({"verdict": "fail", "checks": _checks(True)})["verdict"] == "fail"
    assert ai.normalize_review({"verdict": "pass", "checks": [{"criterion": "c", "met": "true"}]})["verdict"] == "fail"


def test_retries_once_then_uses_the_fallback_provider(monkeypatch, no_sleep):
    calls = []

    async def fake(provider, system, user, max_tokens):
        calls.append(provider)
        if provider == "groq":
            raise ai.AIError("Groq API error 429")
        return {"ok": True}
    monkeypatch.setattr(ai, "_ask_once", fake)
    monkeypatch.setenv("AI_PROVIDER", "groq")
    monkeypatch.setenv("AI_FALLBACK_PROVIDER", "gemini")
    assert run(ai._ask("s", "u")) == {"ok": True}
    assert calls == ["groq", "groq", "gemini"]


def test_a_transient_failure_is_retried_once(monkeypatch, no_sleep):
    calls = []

    async def flaky(provider, system, user, max_tokens):
        calls.append(provider)
        if len(calls) == 1:
            raise ai.AIError("timeout")
        return {"ok": True}
    monkeypatch.setattr(ai, "_ask_once", flaky)
    monkeypatch.setenv("AI_PROVIDER", "groq")
    monkeypatch.delenv("AI_FALLBACK_PROVIDER", raising=False)
    assert run(ai._ask("s", "u")) == {"ok": True} and calls == ["groq", "groq"]


def test_configuration_errors_are_not_retried(monkeypatch, no_sleep):
    calls = []

    async def broken(provider, system, user, max_tokens):
        calls.append(provider)
        raise ai.AIError("GROQ_API_KEY is not set on the server")
    monkeypatch.setattr(ai, "_ask_once", broken)
    monkeypatch.setenv("AI_PROVIDER", "groq")
    monkeypatch.delenv("AI_FALLBACK_PROVIDER", raising=False)
    with pytest.raises(ai.AIError):
        run(ai._ask("s", "u"))
    assert calls == ["groq"]


def test_review_prompt_separates_trusted_facts_from_untrusted_excerpts(monkeypatch):
    seen = {}

    async def fake_ask(system, user, max_tokens=1500):
        seen["user"] = user
        return {"verdict": "pass", "summary": "", "checks": [{"criterion": "c", "met": True, "evidence": ""}], "missing": []}
    monkeypatch.setattr(ai, "_ask", fake_ask)
    run(ai.review_deliverable("T", "crit", "desc", "MEASURED FACTS", "EXCERPT TEXT"))
    assert "<measured_from_uploaded_files>\nMEASURED FACTS" in seen["user"]
    assert "<untrusted_file_contents>\nEXCERPT TEXT" in seen["user"]
    run(ai.review_deliverable("T", "crit", "desc"))
    assert "measured_from_uploaded_files" not in seen["user"] and "untrusted_file_contents" not in seen["user"]


def test_guard_rails_stay_in_the_prompts():
    assert "never run" in ai.REVIEW_SYSTEM and "SYNTAX ERROR" in ai.REVIEW_SYSTEM and "untrusted" in ai.REVIEW_SYSTEM
    assert "Never invent file names" in ai.SCOPE_SYSTEM and "trace back" in ai.SCOPE_SYSTEM
