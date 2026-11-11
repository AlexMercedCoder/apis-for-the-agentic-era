"""Call Harbor's protected API with Rosa's tokens, and with tokens that
should fail: none at all, her ID token, a forged `alg: none` token, a token
whose payload was edited, a token without the scope, and someone else's
order.

    uv run python code_flow.py rosa && uv run python call_api.py
"""

import base64
import json
from pathlib import Path

import httpx

from oauth import API, b64url, claims, header, show

TOKENS = json.loads(Path(__file__).with_name("tokens.json").read_text())


def get(path, token=None, method="GET", headers=None, **kw):
    headers = dict(headers or {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.request(method, API + path, headers=headers, **kw)


def one_line(label, resp):
    challenge = resp.headers.get("www-authenticate", "")
    detail = resp.json().get("detail", "") if resp.content else ""
    print(f"{label:<28} {resp.status_code}  {detail}")
    if challenge:
        print(f"{'':<28}      {challenge.split(', error_description')[0]}")


def main():
    rosa = TOKENS["rosa"]
    access, id_token = rosa["access_token"], rosa["id_token"]

    print("== decoded access token (header, then payload)")
    print(json.dumps(header(access)))
    print(json.dumps(claims(access), indent=2))

    print("\n== no token")
    print(show(get("/orders/ord-5003")))

    print("\n== Rosa's access token, her own order")
    resp = get("/orders/ord-5003", access)
    print(f"{resp.status_code} {resp.json()['id']} {resp.json()['status']}"
          f" customer {resp.json()['customer_id']}")

    print("\n== tokens that must fail")
    one_line("ID token as access token", get("/orders/ord-5003", id_token))
    head, body, sig = access.split(".")
    none = b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    one_line("alg none, no signature",
             get("/orders/ord-5003", f"{none}.{body}."))
    edited = claims(access) | {"customer_id": "cus-002"}
    edited = b64url(json.dumps(edited).encode())
    one_line("payload edited", get("/orders/ord-5002",
                                   f"{head}.{edited}.{sig}"))

    print("\n== Sam's order with Rosa's token")
    print(show(get("/orders/ord-5002", access)))

    print("\n== an order for another customer")
    order = json.loads((Path(__file__).parent.parent / "ch03-rest-apis"
                        / "requests" / "place-order.json").read_text())
    order["customer_id"] = "cus-002"
    resp = get("/orders", access, method="POST", json=order, headers={
        "Idempotency-Key": '"0b6f2d1e-8c3a-4f5b-9d7e-1a2b3c4d5e6f"'})
    one_line("customer_id cus-002", resp)

    print("\n== the same order for Rosa herself")
    order["customer_id"] = "cus-001"
    resp = get("/orders", access, method="POST", json=order, headers={
        "Idempotency-Key": '"0b6f2d1e-8c3a-4f5b-9d7e-1a2b3c4d5e6f"'})
    print(resp.status_code, resp.headers.get("location"))

    if "reader" in TOKENS:
        print("\n== a token with orders:read only tries to cancel")
        print(show(get("/orders/ord-5003/cancellation",
                       TOKENS["reader"]["access_token"], method="PUT",
                       json={"reason": "customer_request"})))


if __name__ == "__main__":
    main()
