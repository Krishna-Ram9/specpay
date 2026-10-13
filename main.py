"""SpecPay backend starter: PayPal Orders v2 (authorize -> capture) + webhooks."""
import logging
import os
import time

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

load_dotenv()
log = logging.getLogger("specpay")
logging.basicConfig(level=logging.INFO)

PAYPAL_BASE = os.getenv("PAYPAL_BASE", "https://api-m.sandbox.paypal.com")
CLIENT_ID = os.environ["PAYPAL_CLIENT_ID"]
SECRET = os.environ["PAYPAL_SECRET"]
WEBHOOK_ID = os.getenv("PAYPAL_WEBHOOK_ID")  # set after you create the webhook
RETURN_URL = os.getenv("RETURN_URL", "http://localhost:8000/return")
CANCEL_URL = os.getenv("CANCEL_URL", "http://localhost:8000/cancel")

app = FastAPI(title="SpecPay")
_token = {"value": None, "expires_at": 0.0}


async def paypal(method: str, path: str, json: dict | None = None, headers: dict | None = None) -> dict:
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
            headers={"Authorization": f"Bearer {_token['value']}", **(headers or {})},
        )
        if r.status_code >= 400:
            raise HTTPException(r.status_code, r.json() if r.content else r.text)
        return r.json() if r.content else {}


class FundRequest(BaseModel):
    reference_id: str          # e.g. your milestone id
    amount: str                # e.g. "50.00"
    currency: str = "USD"


@app.post("/milestones/fund")
async def fund_milestone(req: FundRequest):
    """Create an AUTHORIZE order. Send the buyer to approve_url."""
    order = await paypal("POST", "/v2/checkout/orders", {
        "intent": "AUTHORIZE",
        "purchase_units": [{
            "reference_id": req.reference_id,
            "amount": {"currency_code": req.currency, "value": req.amount},
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
    return {"order_id": order["id"], "status": order["status"], "approve_url": approve_url}


@app.post("/orders/{order_id}/authorize")
async def authorize_order(order_id: str):
    """Call after the buyer approves. Holds the funds."""
    res = await paypal("POST", f"/v2/checkout/orders/{order_id}/authorize")
    auth = res["purchase_units"][0]["payments"]["authorizations"][0]
    return {"authorization_id": auth["id"], "status": auth["status"]}


@app.post("/authorizations/{auth_id}/capture")
async def capture(auth_id: str):
    """Release the held funds (call this when the AI review passes)."""
    res = await paypal("POST", f"/v2/payments/authorizations/{auth_id}/capture")
    return {"capture_id": res["id"], "status": res["status"]}


@app.post("/authorizations/{auth_id}/void")
async def void(auth_id: str):
    """Cancel the hold (milestone rejected or abandoned)."""
    await paypal("POST", f"/v2/payments/authorizations/{auth_id}/void")
    return {"status": "VOIDED"}


@app.get("/return")
async def return_page(token: str | None = None):
    return {"message": "Approved. You can close this tab.", "order_id": token}


@app.get("/cancel")
async def cancel_page():
    return {"message": "Payment cancelled."}


@app.post("/webhooks/paypal")
async def paypal_webhook(request: Request):
    """Receive PayPal events. Signature is verified once PAYPAL_WEBHOOK_ID is set."""
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

    log.info("PayPal event: %s", event.get("event_type"))
    # TODO: map event_type to milestone state changes
    # (e.g. PAYMENT.AUTHORIZATION.CREATED -> funded, PAYMENT.CAPTURE.COMPLETED -> released)
    return {"received": True}
