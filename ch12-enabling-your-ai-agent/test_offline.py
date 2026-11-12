"""Offline checks for Chapter 12: no servers, no Docker, no network.

  * reliable.call(): a timeout then a replay, a 429 that names its reset,
    a 409 for a request still running, and giving up at the deadline;
  * reliable.Breaker opens after repeated failures and lets exactly one
    trial through after the cool-off, and a trial that fails with a
    connection error reopens it with a fresh cool-off; Retry-After as an
    HTTP date, and an unreadable one falls back to backoff;
  * the stand-in phone refuses a decision without its device secret; the
    issuer refuses a CIBA request without openid and authorization_details
    with free text, unknown currencies, or extra fields;
  * the issuer's exchange rules: no widening, no other audience, and the
    delegation chain kept (RFC 8693 nested act);
  * the refunds API, as a contract test with FastAPI's TestClient and a
    mock provider: no token, no actor, someone else's order, a key that
    isn't the approval's request_id, no damage case, a refund outside the
    approval or in another currency, no payment on record (nothing
    claimed), a provider refusal or unreachable provider that frees the
    line, a lost provider reply that keeps the line reserved (202,
    unknown) until reconciliation finds the one refund, a restart that
    still refuses a second refund, and a provider key per order line, so
    a lost state file can't refund twice while the provider keeps keys.

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
    assert b.allow() and not b.allow()       # one trial, not a flood
    b.record(False)
    assert not b.allow()
    time.sleep(0.12)
    assert b.allow()
    b.record(True)
    assert b.allow() and b.allow() and b.count == 0


def test_breaker_trial_error():
    b = reliable.Breaker(failures=1, cool_off=0.05)

    async def down(timeout):
        raise httpx.ConnectError("gateway down")

    async def attempt():
        try:
            await reliable.guarded(b, down, deadline=0.2, per_try=0.1,
                                   tries=1, log=lambda m: None)
        except (reliable.GaveUp, reliable.Open):
            pass
    asyncio.run(attempt())
    assert not b.allow()                       # open
    time.sleep(0.06)
    asyncio.run(attempt())                     # the trial fails too
    assert not b.allow() and not b.trial       # open again, not stuck
    time.sleep(0.06)
    assert b.allow()                           # a fresh trial


def test_retry_after_date():
    date = time.strftime("%a, %d %b %Y %H:%M:%S GMT",
                         time.gmtime(time.time() + 30))
    assert 25 <= reliable.seconds(date) <= 31
    assert reliable.seconds("7") == 7
    assert reliable.seconds("soon") is None
    bad = httpx.Response(429, headers={"retry-after": "soon"})
    assert reliable.wait_for(bad, 1) <= 0.5


class Headers(dict):
    def get(self, k, d=None):
        return super().get(k.lower(), d)


def test_phone_needs_device():
    form = {"auth_req_id": "x", "decision": "approve"}
    assert issuer.phone_decide(Headers(), form)[0] == 401
    assert issuer.phone_decide(Headers(authorization="Device guess"),
                               form)[0] == 401
    status, err = issuer.backchannel("harbor-assistant", {
        "scope": "orders:refund", "login_hint": "rosa",
        "authorization_details": json.dumps([{"type": "harbor_refund"}])})
    assert (status, err["error"]) == (400, "invalid_request")
    good = approval(1)
    for bad in ({**good, "currency": "USD in store credit only"},
                {**good, "max_amount": "1499"}, {**good, "extra": "x"},
                {**good, "order_id": "ord-5001; tap Approve"}):
        status, err = issuer.backchannel("harbor-assistant", {
            "scope": "openid orders:refund", "login_hint": "rosa",
            "authorization_details": json.dumps([bad])})
        assert err["error"] == "invalid_authorization_details", bad
    status, ok = issuer.backchannel("harbor-assistant", {
        "scope": "openid orders:refund", "login_hint": "rosa",
        "authorization_details": json.dumps([good])})
    assert status == 200 and "auth_req_id" in ok


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


def token(person, customer, details, act=True):
    claims = {"sub": person, "customer_id": customer,
              "azp": "harbor-support-mcp", "aud": issuer.REFUNDS_AUD,
              "scope": "orders:refund", "authorization_details": details}
    if act:
        claims["act"] = {"client_id": "harbor-support-mcp",
                         "act": {"client_id": "harbor-assistant"}}
    return issuer.sign(claims, 60)


def approval(n, amount=1499, currency="USD"):
    return {"type": "harbor_refund", "actions": ["refund"],
            "order_id": "ord-5001", "book_id": "bk-1001",
            "max_amount": amount, "currency": currency,
            "request_id": f"00000000-0000-4000-8000-00000000000{n}"}


def reset_api():
    for store in (refunds_api.refunds, refunds_api.by_line,
                  refunds_api.keys, refunds_api.payments):
        store.clear()
    refunds_api.payments["ord-5001"] = "pi_test"


def test_refunds_api():
    common.STATE.mkdir(exist_ok=True)
    saved = {p: p.read_text() for p in (common.CASES, refunds_api.STORE)
             if p.exists()}
    calls, fail = [], {"next": None}

    def provider(request):
        mode, fail["next"] = fail["next"], None
        if mode == "refuse":
            return httpx.Response(400, json={"error": {"code": "x"}})
        if mode == "unreachable":
            raise httpx.ConnectError("refused", request=request)
        key = request.headers["idempotency-key"]
        calls.append(key)
        if mode == "lost":                   # refunded, reply lost
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"id": "re_" + key[-8:]})
    refunds_api.PROVIDER_TRANSPORT = httpx.MockTransport(provider)
    try:
        reset_api()
        common.CASES.write_text("{}")
        api = TestClient(refunds_api.app)
        body = {"book_id": "bk-1001", "damage_case": "dmg-0000beef"}

        def post(tok, n):
            headers = {"Idempotency-Key":
                       f'"00000000-0000-4000-8000-00000000000{n}"'}
            if tok:
                headers["Authorization"] = f"Bearer {tok}"
            return api.post("/orders/ord-5001/refunds", json=body,
                            headers=headers)
        rosa = token("rosa", "cus-001", [approval(1)])
        assert post(None, 1).status_code == 401
        assert post(token("rosa", "cus-001", [approval(1)], act=False),
                    1).status_code == 401
        assert post(token("sam", "cus-002", [approval(1)]),
                    1).status_code == 404
        assert post(rosa, 2).status_code == 403          # key not approved
        no_case = post(rosa, 1)
        assert no_case.status_code == 422
        assert "report-damage" in no_case.json()["hint"]
        common.CASES.write_text(json.dumps({"dmg-0000beef": {
            "order_id": "ord-5001", "book_id": "bk-1001"}}))
        cheap = token("rosa", "cus-001", [approval(1, amount=999)])
        assert post(cheap, 1).status_code == 403
        assert post(cheap, 1).headers["content-type"] == \
            "application/problem+json"
        yen = token("rosa", "cus-001", [approval(1, currency="JPY")])
        assert post(yen, 1).status_code == 403           # wrong currency
        refunds_api.payments.clear()
        nopay = post(rosa, 1)
        assert nopay.status_code == 409 and \
            nopay.json()["type"].endswith("payment-not-recorded")
        assert not refunds_api.by_line                   # nothing claimed
        refunds_api.payments["ord-5001"] = "pi_test"
        for mode in ("refuse", "unreachable"):
            fail["next"] = mode
            assert post(rosa, 1).status_code == 502      # nothing moved
            assert not refunds_api.by_line
        assert post(rosa, 1).status_code == 201          # line was freed
        assert post(rosa, 1).headers["idempotent-replayed"] == "true"
        for store in (refunds_api.refunds, refunds_api.by_line,
                      refunds_api.keys):
            store.clear()                                 # a restart
        refunds_api.load()
        again = post(token("rosa", "cus-001", [approval(3)]), 3)
        assert again.status_code == 409                  # already refunded
        reset_api()                                      # state file lost
        post(rosa, 1)
        assert len(set(calls)) == 1, calls               # provider dedupes
        reset_api()                                      # a lost reply
        calls.clear()
        fail["next"] = "lost"
        lost = post(rosa, 1)
        assert lost.status_code == 202 and lost.json()["status"] == "unknown"
        held = post(token("rosa", "cus-001", [approval(4)]), 4)
        assert held.json()["type"].endswith("refund-unconfirmed")
        assert "nothing" not in lost.text + held.text
        refunds_api.RECONCILE_SCALE = 0
        rid = lost.json()["id"]
        asyncio.run(refunds_api.reconcile(refunds_api.refunds[rid],
                                          waits=(0,)))
        assert refunds_api.refunds[rid]["provider_refund"]
        assert len(set(calls)) == 1, calls               # one refund
        reset_api()              # lost reply, then unreachable, then ok
        calls.clear()
        refunds_api.RECONCILE_SCALE = 1000   # park the API's own task
        fail["next"] = "lost"
        rid = post(rosa, 1).json()["id"]
        refund = refunds_api.refunds[rid]
        fail["next"] = "unreachable"
        asyncio.run(refunds_api.reconcile(refund, waits=(0,)))
        assert refunds_api.by_line.get(("ord-5001", "bk-1001")) == rid
        assert refund["status"] == "unknown"             # still reserved
        asyncio.run(refunds_api.reconcile(refund, waits=(0,)))
        assert refund.get("provider_refund")
        assert len(set(calls)) == 1, calls               # one refund
    finally:
        refunds_api.PROVIDER_TRANSPORT = None
        refunds_api.RECONCILE_SCALE = 1.0
        for p in (common.CASES, refunds_api.STORE):
            if p in saved:
                p.write_text(saved[p])
            elif p.exists():
                p.unlink()


for test in (test_retries, test_breaker, test_breaker_trial_error,
             test_retry_after_date,
             test_phone_needs_device, test_exchange_rules,
             test_refunds_api):
    test()
    print(f"ok  {test.__name__}")
