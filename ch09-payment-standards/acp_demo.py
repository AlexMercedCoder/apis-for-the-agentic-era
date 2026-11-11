"""An agent checkout at Harbor under the Agentic Commerce Protocol.

The agent host (the platform the shopping agent runs in) does what ACP
2026-04-17 gives it to do: create a checkout session at the seller, show
the person the total and get approval, ask the payment provider for a
delegated, one-time token limited to that total, seller, and session,
and complete the session with the token. The model never sees the card.

Then the host and a misbehaving seller try the token where its allowance
says no. Usage: acp_demo.py --dump-dir captures
"""

import argparse
import datetime as dt
import json
import uuid
from pathlib import Path

import httpx

from harbor_shop import ACP_KEY, API_VERSION
from provider import (MERCHANT_SECRET_KEY, OTHER_SHOP_KEY,
                      PLATFORM_SECRET_KEY)

HARBOR = "http://127.0.0.1:8009"
PROVIDER = "http://127.0.0.1:8091"
APPROVAL_LIMIT = 3000      # Rosa's rule: ask her above $30.00
DUMP = Path(".")


def save(name: str, obj: dict) -> None:
    (DUMP / name).write_text(json.dumps(obj, indent=2) + "\n")


def acp(path: str, body: dict | None = None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {ACP_KEY}",
               "API-Version": API_VERSION,
               "Idempotency-Key": str(uuid.uuid4())}
    return httpx.post(f"{HARBOR}{path}", headers=headers, json=body)


def delegate(session: dict, max_amount: int, number: str,
             record: bool = True) -> httpx.Response:
    expires = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=15)
    body = {
        "payment_method": {
            "type": "card", "card_number_type": "fpan", "number": number,
            "exp_month": "12", "exp_year": "2030",
            "display_card_funding_type": "credit", "display_brand": "visa",
            "display_last4": number[-4:], "metadata": {}},
        "allowance": {
            "reason": "one_time", "max_amount": max_amount,
            "currency": "usd", "checkout_session_id": session["id"],
            "merchant_id": "harbor-books",
            "expires_at": expires.isoformat(timespec="seconds")},
        "risk_signals": [{"type": "card_testing", "score": 2,
                          "action": "authorized"}],
        "metadata": {"agent": "harbor-assistant"},
    }
    if record:
        save("delegate-request.json", body)
    return httpx.post(
        f"{PROVIDER}/agentic_commerce/delegate_payment", json=body,
        headers={"Authorization": f"Bearer {PLATFORM_SECRET_KEY}",
                 "API-Version": API_VERSION,
                 "Idempotency-Key": str(uuid.uuid4())})


def charge(token: str, amount: int, session_id: str,
           key: str = MERCHANT_SECRET_KEY) -> str:
    r = httpx.post(f"{PROVIDER}/v1/payment_intents",
                   headers={"Authorization": f"Bearer {key}"},
                   data={"amount": amount, "currency": "usd",
                         "payment_method": token, "confirm": "true",
                         "metadata[checkout_session_id]": session_id})
    body = r.json()
    return f"{r.status_code} " + (body.get("status") or
                                  body["error"]["message"])


def main() -> None:
    global DUMP
    parser = argparse.ArgumentParser()
    parser.add_argument("--dump-dir", default=".")
    DUMP = Path(parser.parse_args().dump_dir)

    create = {"line_items": [{"id": "bk-1003"}], "currency": "usd",
              "buyer": {"email": "rosa@example.com"}, "capabilities": {}}
    save("acp-create-request.json", create)
    r = acp("/checkout_sessions", create)
    session = r.json()
    save("acp-create-response.json", session)
    total = next(t["amount"] for t in session["totals"]
                 if t["type"] == "total")
    print(f"create session: {r.status_code} {session['id']} "
          f"{session['status']} total={total}")

    if total > APPROVAL_LIMIT:
        raise SystemExit(f"needs Rosa's approval: {total} > "
                         f"{APPROVAL_LIMIT}")
    print(f"host: {total} <= Rosa's limit {APPROVAL_LIMIT}, approved")

    r = delegate(session, total, "4242424242424242")
    vault = r.json()
    save("delegate-response.json", vault)
    print(f"delegate_payment: {r.status_code} {vault['id']}")

    complete = {"payment_data": {
        "handler_id": "card",
        "instrument": {"type": "card", "credential": {
            "type": "vault_token", "token": vault["id"]}}}}
    save("acp-complete-request.json", complete)
    r = acp(f"/checkout_sessions/{session['id']}/complete", complete)
    done = r.json()
    save("acp-complete-response.json", done)
    print(f"complete: {r.status_code} {done['status']} "
          f"order={done['order']['id']}")

    print("misuse of delegated tokens:")
    rows = [("same token, second charge", vault["id"], total, session["id"])]
    r2 = acp("/checkout_sessions", create).json()
    v2 = delegate(r2, 2299, "4242424242424242", record=False).json()
    rows += [("seller charges 1 cent more", v2["id"], 2300, r2["id"]),
             ("token used on another session", v2["id"], 2299, session["id"])]
    for label, token, amount, sid in rows:
        print(f"  {label:<33} {charge(token, amount, sid)}")
    other = charge(v2["id"], 2299, r2["id"], key=OTHER_SHOP_KEY)
    print(f"  {'another seller\'s API key':<33} {other}")
    print(f"  {'the right seller, amount, session':<33} "
          f"{charge(v2['id'], 2299, r2['id'])}")


if __name__ == "__main__":
    main()
