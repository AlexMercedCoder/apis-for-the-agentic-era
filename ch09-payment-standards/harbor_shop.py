"""Harbor's payment-facing service on 127.0.0.1:8009.

- POST /hooks/payments: the provider's signed webhooks. An order becomes
  paid here, never on the checkout call's response.
- GET /orders/{order_id}: the order's status, for the demo.
- POST /checkout_sessions, GET /checkout_sessions/{id},
  POST /checkout_sessions/{id}/complete: Harbor as an ACP seller
  (API version 2026-04-17, contract pinned in acp/).
- GET /books/{book_id}/excerpt: an excerpt sold per request under x402.

Every key is a placeholder. No customer authentication here; Chapter 8's
gate would sit in front of the order routes.
"""

import hashlib
import json
import secrets
import sys
import time

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

import webhooks
import x402_local
from provider import MERCHANT_SECRET_KEY

PROVIDER = "http://127.0.0.1:8091"
API_VERSION = "2026-04-17"
ACP_KEY = "harbor_acp_not_a_real_key"   # issued by Harbor to the platform
BOOKS = {b["id"]: b for b in json.load(open("../harbor-data/books.json"))}
EXCERPT = {"bk-1001": "Call me Ishmael. Some years ago, never mind how "
                      "long precisely, having little or no money in my "
                      "purse..."}
EXCERPT_PRICE = "10000"   # 0.01 USDC, in the token's 6-decimal units

orders = {"ord-5005": {"id": "ord-5005", "customer_id": "cus-001",
                       "total_cents": 3797, "status": "placed"}}
seen_webhooks: set[str] = set()
sessions: dict[str, dict] = {}
acp_keys: dict[str, tuple[str, int, dict]] = {}


def log(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


async def payment_webhook(request: Request) -> Response:
    body = await request.body()
    try:
        msg_id = webhooks.verify(webhooks.SECRET, request.headers, body)
    except webhooks.InvalidWebhook as exc:
        log(f"401 webhook rejected: {exc}")
        return JSONResponse({"error": "invalid_webhook"}, status_code=401)
    if msg_id in seen_webhooks:
        log(f"204 {msg_id} duplicate, already processed")
        return Response(status_code=204)
    event = json.loads(body)
    obj = event["data"]["object"]
    note = "recorded, no open order matches"
    if event["type"] == "payment_intent.succeeded":
        order = orders.get(obj["metadata"].get("order_id", ""))
        if order and order["status"] == "placed":
            if (obj["amount"], obj["currency"]) == (order["total_cents"],
                                                    "usd"):
                order["status"] = "paid"
                order["payment_intent"] = obj["id"]
                note = f"{order['id']} paid"
            else:
                note = "amount or currency mismatch, held for review"
        elif order:
            note = (f"{order['id']} already paid by "
                    f"{order.get('payment_intent', '?')}, flag for refund")
    elif event["type"] == "charge.refunded":
        note = "refund recorded"
    # Record the delivery only after the state change. A real service
    # writes both in one database transaction, so a crash in between
    # lets the provider's retry finish the work instead of skipping it.
    seen_webhooks.add(msg_id)
    log(f"204 {event['type']} {obj['id']} -> {note}")
    return Response(status_code=204)


async def get_order(request: Request) -> JSONResponse:
    order = orders.get(request.path_params["order_id"])
    if order is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(order)


# -- ACP seller ---------------------------------------------------------

def acp_error(status: int, type_: str, code: str, message: str):
    return JSONResponse({"type": type_, "code": code, "message": message},
                        status_code=status)


def acp_guard(request: Request, body: bytes):
    """Check the headers ACP 2026-04-17 requires; replay repeated keys."""
    if request.headers.get("authorization") != f"Bearer {ACP_KEY}":
        return acp_error(401, "invalid_request", "unauthorized",
                         "Invalid API key."), None
    if request.headers.get("api-version") != API_VERSION:
        return acp_error(400, "invalid_request", "unsupported_api_version",
                         f"This seller supports {API_VERSION}."), None
    key = request.headers.get("idempotency-key")
    if not key:
        return acp_error(400, "invalid_request", "idempotency_key_required",
                         "Idempotency-Key must be present on POST."), None
    digest = hashlib.sha256(request.url.path.encode() + body).hexdigest()
    saved = acp_keys.get(key)
    if saved:
        if saved[0] != digest:
            return acp_error(422, "invalid_request", "idempotency_conflict",
                             "Key reused with a different request."), None
        return JSONResponse(saved[2], status_code=saved[1],
                            headers={"Idempotent-Replayed": "true"}), None
    return None, (key, digest)


def remember(slot, response: JSONResponse) -> JSONResponse:
    acp_keys[slot[0]] = (slot[1], response.status_code,
                         json.loads(response.body))
    response.headers["Idempotency-Key"] = slot[0]
    return response


def session_view(s: dict) -> dict:
    line = s["line_items"][0]
    total = line["totals"][0]["amount"]
    view = {
        "id": s["id"], "status": s["status"], "currency": "usd",
        "line_items": s["line_items"],
        "totals": [
            {"type": "subtotal", "display_text": "Subtotal",
             "amount": total},
            {"type": "total", "display_text": "Total", "amount": total}],
        "fulfillment_options": [], "messages": [],
        "links": [{"type": "return_policy",
                   "url": "https://harbor.example/returns"}],
        "capabilities": {"payment": {"handlers": [{
            "id": "card",
            "name": "dev.acp.tokenized.card",
            "version": API_VERSION,
            "spec": "https://harbor.example/acp/card-handler",
            "requires_delegate_payment": True,
            "requires_pci_compliance": False,
            "psp": "harbor-test-psp",
            "config_schema": "https://harbor.example/acp/config.json",
            "instrument_schemas": ["https://harbor.example/acp/card.json"],
            "config": {"merchant_id": "harbor-books"}}]}},
    }
    if "order" in s:
        view["order"] = s["order"]
    return view


async def create_session(request: Request) -> JSONResponse:
    body = await request.body()
    refused, slot = acp_guard(request, body)
    if refused:
        return refused
    data = json.loads(body)
    book = BOOKS.get(data["line_items"][0]["id"])
    if book is None or book["in_stock"] < 1:
        return remember(slot, acp_error(
            400, "invalid_request", "out_of_stock", "Item unavailable."))
    sid = "cs_" + secrets.token_hex(6)
    sessions[sid] = {"id": sid, "status": "ready_for_payment",
                     "buyer": data.get("buyer"),
                     "line_items": [{
                         "id": "li_1", "item": {"id": book["id"]},
                         "quantity": 1, "name": book["title"],
                         "unit_amount": book["price_cents"],
                         "totals": [{"type": "total",
                                     "display_text": "Total",
                                     "amount": book["price_cents"]}]}]}
    log(f"201 checkout session {sid} {book['id']} "
        f"{book['price_cents']} usd")
    return remember(slot, JSONResponse(session_view(sessions[sid]),
                                       status_code=201))


async def get_session(request: Request) -> JSONResponse:
    s = sessions.get(request.path_params["sid"])
    if s is None:
        return acp_error(404, "invalid_request", "not_found", "No session.")
    return JSONResponse(session_view(s))


async def complete_session(request: Request) -> JSONResponse:
    body = await request.body()
    refused, slot = acp_guard(request, body)
    if refused:
        return refused
    s = sessions.get(request.path_params["sid"])
    if s is None or s["status"] != "ready_for_payment":
        return remember(slot, acp_error(
            409, "invalid_request", "session_not_payable",
            "Session is not ready for payment."))
    token = json.loads(body)["payment_data"]["instrument"]["credential"][
        "token"]
    amount = s["line_items"][0]["totals"][0]["amount"]
    order_id = "ord-" + str(5005 + len(orders))
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.post(
            f"{PROVIDER}/v1/payment_intents",
            headers={"Authorization": f"Bearer {MERCHANT_SECRET_KEY}",
                     "Idempotency-Key": f"acp-complete-{s['id']}"},
            data={"amount": amount, "currency": "usd",
                  "payment_method": token, "confirm": "true",
                  "metadata[order_id]": order_id,
                  "metadata[checkout_session_id]": s["id"]})
    if r.status_code != 200:
        reason = r.json()["error"]["message"]
        log(f"402 complete {s['id']}: provider refused: {reason}")
        return remember(slot, acp_error(
            402, "invalid_request", "payment_declined", reason))
    orders[order_id] = {"id": order_id, "customer_id": "cus-001",
                        "total_cents": amount, "status": "placed"}
    s["status"] = "completed"
    s["order"] = {"id": order_id, "checkout_session_id": s["id"],
                  "permalink_url": f"https://harbor.example/orders/{order_id}",
                  "status": "confirmed"}
    log(f"200 complete {s['id']} -> {order_id} charged {amount} usd")
    return remember(slot, JSONResponse(session_view(s)))


# -- x402 seller --------------------------------------------------------

async def excerpt(request: Request) -> Response:
    book_id = request.path_params["book_id"]
    if book_id not in EXCERPT:
        return JSONResponse({"error": "not_found"}, status_code=404)
    required = x402_local.payment_required(
        str(request.url), EXCERPT_PRICE, "PAYMENT-SIGNATURE header is "
        "required")
    header = request.headers.get("payment-signature")
    if header is None:
        log(f"402 {request.url.path} no payment")
        return JSONResponse({}, status_code=402, headers={
            "PAYMENT-REQUIRED": x402_local.b64(required)})
    try:
        payload = x402_local.unb64(header)
        reason = x402_local.verify(payload, required)
    except (ValueError, KeyError, TypeError):
        reason = "invalid_payload"
    if reason:
        log(f"402 {request.url.path} {reason}")
        required["error"] = reason
        return JSONResponse({}, status_code=402, headers={
            "PAYMENT-REQUIRED": x402_local.b64(required)})
    settled = x402_local.settle(payload)
    log(f"200 {request.url.path} paid {EXCERPT_PRICE} by {settled['payer']}")
    return JSONResponse({"book_id": book_id, "excerpt": EXCERPT[book_id]},
                        headers={"PAYMENT-RESPONSE": x402_local.b64(settled)})


app = Starlette(routes=[
    Route("/hooks/payments", payment_webhook, methods=["POST"]),
    Route("/orders/{order_id}", get_order),
    Route("/checkout_sessions", create_session, methods=["POST"]),
    Route("/checkout_sessions/{sid}", get_session),
    Route("/checkout_sessions/{sid}/complete", complete_session,
          methods=["POST"]),
    Route("/books/{book_id}/excerpt", excerpt),
])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8009, log_level="warning")
