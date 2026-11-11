"""A local test issuer that stands in for Chapter 8's Keycloak.

It signs RS256 access tokens (typ at+jwt) with a key it generates on first
run into keys/ (ignored by Git), and serves the discovery document and the
public key set (JWKS) on 127.0.0.1:8110, so Harbor and the MCP server can
validate tokens the way Chapter 8 taught. It has no login: run.sh mints the
tokens each caller would have received. Never use it outside this demo.

    uv run python issuer.py serve                 # 127.0.0.1:8110
    uv run python issuer.py mint harbor-assistant # print one token
    uv run python issuer.py pem                   # the public key, PEM
"""

import json
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "http://127.0.0.1:8110"
GATEWAY = "http://127.0.0.1:9080"
KEY_FILE = Path(__file__).parent / "keys" / "issuer.pem"
KID = "harbor-ch11-demo"

# One entry per caller type. The gateway knows each by its azp claim.
CLIENTS = {
    "harbor-storefront": {"sub": "rosa", "customer_id": "cus-001",
                          "scope": "orders:read orders:write",
                          "aud": GATEWAY},
    "harbor-partner": {"sub": "harbor-partner", "scope": "catalog:read",
                       "aud": GATEWAY},
    "harbor-assistant": {"sub": "rosa", "customer_id": "cus-001",
                         "act": {"client_id": "harbor-assistant"},
                         "scope": "orders:read", "aud": GATEWAY + "/mcp"},
}


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


def mint(client, lifetime=300):
    now = int(time.time())
    claims = {"iss": ISSUER, "azp": client, "iat": now,
              "exp": now + lifetime, "jti": str(uuid.uuid4()),
              **CLIENTS[client]}
    return jwt.encode(claims, private_key(), algorithm="RS256",
                      headers={"typ": "at+jwt", "kid": KID})


def jwks():
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(
        private_key().public_key()))
    return {"keys": [{**jwk, "kid": KID, "use": "sig", "alg": "RS256"}]}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        docs = {"/.well-known/openid-configuration": {
                    "issuer": ISSUER, "jwks_uri": ISSUER + "/jwks"},
                "/jwks": jwks()}
        if self.path not in docs:
            self.send_error(404)
            return
        body = json.dumps(docs[self.path]).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "mint":
        print(mint(sys.argv[2]))
    elif cmd == "pem":
        print(public_pem(), end="")
    else:
        private_key()
        ThreadingHTTPServer(("127.0.0.1", 8110), Handler).serve_forever()
