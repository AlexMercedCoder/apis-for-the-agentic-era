"""Harbor's refunds API: one REST resource, designed for agent callers.

POST /orders/{order_id}/refunds refunds one damaged copy of one book on a
delivered order. It is narrow on purpose: the token must carry
orders:refund and an RFC 9396 authorization_details entry naming this
order, this book, and an amount at least the line's price, which only
Rosa's approval on her phone produces; the damage must already be on
record with Harbor's fulfillment agent; each line refunds once; and the
Idempotency-Key (Chapter 3's format) makes a retried request return the
first result. Errors are Chapter 3's problem details with a hint.

The refund is pending until the payment provider's signed charge.refunded
webhook arrives (Chapter 9's rule), and then Harbor tells its support
agent's host with a signed refund.succeeded webhook (Chapter 6).

    uv run python refunds_api.py [--slow-first]   # 127.0.0.1:8123
"""

import argparse
import asyncio
import json
import logging
import re
import secrets
import sys
import time

import httpx
import jwt
import uvicorn
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response
from opentelemetry import trace
from opentelemetry.propagate import extract
from opentelemetry.trace import SpanKind
from pydantic import BaseModel, ConfigDict, Field

import common
import issuer
import webhooks_ch09 as webhooks

PROVIDER = "http://127.0.0.1:8091"
PROVIDER_KEY = "sk_test_not_a_real_key_harbor"       # placeholder
HOST_HOOK, HOST_SECRET = common.HOST_HOOK, common.HOST_SECRET
KEY = re.compile(r'"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}"')
PROBLEMS = "https://harbor.example/problems/"
SLOW_FIRST = False

log = logging.getLogger("refunds")
logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(message)s")
tracer = common.tracer("refunds-api")
app = FastAPI(title="Harbor refunds API", version="1.0.0")

payments: dict[str, str] = {}      # order_id -> payment intent, by webhook
refunds: dict[str, dict] = {}      # refund_id -> refund
by_line: dict[tuple, str] = {}     # (order_id, book_id) -> refund_id
keys: dict[tuple, dict] = {}       # (customer, key) -> saved response
seen_hooks: set[str] = set()


class RefundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    book_id: str = Field(pattern=r"^bk-[0-9]{4}$",
                         description="The damaged book, such as bk-1001.")
    damage_case: str = Field(
        pattern=r"^dmg-[0-9]{4}-[0-9]{4}$",
        description="The case ID Harbor's fulfillment agent returned "
                    "when the damage was reported.")


def problem(status, slug, title, detail, hint=None, **extra):
    body = {"type": PROBLEMS + slug, "title": title, "status": status,
            "detail": detail, **extra}
    if hint:
        body["hint"] = hint
    return JSONResponse(body, status_code=status,
                        media_type="application/problem+json")


def caller(authorization):
    if not (authorization or "").startswith("Bearer "):
        raise PermissionError
    claims = common.check_token(authorization[7:], issuer.REFUNDS_AUD)
    if "orders:refund" not in claims.get("scope", "").split():
        raise PermissionError
    return claims


@app.post("/orders/{order_id}/refunds", status_code=201,
          operation_id="create_refund",
          summary="Refund one damaged copy on a delivered order")
async def create_refund(order_id: str, body: RefundRequest,
                        request: Request,
                        authorization: str = Header(None),
                        idempotency_key: str = Header(None)):
    with tracer.start_as_current_span(
            "POST /orders/{order_id}/refunds", kind=SpanKind.SERVER,
            context=extract(request.headers),
            attributes={"http.request.method": "POST",
                        "http.route": "/orders/{order_id}/refunds"}) as span:
        try:
            claims = caller(authorization)
        except (PermissionError, jwt.PyJWTError):
            return problem(401, "unauthorized", "Unauthorized",
                           "A token for this API with orders:refund is "
                           "required.")
        customer = claims["customer_id"]
        who = "->".join([claims["sub"]] + common.actors(claims)[::-1])
        span.set_attribute("enduser.id", claims["sub"])
        if idempotency_key is None or not KEY.fullmatch(idempotency_key):
            return problem(400, "idempotency-key-invalid",
                           "Idempotency-Key required",
                           "Send a lowercase UUID in double quotes.")
        slot = (customer, idempotency_key)
        fingerprint = [order_id, body.model_dump()]
        saved = keys.get(slot)
        if saved:
            if saved["fingerprint"] != fingerprint:
                return problem(422, "idempotency-key-reused",
                               "Key reused", "This key was sent with a "
                               "different request.")
            if saved["response"] is None:
                log.info("409 in progress key=%s", idempotency_key[1:9])
                return problem(409, "request-in-progress",
                               "Request in progress",
                               "The first request with this key is still "
                               "running.", hint="Retry after a moment "
                               "with the same key.")
            log.info("replayed key=%s %s", idempotency_key[1:9], who)
            status, doc = saved["response"]
            return JSONResponse(doc, status_code=status,
                                headers={"Idempotent-Replayed": "true"})
        keys[slot] = {"fingerprint": fingerprint, "response": None}
        result = await decide(order_id, body, claims, who)
        keys[slot]["response"] = (result.status_code,
                                  json.loads(result.body))
        if result.status_code >= 400:
            del keys[slot]           # a refusal may be retried once fixed
        global SLOW_FIRST
        if SLOW_FIRST and result.status_code == 201:
            SLOW_FIRST = False
            await asyncio.sleep(1.5)  # a slow answer, after the commit
        return result


async def decide(order_id, body, claims, who):
    order = common.ORDERS.get(order_id)
    if order is None or order["customer_id"] != claims["customer_id"]:
        log.info("404 %s %s", order_id, who)
        return problem(404, "order-not-found", "No such order",
                       f"No order with id {order_id}.")
    line = next((ln for ln in order["lines"]
                 if ln["book_id"] == body.book_id), None)
    if line is None:
        return problem(422, "book-not-in-order", "Book not in order",
                       f"{order_id} has no {body.book_id}.",
                       hint="Use a book_id from the order's lines.")
    if order["status"] != "delivered":
        return problem(409, "order-not-delivered", "Not delivered",
                       f"{order_id} is {order['status']}.",
                       hint="Damage refunds are for delivered orders; "
                            "cancel an order that hasn't shipped.")
    case = common.cases().get(body.damage_case)
    if not case or (case["order_id"], case["book_id"]) != (
            order_id, body.book_id):
        log.info("422 damage-case-required %s %s", order_id, who)
        return problem(422, "damage-case-required", "No damage on record",
                       f"No damage case {body.damage_case} for "
                       f"{body.book_id} on {order_id}.",
                       hint="Report the damage to Harbor's fulfillment "
                            "agent (skill report-damage) and send the "
                            "case_id it returns.")
    amount = line["unit_price_cents"]
    approved = [d for d in claims.get("authorization_details", [])
                if d.get("type") == "harbor_refund"
                and d.get("order_id") == order_id
                and d.get("book_id") == body.book_id
                and d.get("max_amount", 0) >= amount]
    if not approved:
        log.info("403 outside-approval %s %s", order_id, who)
        return problem(403, "outside-approval", "Not what was approved",
                       "The person's approval doesn't cover this refund.")
    if (order_id, body.book_id) in by_line:
        done = refunds[by_line[(order_id, body.book_id)]]
        return problem(409, "already-refunded", "Already refunded",
                       f"{body.book_id} on {order_id} was refunded as "
                       f"{done['id']}.", refund_id=done["id"])
    refund = {"id": f"rf-{secrets.token_hex(4)}", "order_id": order_id,
              "book_id": body.book_id, "amount_cents": amount,
              "currency": order["currency"], "status": "pending",
              "damage_case": body.damage_case}
    refunds[refund["id"]] = refund
    by_line[(order_id, body.book_id)] = refund["id"]
    with tracer.start_as_current_span(
            "POST /v1/refunds", kind=SpanKind.CLIENT,
            attributes={"http.request.method": "POST",
                        "server.address": "payment-provider"}):
        async with httpx.AsyncClient(timeout=5) as http:
            resp = await http.post(
                f"{PROVIDER}/v1/refunds",
                headers={"Authorization": f"Bearer {PROVIDER_KEY}",
                         "Idempotency-Key": refund["id"]},
                data={"payment_intent": payments[order_id],
                      "amount": str(amount)})
    resp.raise_for_status()
    refund["provider_refund"] = resp.json()["id"]
    log.info("201 %s %s %s %d cents, provider %s", refund["id"], order_id,
             body.book_id, amount, refund["provider_refund"])
    return JSONResponse(refund, status_code=201, headers={
        "Location": f"/orders/{order_id}/refunds/{refund['id']}"})


@app.get("/orders/{order_id}/refunds/{refund_id}",
         operation_id="get_refund")
async def get_refund(order_id: str, refund_id: str,
                     authorization: str = Header(None)):
    try:
        claims = caller(authorization)
    except (PermissionError, jwt.PyJWTError):
        return problem(401, "unauthorized", "Unauthorized",
                       "A token for this API is required.")
    refund = refunds.get(refund_id)
    if (refund is None or refund["order_id"] != order_id or
            common.ORDERS[order_id]["customer_id"] != claims["customer_id"]):
        return problem(404, "refund-not-found", "No such refund",
                       f"No refund {refund_id} on {order_id}.")
    return refund


@app.post("/hooks/payments", include_in_schema=False)
async def payment_webhook(request: Request):
    body = await request.body()
    try:
        msg_id = webhooks.verify(webhooks.SECRET, request.headers, body)
    except webhooks.InvalidWebhook as err:
        log.info("401 webhook: %s", err)
        return Response(status_code=401)
    if msg_id in seen_hooks:
        return Response(status_code=204)
    event = json.loads(body)
    obj = event["data"]["object"]
    if event["type"] == "payment_intent.succeeded":
        payments[obj["metadata"]["order_id"]] = obj["id"]
    elif event["type"] == "charge.refunded":
        refund = next((r for r in refunds.values()
                       if r["status"] == "pending"
                       and payments.get(r["order_id"]) ==
                       obj["payment_intent"]
                       and r["amount_cents"] == obj["amount"]), None)
        if refund:
            refund["status"] = "succeeded"
            log.info("webhook charge.refunded %s -> %s succeeded",
                     obj["id"], refund["id"])
            asyncio.create_task(tell_host(refund))
    seen_hooks.add(msg_id)
    return Response(status_code=204)


async def tell_host(refund):
    body = json.dumps({"type": "refund.succeeded",
                       "timestamp": int(time.time()),
                       "data": refund}).encode()
    msg_id, ts = f"msg_{secrets.token_hex(8)}", str(int(time.time()))
    headers = {"content-type": "application/json", "webhook-id": msg_id,
               "webhook-timestamp": ts,
               "webhook-signature": webhooks.sign(HOST_SECRET, msg_id, ts,
                                                  body)}
    try:
        async with httpx.AsyncClient(timeout=5) as http:
            r = await http.post(HOST_HOOK, content=body, headers=headers)
        log.info("refund.succeeded %s -> host %s", refund["id"],
                 r.status_code)
    except httpx.HTTPError as err:
        log.info("refund.succeeded %s -> host failed: %s", refund["id"],
                 type(err).__name__)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--slow-first", action="store_true")
    parser.add_argument("--export", action="store_true")
    args = parser.parse_args()
    if args.export:
        print(json.dumps(app.openapi(), indent=2))
        sys.exit()
    SLOW_FIRST = args.slow_first
    for noisy in ("httpx", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    uvicorn.run(app, host="127.0.0.1", port=8123, log_level="warning")
