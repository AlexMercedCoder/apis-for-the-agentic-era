"""A local stand-in for a card payment provider's test mode.

Not a real provider and not a client for one. It follows the published
shape of a developer payment API (form-encoded requests, payment methods,
payment intents, refunds, idempotency keys, signed webhooks) and the
Agentic Commerce Protocol's delegate_payment endpoint (2026-04-17,
pinned in acp/openapi.delegate_payment.yaml), so the chapter's listings
run with no account. Every key below is a placeholder.

Runs on 127.0.0.1:8091. Ledger lines (charges, refunds, refusals) go to
stderr, which run.sh saves as captures/provider.log.
"""

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import secrets
import sys
import time
from urllib.parse import parse_qsl

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import webhooks

# Placeholder keys, labeled as such. A real secret key never sits in code.
MERCHANT_SECRET_KEY = "sk_test_not_a_real_key_harbor"
MERCHANT_PUBLISHABLE_KEY = "pk_test_not_a_real_key_harbor"
PLATFORM_SECRET_KEY = "sk_test_not_a_real_key_agent_platform"
OTHER_SHOP_KEY = "sk_test_not_a_real_key_other_shop"
ACCOUNTS = {MERCHANT_SECRET_KEY: "harbor-books",
            OTHER_SHOP_KEY: "other-shop"}

# The provider's published test cards: one that pays, one that declines.
TEST_CARDS = {"4242424242424242": "succeeds", "4000000000000002": "declines"}

WEBHOOK_URL = "http://127.0.0.1:8009/hooks/payments"
SLOW_FIRST = 0          # set by --slow-first
IDEMPOTENCY_TTL = 24 * 3600

payment_methods: dict[str, dict] = {}
payment_intents: dict[str, dict] = {}
vault_tokens: dict[str, dict] = {}
refunded: dict[str, int] = {}
idempotency: dict[tuple, dict] = {}
created_count = 0


def log(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def error(status: int, type_: str, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"type": type_, "code": code,
                                   "message": message}}, status_code=status)


async def read_form(request: Request) -> dict:
    """Parse an application/x-www-form-urlencoded body."""
    return dict(parse_qsl((await request.body()).decode()))


def bearer(request: Request) -> str:
    return request.headers.get("authorization", "").removeprefix("Bearer ")


async def send_event(event_type: str, obj: dict) -> None:
    """Deliver one event, signed the way Chapter 6's webhooks.py signs."""
    body = json.dumps({"id": new_id("evt"), "type": event_type,
                       "created": int(time.time()),
                       "data": {"object": obj}}).encode()
    msg_id = new_id("msg")
    ts = str(int(time.time()))
    headers = {"content-type": "application/json", "webhook-id": msg_id,
               "webhook-timestamp": ts,
               "webhook-signature": webhooks.sign(webhooks.SECRET, msg_id,
                                                  ts, body)}
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            r = await client.post(WEBHOOK_URL, content=body, headers=headers)
            log(f"webhook {event_type} {obj['id']} -> {r.status_code}")
        except httpx.HTTPError as exc:
            log(f"webhook {event_type} {obj['id']} failed: {exc!r}")


async def idempotent(request: Request, account: str, handler):
    """Replay a saved response for a repeated Idempotency-Key.

    Keys are scoped to the account. The same key with different
    parameters is refused, the way payment APIs and the IETF draft do.
    """
    form = await read_form(request)
    key = request.headers.get("idempotency-key")
    if key is None:
        return await handler(form)
    if len(key) > 255:
        return error(400, "invalid_request_error", "idempotency_key_invalid",
                     "Idempotency-Key is longer than 255 characters.")
    fingerprint = hashlib.sha256(
        (request.url.path + json.dumps(form, sort_keys=True)).encode()
    ).hexdigest()
    slot = (account, key)
    saved = idempotency.get(slot)
    if saved and time.time() - saved["at"] < IDEMPOTENCY_TTL:
        if saved["fingerprint"] != fingerprint:
            log(f"refused {request.url.path}: key reused with other "
                "parameters")
            return error(400, "idempotency_error", "idempotency_key_reused",
                         "This key was used with different parameters.")
        if saved["status"] is None:
            log(f"conflict {request.url.path}: first request still running")
            return error(409, "idempotency_error", "idempotency_key_in_use",
                         "A request with this key is still in progress.")
        log(f"replayed {request.url.path} key={key[:8]}...")
        return JSONResponse(saved["body"], status_code=saved["status"],
                            headers={"Idempotent-Replayed": "true"})
    idempotency[slot] = {"fingerprint": fingerprint, "at": time.time(),
                         "status": None, "body": None}
    response = await handler(form)
    idempotency[slot].update(status=response.status_code,
                             body=json.loads(response.body))
    return response


async def create_payment_method(request: Request) -> JSONResponse:
    """Tokenize a card. Only the browser's provider field calls this."""
    if bearer(request) != MERCHANT_PUBLISHABLE_KEY:
        return error(401, "invalid_request_error", "invalid_api_key",
                     "Use the publishable key.")
    form = await read_form(request)
    number = str(form.get("card[number]", ""))
    if number not in TEST_CARDS:
        return error(402, "card_error", "invalid_number",
                     "Test mode accepts only the provider's test cards.")
    pm = {"id": new_id("pm"), "object": "payment_method", "type": "card",
          "card": {"brand": "visa", "last4": number[-4:],
                   "exp_month": int(form.get("card[exp_month]", 12)),
                   "exp_year": int(form.get("card[exp_year]", 2030))}}
    payment_methods[pm["id"]] = {**pm, "_number": number}
    log(f"tokenized card ****{number[-4:]} as {pm['id']}")
    return JSONResponse(pm)


def check_allowance(token: str, account: str, amount: int, currency: str,
                    session_id: str | None) -> str | None:
    """Return a refusal reason, or None if the vault token may pay."""
    vt = vault_tokens.get(token)
    if vt is None:
        return "unknown vault token"
    allowance = vt["allowance"]
    if vt["used"]:
        return "allowance already used (one_time)"
    if allowance["merchant_id"] != account:
        return f"allowance is for {allowance['merchant_id']}, not {account}"
    if session_id != allowance["checkout_session_id"]:
        return "allowance is for another checkout session"
    if currency != allowance["currency"]:
        return "currency differs from the allowance"
    if amount > allowance["max_amount"]:
        return f"{amount} exceeds max_amount {allowance['max_amount']}"
    if dt.datetime.now(dt.UTC) > dt.datetime.fromisoformat(
            allowance["expires_at"]):
        return "allowance expired"
    return None


async def create_payment_intent(request: Request):
    global created_count
    account = ACCOUNTS.get(bearer(request))
    if account is None:
        return error(401, "invalid_request_error", "invalid_api_key",
                     "Invalid API key.")

    async def handler(form: dict) -> JSONResponse:
        global created_count
        amount, currency = int(form["amount"]), form["currency"]
        method = form.get("payment_method", "")
        session_id = form.get("metadata[checkout_session_id]")
        pi = {"id": new_id("pi"), "object": "payment_intent",
              "amount": amount, "currency": currency,
              "payment_method": method, "status": "requires_payment_method",
              "metadata": {k[9:-1]: v for k, v in form.items()
                           if k.startswith("metadata[")}}
        if method.startswith("vt_"):
            reason = check_allowance(method, account, amount, currency,
                                     session_id)
            if reason:
                log(f"refused {method}: {reason}")
                return error(402, "card_error", "allowance_refused", reason)
            vault_tokens[method]["used"] = True
            outcome = "succeeds"
        elif method in payment_methods:
            outcome = TEST_CARDS[payment_methods[method]["_number"]]
        else:
            return error(400, "invalid_request_error", "resource_missing",
                         "No such payment method.")
        payment_intents[pi["id"]] = pi
        if outcome == "declines":
            pi["last_payment_error"] = {"code": "card_declined"}
            log(f"declined {pi['id']} {amount} {currency}")
            return JSONResponse(pi, status_code=402)
        pi["status"] = "succeeded"
        created_count += 1
        log(f"charge {pi['id']} {amount} {currency} "
            f"order={pi['metadata'].get('order_id', '-')}")
        delay = SLOW_FIRST > 0 and created_count <= SLOW_FIRST
        if delay:
            await asyncio.sleep(1.5)
        return JSONResponse(pi, background=BackgroundTask(
            send_event, "payment_intent.succeeded", dict(pi)))

    return await idempotent(request, account, handler)


async def create_refund(request: Request):
    account = ACCOUNTS.get(bearer(request))
    if account is None:
        return error(401, "invalid_request_error", "invalid_api_key",
                     "Invalid API key.")

    async def handler(form: dict) -> JSONResponse:
        pi = payment_intents.get(form.get("payment_intent", ""))
        if pi is None or pi["status"] != "succeeded":
            return error(400, "invalid_request_error", "resource_missing",
                         "No succeeded payment intent with that ID.")
        amount = int(form.get("amount", pi["amount"]))
        if refunded.get(pi["id"], 0) + amount > pi["amount"]:
            log(f"refused refund {amount} on {pi['id']}: over the charge")
            return error(400, "invalid_request_error",
                         "charge_already_refunded",
                         "Refunds would exceed the amount charged.")
        refunded[pi["id"]] = refunded.get(pi["id"], 0) + amount
        refund = {"id": new_id("re"), "object": "refund", "amount": amount,
                  "payment_intent": pi["id"], "status": "succeeded"}
        log(f"refund {refund['id']} {amount} on {pi['id']}")
        return JSONResponse(refund, background=BackgroundTask(
            send_event, "charge.refunded", dict(refund)))

    return await idempotent(request, account, handler)


async def delegate_payment(request: Request) -> JSONResponse:
    """ACP delegate_payment: vault a card behind a one-time allowance."""
    if bearer(request) != PLATFORM_SECRET_KEY:
        return JSONResponse({"type": "invalid_request", "code": "unauthorized",
                             "message": "Invalid API key."}, status_code=401)
    if not request.headers.get("idempotency-key"):
        return JSONResponse({"type": "invalid_request",
                             "code": "idempotency_key_required",
                             "message": "Idempotency-Key is required."},
                            status_code=400)
    body = await request.json()
    card, allowance = body["payment_method"], body["allowance"]
    if card["number"] not in TEST_CARDS:
        return JSONResponse({"type": "invalid_request", "code": "invalid_card",
                             "message": "Not a test card.",
                             "param": "payment_method.number"},
                            status_code=400)
    token = new_id("vt")
    vault_tokens[token] = {"allowance": allowance, "used": False}
    log(f"vaulted card ****{card['number'][-4:]} as {token}: "
        f"max {allowance['max_amount']} {allowance['currency']} for "
        f"{allowance['merchant_id']} {allowance['checkout_session_id']}")
    return JSONResponse({"id": token,
                         "created": dt.datetime.now(dt.UTC).isoformat(
                             timespec="seconds").replace("+00:00", "Z"),
                         "metadata": {**body["metadata"],
                                      "source": "harbor-test-psp"}},
                        status_code=201)


app = Starlette(routes=[
    Route("/v1/payment_methods", create_payment_method, methods=["POST"]),
    Route("/v1/payment_intents", create_payment_intent, methods=["POST"]),
    Route("/v1/refunds", create_refund, methods=["POST"]),
    Route("/agentic_commerce/delegate_payment", delegate_payment,
          methods=["POST"]),
])

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--slow-first", type=int, default=0,
                        help="hold the first N charge responses 1.5 s")
    args = parser.parse_args()
    SLOW_FIRST = args.slow_first
    uvicorn.run(app, host="127.0.0.1", port=8091, log_level="warning")
