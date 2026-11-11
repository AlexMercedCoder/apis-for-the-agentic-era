"""A local test issuer for Chapter 12, standing in for Chapter 8's Keycloak.

It signs RS256 access tokens (typ at+jwt) with a key generated on first run
into keys/ (ignored by Git) and serves, on 127.0.0.1:8120:

  /.well-known/openid-configuration, /jwks   discovery and public keys
  POST /token, grant token-exchange          RFC 8693, as in Chapter 8
  POST /bc-authorize                         a CIBA backchannel request
  POST /token, grant urn:openid:params:grant-type:ciba
                                             CIBA poll mode
  GET /phone/pending, POST /phone/decide     the stand-in for Rosa's phone

The CIBA request carries RFC 9396 authorization_details, and the token
issued after Rosa approves carries them too, so the refund it allows is
the one she saw. phone.py plays Rosa's phone; nothing here signs anyone
in, and no value here is a real credential. Never use it outside the demo.

    uv run python issuer.py serve
    uv run python issuer.py grant [rosa|sam]    # the assistant's grant
"""

import json
import secrets
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "http://127.0.0.1:8120"
GATEWAY = "http://127.0.0.1:9080"
MCP_AUD = GATEWAY + "/mcp"
FULFILLMENT_AUD = GATEWAY + "/a2a/fulfillment/"
REFUNDS_AUD = GATEWAY + "/orders"
KEY_FILE = Path(__file__).parent / "keys" / "issuer.pem"
KID = "harbor-ch12-demo"
CIBA = "urn:openid:params:grant-type:ciba"
EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"

# Placeholder client secrets, labeled as such.
SECRETS = {"harbor-assistant": "not-a-real-secret",
           "harbor-support-mcp": "not-a-real-secret"}
PEOPLE = {"rosa": "cus-001", "sam": "cus-002", "mei": "cus-003"}
# What each client may obtain by exchange, per audience: the most scope.
EXCHANGES = {"harbor-assistant": {MCP_AUD: {"orders:read"},
                                  FULFILLMENT_AUD: {"orders:read"}},
             "harbor-support-mcp": {REFUNDS_AUD: {"orders:refund"}}}

pending: dict[str, dict] = {}       # CIBA requests by auth_req_id


def private_key():
    if not KEY_FILE.exists():
        KEY_FILE.parent.mkdir(exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        KEY_FILE.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
    return serialization.load_pem_private_key(KEY_FILE.read_bytes(), None)


def public_pem():
    return private_key().public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def sign(claims, lifetime):
    now = int(time.time())
    claims = {"iss": ISSUER, "iat": now, "exp": now + lifetime,
              "jti": str(uuid.uuid4()), **claims}
    return jwt.encode(claims, private_key(), algorithm="RS256",
                      headers={"typ": "at+jwt", "kid": KID})


def grant(person="rosa"):
    """The assistant-only grant Chapter 8's storefront obtained for it.

    Its audience is the assistant, it carries no Harbor scope, and
    may_act names the one client that may act for the person with it.
    """
    return sign({"sub": person, "customer_id": PEOPLE[person],
                 "azp": "harbor-storefront", "aud": "harbor-assistant",
                 "scope": "delegation:client:harbor-assistant",
                 "may_act": {"client_id": "harbor-assistant"}}, 600)


def verify(token):
    return jwt.decode(token, private_key().public_key(),
                      algorithms=["RS256"], issuer=ISSUER,
                      options={"verify_aud": False,
                               "require": ["exp", "iat", "iss", "sub"]})


def exchange(client, form):
    """RFC 8693: a narrower token for one audience that names the actor."""
    subject = verify(form["subject_token"])
    audience = form.get("audience", "")
    allowed = EXCHANGES.get(client, {}).get(audience)
    if allowed is None:
        return 400, {"error": "invalid_target"}
    if subject["aud"] == "harbor-assistant":
        if subject.get("may_act", {}).get("client_id") != client:
            return 400, {"error": "invalid_grant"}
        held = allowed                       # the grant itself scopes nothing
    elif client == "harbor-support-mcp" and subject["aud"] == MCP_AUD:
        held = set(subject.get("scope", "").split())  # only what it carried
    else:
        return 400, {"error": "invalid_grant"}
    asked = set(form.get("scope", "").split()) or allowed
    scope = asked & allowed & held
    if not scope:
        return 400, {"error": "invalid_scope"}
    act = {"client_id": client}
    if "act" in subject:
        act["act"] = subject["act"]          # RFC 8693 4.1: the chain
    claims = {"sub": subject["sub"], "customer_id": subject["customer_id"],
              "azp": client, "aud": audience, "act": act,
              "scope": " ".join(sorted(scope))}
    if "orders:refund" in scope:
        claims["authorization_details"] = subject["authorization_details"]
    token = sign(claims, min(300, subject["exp"] - int(time.time())))
    return 200, {"access_token": token, "token_type": "Bearer",
                 "issued_token_type":
                     "urn:ietf:params:oauth:token-type:access_token",
                 "scope": claims["scope"], "expires_in": 300}


def backchannel(client, form):
    """CIBA Core 1.0: ask the person, on her own device, to approve."""
    person = form.get("login_hint", "")
    if person not in PEOPLE or "orders:refund" not in form.get("scope", ""):
        return 400, {"error": "invalid_request"}
    details = json.loads(form.get("authorization_details", "[]"))
    if not details or any(d.get("type") != "harbor_refund"
                          for d in details):
        return 400, {"error": "invalid_authorization_details"}
    auth_req_id = secrets.token_urlsafe(16)
    pending[auth_req_id] = {
        "client": client, "person": person, "status": "pending",
        "binding_message": form.get("binding_message", ""),
        "details": details, "expires": time.time() + 120}
    return 200, {"auth_req_id": auth_req_id, "expires_in": 120,
                 "interval": 1}


def ciba_token(client, form):
    req = pending.get(form.get("auth_req_id", ""))
    if req is None or req["client"] != client:
        return 400, {"error": "invalid_grant"}
    if time.time() > req["expires"]:
        return 400, {"error": "expired_token"}
    if req["status"] == "pending":
        return 400, {"error": "authorization_pending"}
    if req["status"] == "denied":
        return 400, {"error": "access_denied"}
    if req["status"] == "issued":
        return 400, {"error": "invalid_grant"}   # one approval, one token
    req["status"] = "issued"
    person = req["person"]
    token = sign({"sub": person, "customer_id": PEOPLE[person],
                  "azp": client, "aud": MCP_AUD,
                  "act": {"client_id": client},
                  "scope": "orders:read orders:refund",
                  "authorization_details": req["details"]}, 120)
    return 200, {"access_token": token, "token_type": "Bearer",
                 "expires_in": 120, "scope": "orders:read orders:refund",
                 "authorization_details": req["details"]}


def jwks():
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(
        private_key().public_key()))
    return {"keys": [{**jwk, "kid": KID, "use": "sig", "alg": "RS256"}]}


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, doc):
        body = json.dumps(doc).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/.well-known/openid-configuration":
            self.reply(200, {
                "issuer": ISSUER, "jwks_uri": ISSUER + "/jwks",
                "token_endpoint": ISSUER + "/token",
                "backchannel_authentication_endpoint":
                    ISSUER + "/bc-authorize",
                "backchannel_token_delivery_modes_supported": ["poll"],
                "grant_types_supported": [CIBA, EXCHANGE],
                "authorization_details_types_supported":
                    ["harbor_refund"]})
        elif self.path == "/jwks":
            self.reply(200, jwks())
        elif self.path.startswith("/phone/pending?person="):
            person = self.path.split("=", 1)[1]
            self.reply(200, [{"auth_req_id": k,
                              "binding_message": v["binding_message"],
                              "details": v["details"]}
                             for k, v in pending.items()
                             if v["person"] == person
                             and v["status"] == "pending"])
        else:
            self.reply(404, {"error": "not_found"})

    def do_POST(self):
        size = int(self.headers.get("content-length", 0))
        form = dict(parse_qsl(self.rfile.read(size).decode()))
        if self.path == "/phone/decide":
            req = pending.get(form.get("auth_req_id", ""))
            if req is None or req["status"] != "pending":
                return self.reply(400, {"error": "invalid_request"})
            req["status"] = ("approved" if form.get("decision") == "approve"
                             else "denied")
            return self.reply(200, {"status": req["status"]})
        client = form.get("client_id", "")
        if SECRETS.get(client) is None or not secrets.compare_digest(
                SECRETS[client], form.get("client_secret", "")):
            return self.reply(401, {"error": "invalid_client"})
        try:
            if self.path == "/bc-authorize":
                return self.reply(*backchannel(client, form))
            if self.path == "/token" and form.get("grant_type") == CIBA:
                return self.reply(*ciba_token(client, form))
            if self.path == "/token" and form.get("grant_type") == EXCHANGE:
                return self.reply(*exchange(client, form))
        except (jwt.PyJWTError, KeyError, ValueError):
            return self.reply(400, {"error": "invalid_grant"})
        self.reply(400, {"error": "unsupported_grant_type"})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "grant":
        print(grant(sys.argv[2] if len(sys.argv) > 2 else "rosa"))
    elif cmd == "pem":
        print(public_pem(), end="")
    else:
        private_key()
        ThreadingHTTPServer(("127.0.0.1", 8120), Handler).serve_forever()
