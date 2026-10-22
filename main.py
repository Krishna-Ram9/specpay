"""SpecPay backend: milestone state machine on top of PayPal Orders v2 + webhooks."""
import logging
import os
import secrets
import time
from decimal import Decimal, InvalidOperation

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
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
RESET_KEY = os.getenv("RESET_KEY")  # demo reset is disabled unless this is set

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


MAX_AMOUNT = Decimal("10000.00")
AI_LIMIT, AI_WINDOW = 20, 600  # AI calls allowed per visitor per 10 minutes
_ai_hits: dict[str, list[float]] = {}


def _amount(value: str) -> str:
    """Validate a money amount and return it as a two-decimal string like '50.00'."""
    try:
        d = Decimal(str(value).strip()).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise HTTPException(400, "Amount must be a number like '50.00'")
    if d <= 0 or d > MAX_AMOUNT:
        raise HTTPException(400, f"Amount must be between 0.01 and {MAX_AMOUNT}")
    return f"{d:.2f}"


def _text(value: str | None, label: str, limit: int, required: bool = True) -> str | None:
    value = (value or "").strip()
    if required and not value:
        raise HTTPException(400, f"{label} is required")
    if len(value) > limit:
        raise HTTPException(400, f"{label} is too long (max {limit} characters)")
    return value or None


def _rate_limit(request: Request) -> None:
    """Keep one visitor from burning the free AI quota. The last X-Forwarded-For entry is added by Render's proxy."""
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[-1].strip()
    now = time.time()
    recent = [t for t in _ai_hits.get(ip, []) if now - t < AI_WINDOW]
    if len(recent) >= AI_LIMIT:
        raise HTTPException(429, "That is a lot of AI requests in a short time. Please wait a few minutes and try again.")
    recent.append(now)
    _ai_hits[ip] = recent
    if len(_ai_hits) > 1000:
        for k in [k for k, v in _ai_hits.items() if not any(now - t < AI_WINDOW for t in v)]:
            del _ai_hits[k]


def _ai_unavailable(exc: Exception) -> HTTPException:
    log.warning("AI request failed: %s", exc)
    return HTTPException(503, "The AI service is busy or unreachable right now. Please try again in a moment.")


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
    currency = body.currency.strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise HTTPException(400, "Currency must be a 3-letter code like USD")
    return db.create_milestone(
        _text(body.project, "Project", 200), _text(body.title, "Title", 200),
        _amount(body.amount), currency, _text(body.criteria, "Criteria", 2000, required=False),
    )


@app.get("/milestones")
async def list_milestones(project: str | None = None):
    last = db.latest_reviews()
    return [{**m, "last_review": last.get(m["id"])} for m in db.list_milestones(project)]


@app.get("/milestones/{mid}")
async def read_milestone(mid: str):
    m = get_or_404(mid)
    return {**m, "history": db.history(mid), "reviews": db.review_history(mid)}


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
async def ai_scope(body: ScopeRequest, request: Request):
    """Turn a client brief into milestones with amounts and acceptance criteria."""
    _rate_limit(request)
    try:
        if Decimal(body.total_budget) <= 0:
            raise ValueError
    except (InvalidOperation, ValueError):
        raise HTTPException(400, "total_budget must be a positive number like '200.00'")
    if Decimal(body.total_budget) > MAX_AMOUNT:
        raise HTTPException(400, f"total_budget can be at most {MAX_AMOUNT} in this demo")
    if not body.brief.strip():
        raise HTTPException(400, "Write a project brief first")
    if len(body.brief) > 5000:
        raise HTTPException(400, "The brief is too long (max 5000 characters)")
    try:
        plan = await ai.scope_brief(body.brief, body.total_budget, body.currency)
    except ai.AIError as exc:
        raise _ai_unavailable(exc)
    saved = []
    if body.save:
        project = body.project or plan["project_title"]
        for item in plan["milestones"]:
            saved.append(db.create_milestone(project, item["title"], item["amount"], body.currency, item["criteria"]))
    return {"plan": plan, "milestones": saved}


@app.post("/milestones/{mid}/review")
async def ai_review(mid: str, body: ReviewRequest, request: Request):
    """AI checks the deliverable against the criteria. Pass -> approved; fail -> back to funded."""
    m = get_or_404(mid)
    require_status(m, "funded", "submitted")
    _rate_limit(request)
    if not body.deliverable.strip():
        raise HTTPException(400, "Describe the delivered work first")
    if len(body.deliverable) > 20000:
        raise HTTPException(400, "The description is too long (max 20000 characters)")
    if not m["criteria"]:
        raise HTTPException(400, "This milestone has no acceptance criteria to review against")
    try:
        result = await ai.review_deliverable(m["title"], m["criteria"], body.deliverable)
    except ai.AIError as exc:
        raise _ai_unavailable(exc)
    db.add_review(mid, result)  # permanent audit record of the decision and its evidence

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


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _find_index() -> str | None:
    """index.html normally lives in static/, but accept the repo root too."""
    for path in (os.path.join(BASE_DIR, "static", "index.html"), os.path.join(BASE_DIR, "index.html")):
        if os.path.isfile(path):
            return path
    return None


@app.get("/", include_in_schema=False)
async def home():
    """The single-page demo UI."""
    path = _find_index()
    if not path:
        raise HTTPException(404, "index.html was not found on the server (expected static/index.html)")
    return FileResponse(path)


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>SpecPay</title>
<style>body{font:16px/1.5 system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;
background:#0e1116;color:#e8ebf0}main{max-width:420px;padding:24px;text-align:center}
a{display:inline-block;margin-top:14px;background:#5b9bff;color:#fff;padding:10px 18px;border-radius:8px;
text-decoration:none}</style></head><body><main><h1>@@T@@</h1><p>@@B@@</p>
<a href="/">Back to SpecPay</a></main></body></html>"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(_PAGE.replace("@@T@@", title).replace("@@B@@", body))


@app.get("/return", include_in_schema=False)
async def return_page():
    return _page("Payment approved",
                 "PayPal has your approval. Go back to SpecPay and click "
                 "&quot;I approved: hold the funds&quot; to place the hold.")


@app.get("/cancel", include_in_schema=False)
async def cancel_page():
    return _page("Payment cancelled", "Nothing was charged. You can fund the milestone again whenever you are ready.")


@app.post("/demo/reset", include_in_schema=False)
async def demo_reset(request: Request):
    """Demo helper: void open PayPal holds, then wipe all milestones. Needs the RESET_KEY header."""
    sent = request.headers.get("x-reset-key", "")
    if not RESET_KEY or not secrets.compare_digest(sent, RESET_KEY):
        raise HTTPException(403, "Reset is disabled or the key is wrong")
    voided = 0
    for m in db.list_milestones():
        if m["auth_id"] and m["status"] in ("funded", "submitted", "approved"):
            try:
                await paypal("POST", f"/v2/payments/authorizations/{m['auth_id']}/void")
                voided += 1
            except HTTPException as exc:
                log.warning("Could not void hold for %s during reset: %s", m["id"], exc.detail)
    db.reset()
    return {"reset": True, "holds_voided": voided}


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
