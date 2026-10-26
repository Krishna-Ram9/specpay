import pytest
from fastapi.testclient import TestClient

import ai
import main


class FakePayPal:
    """Stands in for the PayPal sandbox: records every call and never touches the network."""

    def __init__(self):
        self.calls = []

    async def __call__(self, method, path, json=None):
        self.calls.append(path)
        if path == "/v2/checkout/orders":
            return {"id": "ORDER1", "links": [{"rel": "payer-action", "href": "https://paypal.test/approve"}]}
        if path.endswith("/authorize"):
            return {"purchase_units": [{"payments": {"authorizations": [{"id": "AUTH1"}]}}]}
        if path.endswith("/capture"):
            return {"id": "CAP1"}
        if path.endswith("/void"):
            return {}
        if path.endswith("/verify-webhook-signature"):
            return {"verification_status": "SUCCESS"}
        raise AssertionError(f"unexpected PayPal call {path}")

    def captured(self):
        return any(p.endswith("/capture") for p in self.calls)


@pytest.fixture
def api(fresh_db, monkeypatch):
    pp = FakePayPal()
    monkeypatch.setattr(main, "paypal", pp)
    monkeypatch.setattr(main, "WEBHOOK_ID", None)
    monkeypatch.setattr(main, "RESET_KEY", "test-reset-key")
    main._ai_hits.clear()
    return TestClient(main.app), pp


def create(client, amount="50", criteria="four holes"):
    return client.post("/milestones", json={"project": "P", "title": "T", "amount": amount, "criteria": criteria}).json()["id"]


def funded(client):
    mid = create(client)
    assert client.post(f"/milestones/{mid}/fund").status_code == 200
    assert client.post(f"/milestones/{mid}/authorize").status_code == 200
    return mid


def stub_review(monkeypatch, verdict="pass", seen=None):
    async def fake(title, criteria, deliverable, measured=None, excerpts=None):
        if seen is not None:
            seen.update(measured=measured, excerpts=excerpts)
        met = verdict == "pass"
        return {"verdict": verdict, "summary": "s", "checks": [{"criterion": "four holes", "met": met, "evidence": "e"}],
                "missing": [] if met else ["four holes"]}
    monkeypatch.setattr(main.ai, "review_deliverable", fake)


@pytest.mark.parametrize("body", [
    {"project": "P", "title": "T", "amount": "abc"}, {"project": "P", "title": "T", "amount": "-5"},
    {"project": "P", "title": "T", "amount": "99999"}, {"project": "P", "title": "  ", "amount": "5"},
    {"project": "", "title": "T", "amount": "5"}, {"project": "P", "title": "T", "amount": "5", "currency": "US1"},
])
def test_bad_milestones_are_rejected(api, body):
    assert api[0].post("/milestones", json=body).status_code == 400


def test_amounts_are_normalised(api):
    r = api[0].post("/milestones", json={"project": "P", "title": "T", "amount": "50"})
    assert r.status_code == 200 and r.json()["amount"] == "50.00"


def test_passing_review_releases_the_payment(api, monkeypatch):
    client, pp = api
    mid = funded(client)
    stub_review(monkeypatch, "pass")
    r = client.post(f"/milestones/{mid}/review", json={"deliverable": "all done", "auto_release": True})
    assert r.status_code == 200 and r.json()["milestone"]["status"] == "released"
    assert r.json()["milestone"]["capture_id"] == "CAP1" and pp.captured()
    body = client.get(f"/milestones/{mid}").json()
    assert [h["to_status"] for h in body["history"]] == ["draft", "awaiting_payment", "funded", "submitted", "approved", "released"]
    assert len(body["reviews"]) == 1
    assert client.get("/milestones").json()[0]["last_review"]["verdict"] == "pass"


def test_failing_review_keeps_the_money_held(api, monkeypatch):
    client, pp = api
    mid = funded(client)
    stub_review(monkeypatch, "fail")
    r = client.post(f"/milestones/{mid}/review", json={"deliverable": "partial", "auto_release": True})
    assert r.json()["verdict"] == "fail" and r.json()["milestone"]["status"] == "funded" and not pp.captured()


def test_payment_cannot_be_released_before_approval(api):
    client, pp = api
    mid = funded(client)
    assert client.post(f"/milestones/{mid}/release").status_code == 409 and not pp.captured()


def test_ai_outage_gives_a_friendly_message_without_leaking_details(api, monkeypatch):
    client, _ = api
    mid = funded(client)

    async def down(*a, **k):
        raise ai.AIError("Groq API error 429: secret-detail")
    monkeypatch.setattr(main.ai, "review_deliverable", down)
    r = client.post(f"/milestones/{mid}/review", json={"deliverable": "x"})
    assert r.status_code == 503 and "busy or unreachable" in r.json()["detail"] and "secret-detail" not in r.text


def test_ai_calls_are_rate_limited(api, monkeypatch):
    client, _ = api
    mid = funded(client)
    stub_review(monkeypatch, "fail")
    monkeypatch.setattr(main, "AI_LIMIT", 3)
    codes = [client.post(f"/milestones/{mid}/review", json={"deliverable": "x"}).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_review_receives_measured_facts_from_attached_files(api, monkeypatch, b64, sample_dxf):
    client, _ = api
    mid = funded(client)
    seen = {}
    stub_review(monkeypatch, "pass", seen)
    r = client.post(f"/milestones/{mid}/review", json={"deliverable": "see file", "files": [{"name": "flat.dxf", "data": b64(sample_dxf)}]})
    assert r.status_code == 200 and "4 circular hole(s)" in seen["measured"]
    assert client.get("/milestones").json()[0]["last_review"]["file_facts"][0]["kind"] == "dxf"


@pytest.mark.parametrize("payload", [
    {"deliverable": "x", "files": [{"name": "setup.exe", "data": "eA=="}]},
    {"deliverable": "x", "files": [{"name": "fake.dxf", "data": "bm90IGEgZHhm"}]},
    {"deliverable": "   "},
])
def test_bad_review_submissions_are_rejected(api, monkeypatch, payload):
    client, _ = api
    mid = funded(client)
    stub_review(monkeypatch)
    assert client.post(f"/milestones/{mid}/review", json=payload).status_code == 400


def test_scope_checks_inputs_and_saves_milestones(api, monkeypatch):
    client, _ = api
    assert client.post("/ai/scope", json={"brief": "x", "total_budget": "999999"}).status_code == 400
    assert client.post("/ai/scope", json={"brief": "  ", "total_budget": "100"}).status_code == 400
    assert client.post("/ai/scope", json={"brief": "x" * 5001, "total_budget": "100"}).status_code == 400

    async def plan(brief, budget, currency):
        return {"project_title": "Proj", "milestones": [{"title": "a", "amount": "60.00", "criteria": "c"}, {"title": "b", "amount": "40.00", "criteria": "c"}]}
    monkeypatch.setattr(main.ai, "scope_brief", plan)
    r = client.post("/ai/scope", json={"brief": "build it", "total_budget": "100"})
    assert r.status_code == 200 and [m["amount"] for m in r.json()["milestones"]] == ["60.00", "40.00"]


def test_reset_needs_the_key_and_releases_holds(api):
    client, pp = api
    funded(client)
    assert client.post("/demo/reset").status_code == 403
    assert client.post("/demo/reset", headers={"X-Reset-Key": "wrong"}).status_code == 403
    r = client.post("/demo/reset", headers={"X-Reset-Key": "test-reset-key"})
    assert r.status_code == 200 and r.json()["holds_voided"] == 1
    assert any(p.endswith("/void") for p in pp.calls) and client.get("/milestones").json() == []


def auth_created(event_id="EV1"):
    return {"id": event_id, "event_type": "PAYMENT.AUTHORIZATION.CREATED",
            "resource": {"id": "AUTH1", "supplementary_data": {"related_ids": {"order_id": "ORDER1"}}}}


def awaiting(client):
    mid = create(client)
    client.post(f"/milestones/{mid}/fund")
    return mid


def test_webhook_moves_the_milestone_and_duplicates_are_ignored(api):
    client, _ = api
    mid = awaiting(client)
    assert client.post("/webhooks/paypal", json=auth_created()).json() == {"received": True}
    m = client.get(f"/milestones/{mid}").json()
    assert m["status"] == "funded" and m["auth_id"] == "AUTH1"
    history_len = len(m["history"])
    assert client.post("/webhooks/paypal", json=auth_created()).json()["duplicate"] is True
    assert len(client.get(f"/milestones/{mid}").json()["history"]) == history_len


def test_failed_webhook_is_not_marked_processed_so_the_retry_works(api, monkeypatch):
    client, _ = api
    mid = awaiting(client)
    with monkeypatch.context() as mp:
        def boom(*a, **k):
            raise RuntimeError("database hiccup")
        mp.setattr(main.db, "transition", boom)
        assert client.post("/webhooks/paypal", json=auth_created("EV7")).status_code == 500
    assert not main.db.event_seen("EV7")
    assert client.post("/webhooks/paypal", json=auth_created("EV7")).status_code == 200
    assert main.db.event_seen("EV7") and client.get(f"/milestones/{mid}").json()["status"] == "funded"


def test_webhook_with_a_bad_signature_is_rejected(api, monkeypatch):
    client, pp = api
    mid = awaiting(client)
    monkeypatch.setattr(main, "WEBHOOK_ID", "WH1")

    async def refuse(method, path, json=None):
        return {"verification_status": "FAILURE"}
    monkeypatch.setattr(main, "paypal", refuse)
    assert client.post("/webhooks/paypal", json=auth_created()).status_code == 400
    assert client.get(f"/milestones/{mid}").json()["status"] == "awaiting_payment"


def test_out_of_order_webhook_is_ignored_not_retried(api):
    client, _ = api
    mid = awaiting(client)
    client.post(f"/milestones/{mid}/void", json={})
    assert client.post("/webhooks/paypal", json=auth_created("EV3")).status_code == 200
    assert client.get(f"/milestones/{mid}").json()["status"] == "voided"


def test_clicking_hold_before_approving_in_paypal_gives_a_clear_message(api, monkeypatch):
    client, _ = api
    mid = awaiting(client)

    async def not_approved(method, path, json=None):
        raise main.HTTPException(422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_NOT_APPROVED"}]})
    monkeypatch.setattr(main, "paypal", not_approved)
    r = client.post(f"/milestones/{mid}/authorize")
    assert r.status_code == 409 and "has not received your approval" in r.json()["detail"]
    assert "ORDER_NOT_APPROVED" not in r.text
    assert client.get(f"/milestones/{mid}").json()["status"] == "awaiting_payment"
