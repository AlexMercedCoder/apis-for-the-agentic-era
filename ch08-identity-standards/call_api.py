"""Call Harbor's protected API with Rosa's tokens, and with tokens that
should fail: none at all, her ID token, a forged `alg: none` token, a token
whose payload was edited, a token without the scope, and someone else's
order.

    uv run python code_flow.py rosa && uv run python call_api.py
"""

import hashlib
import hmac
import json
from pathlib import Path

import httpx
import jwt

from oauth import API, b64url, claims, discover, header, show

def hs256_with_public_key(real_header, body):
    """The key-confusion forgery: HMAC keyed with the issuer's RSA public
    key in PEM form, which a library that trusts alg would accept."""
    from cryptography.hazmat.primitives import serialization
    jwks = httpx.get(discover()["jwks_uri"]).json()
    jwk = next(k for k in jwks["keys"] if k["kid"] == real_header["kid"])
    pem = jwt.PyJWK(jwk).key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    head = b64url(json.dumps(real_header | {"alg": "HS256"}).encode())
    mac = hmac.new(pem, f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{b64url(mac)}"


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
    real = header(access)        # forgeries reuse the real kid and typ
    none = b64url(json.dumps(real | {"alg": "none"}).encode())
    one_line("alg none, no signature",
             get("/orders/ord-5003", f"{none}.{body}."))
    one_line("HS256, public key as secret",
             get("/orders/ord-5003", hs256_with_public_key(real, body)))
    edited = claims(access) | {"customer_id": "cus-002"}
    edited = b64url(json.dumps(edited).encode())
    one_line("payload edited", get("/orders/ord-5002",
                                   f"{head}.{edited}.{sig}"))
    resp = httpx.get(API + "/orders/ord-5003", headers=[
        ("Authorization", "Bearer x"), ("Authorization", f"Bearer {access}")])
    one_line("two Authorization headers", resp)

    print("\n== Sam's order with Rosa's token")
    theirs = get("/orders/ord-5002", access)
    print(show(theirs))
    missing = get("/orders/ord-9999", access)
    same = (theirs.status_code == missing.status_code
            and list(theirs.headers.keys()) == list(missing.headers.keys())
            and theirs.content.replace(b"5002", b"9999") == missing.content)
    print(f"ord-9999, which doesn't exist: {missing.status_code}, "
          f"same headers and body apart from the ID: {same}")

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
