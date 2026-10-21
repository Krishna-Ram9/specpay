"""SpecPay backend: milestone state machine on top of PayPal Orders v2 + webhooks."""
import logging
import os
import time
from decimal import Decimal, InvalidOperation

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

load_dotenv()
import ai  # noqa: E402
import db  # imported after load_dotenv so SPECPAY_DB from .env is honoured  # noqa: E402

log = logging.getLogger("specpay")
logging.basicConfig(level=logging.INFO)

PAYPAL_BASE = os.getenv("PAYPAL_BASE", "https://api-m.sandbox.paypal.com")
CLIENT_ID = os.environ["PAYPAL_CLIENT_ID"]
SECRET = os.environ["PAYPAL_SECRET"]
WEBHOOK_ID = os.getenv("PAYPAL_WEBHOOK_ID")  # set after you create the webhook
RETURN_URL = os.getenv("RETURN_URL", "http://localhost:8000/return")
CANCEL_URL = os.getenv("CANCEL_URL", "http://localhost:8000/cancel")

app = FastAPI(title="SpecPay")
db.init_db()
_token = {"value": None, "expires_at": 0.0}


# ---------------------------------------------------------------- PayPal helper
async def paypal(method: str, path: str, json: dict | None = None) -> dict:
    """Call PayPal with a cached OAuth token. Raises HTTPException on PayPal errors."""
    async with httpx.AsyncClient(timeout=30) as client:
        if not _token["value"] or time.time() > _token["expires_at"] - 60:
            r = await client.post(
                f"{PAYPAL_BASE}/v1/oauth2/token",
                auth=(CLIENT_ID, SECRET),
                data={"grant_type": "client_credentials"},
            )
            if r.status_code != 200:
                raise HTTPException(502, f"PayPal auth failed: {r.text}")
            body = r.json()
            _token["value"] = body["access_token"]
            _token["expires_at"] = time.time() + body["expires_in"]

        r = await client.request(
            method,
            f"{PAYPAL_BASE}{path}",
            json=json if json is not None else ({} if method == "POST" else None),
            headers={"Authorization": f"Bearer {_token['value']}"},
        )
        if r.status_code >= 400:
            raise HTTPException(r.status_code, r.json() if r.content else r.text)
        return r.json() if r.content else {}


# ---------------------------------------------------------------- helpers
def get_or_404(mid: str) -> dict:
    m = db.get(mid)
    if not m:
        raise HTTPException(404, "Milestone not found")
    return m


def require_status(m: dict, *allowed: str) -> None:
    if m["status"] not in allowed:
        raise HTTPException(
            409, f"Milestone is '{m['status']}'; this action needs one of: {', '.join(allowed)}"
        )


# ---------------------------------------------------------------- models
class MilestoneCreate(BaseModel):
    project: str
    title: str
    amount: str                 # e.g. "50.00"
    currency: str = "USD"
    criteria: str | None = None  # acceptance criteria (used by the AI review later)


class Note(BaseModel):
    note: str = ""


# ---------------------------------------------------------------- milestone endpoints
@app.post("/milestones")
async def create_milestone(body: MilestoneCreate):
    return db.create_milestone(body.project, body.title, body.amount, body.currency, body.criteria)


@app.get("/milestones")
async def list_milestones(project: str | None = None):
    return db.list_milestones(project)


@app.get("/milestones/{mid}")
async def read_milestone(mid: str):
    m = get_or_404(mid)
    return {**m, "history": db.history(mid)}


@app.post("/milestones/{mid}/fund")
async def fund(mid: str):
    """Create an AUTHORIZE order. Send the buyer to approve_url."""
    m = get_or_404(mid)
    require_status(m, "draft", "awaiting_payment")
    order = await paypal("POST", "/v2/checkout/orders", {
        "intent": "AUTHORIZE",
        "purchase_units": [{
            "reference_id": mid,
            "amount": {"currency_code": m["currency"], "value": m["amount"]},
        }],
        "payment_source": {"paypal": {"experience_context": {
            "return_url": RETURN_URL,
            "cancel_url": CANCEL_URL,
            "user_action": "PAY_NOW",
        }}},
    })
    approve_url = next(
        (l["href"] for l in order["links"] if l["rel"] in ("payer-action", "approve")), None
    )
    db.transition(mid, "awaiting_payment", note="PayPal order created", order_id=order["id"])
    return {"milestone": db.get(mid), "approve_url": approve_url}


@app.post("/milestones/{mid}/authorize")
async def authorize(mid: str):
    """Call after the buyer approves. Holds the funds."""
    m = get_or_404(mid)
    require_status(m, "awaiting_payment")
    res = await paypal("POST", f"/v2/checkout/orders/{m['order_id']}/authorize")
    auth = res["purchase_units"][0]["payments"]["authorizations"][0]
    return db.transition(mid, "funded", note="funds held", auth_id=auth["id"])


@app.post("/milestones/{mid}/submit")
async def submit(mid: str, body: Note = Note()):
    """Freelancer delivers the work (AI review hooks in here later)."""
    m = get_or_404(mid)
    require_status(m, "funded")
    return db.transition(mid, "submitted", note=body.note or "deliverable submitted")


@app.post("/milestones/{mid}/reject")
async def reject(mid: str, body: Note = Note()):
    """Review failed: back to 'funded' so the freelancer can resubmit."""
    m = get_or_404(mid)
    require_status(m, "submitted")
    return db.transition(mid, "funded", note=body.note or "review rejected")


@app.post("/milestones/{mid}/approve")
async def approve(mid: str, body: Note = Note()):
    m = get_or_404(mid)
    require_status(m, "submitted")
    return db.transition(mid, "approved", note=body.note or "review passed")


async def _release(mid: str) -> dict:
    """Capture the held funds. Only allowed once the milestone is approved."""
    m = get_or_404(mid)
    require_status(m, "approved")
    res = await paypal("POST", f"/v2/payments/authorizations/{m['auth_id']}/capture")
    return db.transition(mid, "released", note="payment captured", capture_id=res["id"])


@app.post("/milestones/{mid}/release")
async def release(mid: str):
    return await _release(mid)


@app.post("/milestones/{mid}/void")
async def void(mid: str, body: Note = Note()):
    """Cancel the milestone; releases any PayPal hold."""
    m = get_or_404(mid)
    require_status(m, "draft", "awaiting_payment", "funded", "submitted", "approved")
    if m["auth_id"]:
        await paypal("POST", f"/v2/payments/authorizations/{m['auth_id']}/void")
    return db.transition(mid, "voided", note=body.note or "voided")


# ---------------------------------------------------------------- AI endpoints
class ScopeRequest(BaseModel):
    brief: str                   # the client's project description
    total_budget: str            # e.g. "200.00"
    currency: str = "USD"
    project: str | None = None   # defaults to the AI's suggested project title
    save: bool = True            # create the proposed milestones as drafts


class ReviewRequest(BaseModel):
    deliverable: str             # text description / extract of the delivered work
    auto_release: bool = False   # capture the payment immediately if the review passes


@app.post("/ai/scope")
async def ai_scope(body: ScopeRequest):
    """Turn a client brief into milestones with amounts and acceptance criteria."""
    try:
        if Decimal(body.total_budget) <= 0:
            raise ValueError
    except (InvalidOperation, ValueError):
        raise HTTPException(400, "total_budget must be a positive number like '200.00'")
    try:
        plan = await ai.scope_brief(body.brief, body.total_budget, body.currency)
    except ai.AIError as exc:
        raise HTTPException(502, str(exc))
    saved = []
    if body.save:
        project = body.project or plan["project_title"]
        for item in plan["milestones"]:
            saved.append(db.create_milestone(project, item["title"], item["amount"], body.currency, item["criteria"]))
    return {"plan": plan, "milestones": saved}


@app.post("/milestones/{mid}/review")
async def ai_review(mid: str, body: ReviewRequest):
    """AI checks the deliverable against the criteria. Pass -> approved; fail -> back to funded."""
    m = get_or_404(mid)
    require_status(m, "funded", "submitted")
    if not m["criteria"]:
        raise HTTPException(400, "This milestone has no acceptance criteria to review against")
    try:
        result = await ai.review_deliverable(m["title"], m["criteria"], body.deliverable)
    except ai.AIError as exc:
        raise HTTPException(502, str(exc))

    if m["status"] == "funded":
        db.transition(mid, "submitted", note="deliverable submitted for AI review")
    if result["verdict"] == "pass":
        db.transition(mid, "approved", note=f"AI review passed: {result['summary'][:300]}")
        if body.auto_release:
            await _release(mid)
    else:
        reasons = "; ".join(result["missing"]) or result["summary"]
        db.transition(mid, "funded", note=f"AI review failed: {reasons[:300]}")
    return {"verdict": result["verdict"], "review": result, "milestone": db.get(mid)}


@app.get("/return")
async def return_page(token: str | None = None):
    return {"message": "Approved. You can close this tab.", "order_id": token}


@app.get("/cancel")
async def cancel_page():
    return {"message": "Payment cancelled."}


# ---------------------------------------------------------------- webhooks
def _related(resource: dict) -> dict:
    return (resource.get("supplementary_data") or {}).get("related_ids") or {}


@app.post("/webhooks/paypal")
async def paypal_webhook(request: Request):
    """Receive PayPal events and keep milestone state in sync."""
    event = await request.json()
    if WEBHOOK_ID:
        h = request.headers
        result = await paypal("POST", "/v1/notifications/verify-webhook-signature", {
            "auth_algo": h.get("paypal-auth-algo"),
            "cert_url": h.get("paypal-cert-url"),
            "transmission_id": h.get("paypal-transmission-id"),
            "transmission_sig": h.get("paypal-transmission-sig"),
            "transmission_time": h.get("paypal-transmission-time"),
            "webhook_id": WEBHOOK_ID,
            "webhook_event": event,
        })
        if result.get("verification_status") != "SUCCESS":
            raise HTTPException(400, "Invalid webhook signature")
    else:
        log.warning("PAYPAL_WEBHOOK_ID not set: webhook signature NOT verified")

    etype = event.get("event_type", "")
    if not db.mark_event(event.get("id", ""), etype):
        return {"received": True, "duplicate": True}  # PayPal retries; stay idempotent

    resource = event.get("resource", {})
    rel = _related(resource)
    log.info("PayPal event: %s", etype)
    try:
        if etype == "PAYMENT.AUTHORIZATION.CREATED":
            m = db.find_by(order_id=rel.get("order_id"))
            if m:
                db.transition(m["id"], "funded", note="webhook: authorization created", auth_id=resource["id"])
        elif etype == "PAYMENT.AUTHORIZATION.VOIDED":
            m = db.find_by(auth_id=resource.get("id"))
            if m:
                db.transition(m["id"], "voided", note="webhook: authorization voided")
        elif etype == "PAYMENT.CAPTURE.COMPLETED":
            m = db.find_by(order_id=rel.get("order_id"), auth_id=rel.get("authorization_id"))
            if m:
                db.transition(m["id"], "released", note="webhook: capture completed", capture_id=resource["id"])
        else:
            log.info("Unhandled event type: %s", etype)
    except ValueError as exc:
        log.warning("Ignoring webhook that doesn't fit the milestone state: %s", exc)
    return {"received": True}
