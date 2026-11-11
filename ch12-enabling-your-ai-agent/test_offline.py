"""Offline checks for Chapter 12: no servers, no Docker, no network.

  * reliable.call(): a timeout then a replay, a 429 that names its reset,
    a 409 for a request still running, and giving up at the deadline;
  * reliable.Breaker opens after repeated failures and lets one call
    through after the cool-off;
  * the issuer's exchange rules: no widening, no other audience, and the
    delegation chain kept (RFC 8693 nested act);
  * the refunds API's refusals, as a contract test with FastAPI's
    TestClient: no token, someone else's order, no damage case on
    record, and a refund outside what the person approved.

    uv run python test_offline.py
"""

import asyncio
import json
import time

import httpx
from fastapi.testclient import TestClient

import common
import issuer
import refunds_api
import reliable


def resp(status, headers=None, body=None):
    return httpx.Response(status, headers=headers or {},
                          content=json.dumps(body or {}).encode())


async def scripted(outcomes):
    log = []

    async def send(timeout):
        item = outcomes.pop(0)
        if item == "timeout":
            raise httpx.ReadTimeout("slow")
        return item
    result = await reliable.call(send, deadline=5, per_try=0.5,
                                 log=log.append)
    return result, log


def test_retries():
    ok, log = asyncio.run(scripted(["timeout", resp(201)]))
    assert ok.status_code == 201 and "timed out" in log[0]
    ok, log = asyncio.run(scripted([
        resp(429, {"x-ratelimit-reset": "0.2"}), resp(200)]))
    assert ok.status_code == 200 and log[0].startswith("attempt 1: 429")
    assert reliable.wait_for(resp(429, {"x-ratelimit-reset": "30"}),
                             1) >= 30
    busy = resp(409, {"content-type": "application/problem+json"},
                {"type": reliable.IN_PROGRESS})
    ok, _ = asyncio.run(scripted([busy, resp(201)]))
    assert ok.status_code == 201
    final = resp(409, {"content-type": "application/problem+json"},
                 {"type": "https://harbor.example/problems/"
                          "already-refunded"})
    ok, log = asyncio.run(scripted([final]))
    assert ok.status_code == 409 and not log
    try:
        asyncio.run(scripted([resp(429, {"x-ratelimit-reset": "60"})]))
        raise AssertionError("waited past the deadline")
    except reliable.GaveUp as err:
        assert "429" in str(err)


def test_breaker():
    b = reliable.Breaker(failures=2, cool_off=0.1)
    b.record(False)
    assert b.allow()
    b.record(False)
    assert not b.allow()
    time.sleep(0.12)
    assert b.allow()
    b.record(True)
    assert b.allow() and b.count == 0


def form(client, subject, audience, scope):
    return client, {"subject_token": subject, "audience": audience,
                    "scope": scope}


def test_exchange_rules():
    grant = issuer.grant("rosa")
    status, mcp = issuer.exchange(*form("harbor-assistant", grant,
                                        issuer.MCP_AUD,
                                        "orders:read orders:refund"))
    assert status == 200 and mcp["scope"] == "orders:read"
    status, _ = issuer.exchange(*form("harbor-assistant", grant,
                                      issuer.REFUNDS_AUD, "orders:refund"))
    assert status == 400
    status, err = issuer.exchange(*form("harbor-support-mcp",
                                        mcp["access_token"],
                                        issuer.REFUNDS_AUD, "orders:refund"))
    assert (status, err["error"]) == (400, "invalid_scope")
    approved = issuer.sign({
        "sub": "rosa", "customer_id": "cus-001", "azp": "harbor-assistant",
        "aud": issuer.MCP_AUD, "act": {"client_id": "harbor-assistant"},
        "scope": "orders:read orders:refund",
        "authorization_details": [{"type": "harbor_refund"}]}, 60)
    status, out = issuer.exchange(*form("harbor-support-mcp", approved,
                                        issuer.REFUNDS_AUD, "orders:refund"))
    claims = common.check_token(out["access_token"], issuer.REFUNDS_AUD)
    assert common.actors(claims) == ["harbor-support-mcp",
                                     "harbor-assistant"]


def token(person, customer, details):
    return issuer.sign({
        "sub": person, "customer_id": customer, "azp": "harbor-support-mcp",
        "aud": issuer.REFUNDS_AUD, "scope": "orders:refund",
        "act": {"client_id": "harbor-support-mcp",
                "act": {"client_id": "harbor-assistant"}},
        "authorization_details": details}, 60)


def test_refund_refusals():
    common.STATE.mkdir(exist_ok=True)
    saved = common.CASES.read_text() if common.CASES.exists() else None
    common.CASES.write_text("{}")
    try:
        api = TestClient(refunds_api.app)
        approval = [{"type": "harbor_refund", "order_id": "ord-5001",
                     "book_id": "bk-1001", "max_amount": 1499}]
        body = {"book_id": "bk-1001", "damage_case": "dmg-5001-1001"}

        def post(tok, key):
            headers = {"Idempotency-Key": f'"{key}"'}
            if tok:
                headers["Authorization"] = f"Bearer {tok}"
            return api.post("/orders/ord-5001/refunds", json=body,
                            headers=headers)
        keys = iter(f"00000000-0000-4000-8000-00000000000{n}"
                    for n in range(10))
        assert post(None, next(keys)).status_code == 401
        sam = post(token("sam", "cus-002", approval), next(keys))
        assert sam.status_code == 404
        rosa = token("rosa", "cus-001", approval)
        no_case = post(rosa, next(keys))
        assert no_case.status_code == 422
        assert "report-damage" in no_case.json()["hint"]
        common.CASES.write_text(json.dumps({"dmg-5001-1001": {
            "order_id": "ord-5001", "book_id": "bk-1001"}}))
        cheap = [dict(approval[0], max_amount=999)]
        outside = post(token("rosa", "cus-001", cheap), next(keys))
        assert outside.status_code == 403
        assert outside.headers["content-type"] == \
            "application/problem+json"
    finally:
        if saved is None:
            common.CASES.unlink()
        else:
            common.CASES.write_text(saved)


for test in (test_retries, test_breaker, test_exchange_rules,
             test_refund_refusals):
    test()
    print(f"ok  {test.__name__}")
