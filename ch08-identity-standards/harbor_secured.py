"""Chapter 3's Harbor REST API behind OAuth 2.0 access tokens.

This file is a resource server in front of ../ch03-rest-apis/harbor_api.py.
It runs Chapter 3's app unchanged and adds, in front of every /orders route:

  * JWT validation against the issuer's published keys (JWKS): algorithm
    pinned to RS256, issuer, audience (Harbor's resource identifier), expiry,
    and the claims Harbor relies on;
  * DPoP (RFC 9449) proof checks for tokens bound to a key (`cnf.jkt`);
  * scope checks per route, answered with 403 and `insufficient_scope`;
  * ownership: a customer's token sees and changes only that customer's
    orders, and the order body's `customer_id` must be the token's.

It also serves its OAuth 2.0 Protected Resource Metadata (RFC 9728) at
/.well-known/oauth-protected-resource. Book routes stay public.

    uv run python harbor_secured.py        # 127.0.0.1:8008
"""

import base64
import hashlib
import json
import re
import sys
import time
from pathlib import Path

import httpx
import jwt
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "ch03-rest-apis"))
import harbor_api  # noqa: E402  Chapter 3's app, unchanged

ISSUER = "http://127.0.0.1:8080/realms/harbor"
RESOURCE = "http://127.0.0.1:8008"
PRM_PATH = "/.well-known/oauth-protected-resource"
PRM_URL = RESOURCE + PRM_PATH
SCOPES = ["orders:read", "orders:write"]
ALGORITHMS = ["RS256"]            # what this issuer signs with, and only that
DPOP_ALGORITHMS = ["ES256"]
DPOP_WINDOW = 60                  # seconds a proof stays acceptable
DPOP_SKEW = 5                     # seconds a client clock may run ahead
ACCESS_TYPES = {"at+jwt", "application/at+jwt"}   # RFC 9068 section 4

# Fixed descriptions: nothing from the token is ever echoed back, so a
# sender can't inject parameters into WWW-Authenticate (RFC 6750 sec. 3).
TOKEN_ERRORS = [
    (jwt.ExpiredSignatureError, "token expired"),
    (jwt.InvalidAudienceError, "audience mismatch"),
    (jwt.InvalidIssuerError, "issuer mismatch"),
    (jwt.InvalidAlgorithmError, "algorithm not allowed"),
    (jwt.InvalidSignatureError, "signature verification failed"),
    (jwt.MissingRequiredClaimError, "required claim missing"),
    (jwt.PyJWKClientError, "unknown signing key"),
    (jwt.DecodeError, "malformed token"),
]


def describe(exc: Exception) -> str:
    for kind, text in TOKEN_ERRORS:
        if isinstance(exc, kind):
            return text
    return "invalid token"

METADATA = {
    "resource": RESOURCE,
    "authorization_servers": [ISSUER],
    "scopes_supported": SCOPES,
    "bearer_methods_supported": ["header"],
    "dpop_signing_alg_values_supported": DPOP_ALGORITHMS,
    "resource_name": "Harbor Books API",
}


class Denied(Exception):
    def __init__(self, status, error, description, scope=None, dpop=False):
        super().__init__(description)
        self.status, self.error, self.description = status, error, description
        self.scope, self.dpop = scope, dpop


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def jwk_thumbprint(jwk: dict) -> str:
    """RFC 7638 thumbprint of an EC or RSA public key."""
    members = {"EC": ("crv", "kty", "x", "y"), "RSA": ("e", "kty", "n")}
    fields = members.get(jwk.get("kty"))
    if fields is None:
        raise Denied(401, "invalid_dpop_proof", "unsupported key type",
                     dpop=True)
    canonical = json.dumps({k: jwk[k] for k in fields},
                           separators=(",", ":"), sort_keys=True)
    return b64url(hashlib.sha256(canonical.encode()).digest())


class Validator:
    def __init__(self):
        meta = httpx.get(f"{ISSUER}/.well-known/openid-configuration")
        meta = meta.raise_for_status().json()
        if meta["issuer"] != ISSUER:      # the document must name itself
            raise SystemExit("issuer mismatch in discovery metadata")
        self.keys = jwt.PyJWKClient(meta["jwks_uri"], cache_keys=True)
        # DPoP replay cache: jti -> time it stops being acceptable. In
        # memory, so it covers one process. Several instances need a
        # shared store (or DPoP server nonces, RFC 9449 section 8).
        self.seen_jti = {}

    def access_token(self, token: str) -> dict:
        try:
            head = jwt.get_unverified_header(token)
            if head.get("alg") not in ALGORITHMS:
                raise jwt.InvalidAlgorithmError()
            if str(head.get("typ", "")).lower() not in ACCESS_TYPES:
                raise Denied(401, "invalid_token", "not an access token")
            key = self.keys.get_signing_key_from_jwt(token)
            return jwt.decode(
                token, key.key, algorithms=ALGORITHMS, audience=RESOURCE,
                issuer=ISSUER,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]})
        except jwt.PyJWTError as exc:
            raise Denied(401, "invalid_token", describe(exc)) from None

    def dpop_proof(self, proof: str, method: str, url: str, token: str,
                   jkt: str):
        try:
            head = jwt.get_unverified_header(proof)
            if head.get("typ") != "dpop+jwt":
                raise Denied(401, "invalid_dpop_proof", "typ is not dpop+jwt",
                             dpop=True)
            if head.get("alg") not in DPOP_ALGORITHMS:
                raise Denied(401, "invalid_dpop_proof", "algorithm not allowed",
                             dpop=True)
            jwk = head.get("jwk", {})
            if "d" in jwk:
                raise Denied(401, "invalid_dpop_proof", "private key in jwk",
                             dpop=True)
            key = jwt.PyJWK(jwk, algorithm=head["alg"]).key
            claims = jwt.decode(proof, key, algorithms=DPOP_ALGORITHMS,
                                leeway=DPOP_SKEW,
                                options={"require": ["jti", "htm", "htu",
                                                     "iat", "ath"]})
        except jwt.PyJWTError as exc:
            raise Denied(401, "invalid_dpop_proof", describe(exc),
                         dpop=True) from None
        except (KeyError, TypeError, ValueError):
            raise Denied(401, "invalid_dpop_proof", "malformed proof",
                         dpop=True) from None
        now = time.time()
        checks = [
            (jwk_thumbprint(jwk) == jkt, "proof key is not the token's key"),
            (claims["htm"] == method, "htm does not match the method"),
            (claims["htu"] == url, "htu does not match the URL"),
            (now - DPOP_WINDOW <= claims["iat"] <= now + DPOP_SKEW,
             "proof outside its time window"),
            (claims["ath"] == b64url(hashlib.sha256(token.encode()).digest()),
             "ath does not match the token"),
            (claims["jti"] not in self.seen_jti, "proof already used"),
        ]
        for passed, why in checks:
            if not passed:
                raise Denied(401, "invalid_dpop_proof", why, dpop=True)
        # Keep each jti until its proof could no longer be accepted.
        self.seen_jti = {j: t for j, t in self.seen_jti.items() if t > now}
        self.seen_jti[claims["jti"]] = claims["iat"] + DPOP_WINDOW


def needs(method: str, path: str):
    """The scope a route needs, or None for a public route."""
    if not path.startswith("/orders"):
        return None
    return "orders:read" if method == "GET" else "orders:write"


ORDER_PATH = re.compile(r"^/orders/(ord-[0-9]{4})(/cancellation)?$")


class TokenGate:
    def __init__(self, inner):
        self.inner = inner
        self.validator = Validator()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        method, path = scope["method"], scope["path"]
        if path == PRM_PATH:
            return await self.json(send, 200, METADATA)
        wanted = needs(method, path)
        if wanted is None:
            return await self.inner(scope, receive, send)
        headers, seen = {}, []
        for k, v in scope["headers"]:
            name = k.decode().lower()
            seen.append(name)
            headers[name] = v.decode()
        bound = False
        try:
            for single in ("authorization", "dpop"):
                if seen.count(single) > 1:   # RFC 9110 5.3, RFC 9449 4.3
                    raise Denied(400, "invalid_request",
                                 f"more than one {single} header")
            claims = self.authenticate(method, path, headers, wanted)
            bound = "cnf" in claims
            granted = set(claims.get("scope", "").split())
            any_order = "orders:read:all" in granted and method == "GET"
            if wanted not in granted and not any_order:
                raise Denied(403, "insufficient_scope",
                             f"{method} {path} needs {wanted}", scope=wanted,
                             dpop=bound)
            customer = claims.get("customer_id")
            if customer is None and not any_order:
                raise Denied(403, None, "token is not for a customer")
            match = ORDER_PATH.match(path)
            if match:
                order = harbor_api.store.orders.get(match.group(1))
                missing = order is None
                foreign = (not missing and not any_order
                           and order["customer_id"] != customer)
                if missing or foreign:
                    # One 404, byte for byte, whether the order is missing
                    # or someone else's, and no hint listing valid IDs.
                    self.log(scope, 404, f"caller={self.who(claims)} "
                             + ("not-owner" if foreign else "missing"))
                    return await self.not_found(scope, send, match.group(1))
            if method == "POST" and path == "/orders":
                body, receive = await self.read_body(receive)
                try:
                    asserted = json.loads(body).get("customer_id")
                except (ValueError, AttributeError):
                    asserted = None
                if asserted not in (None, customer):
                    raise Denied(403, None,
                                 "customer_id is not the token's customer")
        except Denied as denied:
            self.log(scope, denied.status,
                     f"error={denied.error or denied.description}")
            return await self.deny(send, denied)
        scope.setdefault("state", {})["caller"] = self.who(claims)
        await self.inner(scope, receive, send)

    def authenticate(self, method, path, headers, wanted):
        scheme, _, token = headers.get("authorization", "").partition(" ")
        scheme = scheme.lower()
        if scheme not in ("bearer", "dpop") or not token:
            raise Denied(401, None, "no access token", scope=wanted)
        claims = self.validator.access_token(token)
        cnf = claims.get("cnf", {})
        if set(cnf) - {"jkt", "kc-jkt-type"}:
            # Bound some way Harbor can't check (an mTLS x5t#S256, say):
            # refuse it rather than treat it as a bearer token.
            raise Denied(401, "invalid_token", "unsupported token binding")
        jkt = cnf.get("jkt")
        if jkt is None and scheme == "dpop":
            raise Denied(401, "invalid_token", "token is not DPoP-bound",
                         dpop=True)
        if jkt is not None:
            if scheme != "dpop" or "dpop" not in headers:
                raise Denied(401, "invalid_token",
                             "DPoP-bound token sent without a DPoP proof",
                             dpop=True)
            self.validator.dpop_proof(headers["dpop"], method,
                                      RESOURCE + path, token, jkt)
        return claims

    @staticmethod
    def who(claims):
        caller = claims.get("customer_id") or claims.get("azp")
        actor = claims.get("act", {}).get("client_id")
        return f"{caller} act={actor}" if actor else caller

    @staticmethod
    async def read_body(receive):
        chunks, more = [], True
        while more:
            message = await receive()
            chunks.append(message.get("body", b""))
            more = message.get("more_body", False)
        body = b"".join(chunks)
        sent = False

        async def replay():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return body, replay

    async def not_found(self, scope, send, order_id):
        """The one 404 for an order this caller can't see."""
        body = json.dumps({
            "type": "https://harbor.example/problems/order-not-found",
            "title": "Order not found", "status": 404,
            "detail": f"No order with id {order_id}.",
            "instance": scope["path"]}, indent=2).encode()
        await send({"type": "http.response.start", "status": 404,
                    "headers": [
                        (b"content-type", b"application/problem+json"),
                        (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    def challenge(denied: Denied) -> str:
        scheme = "DPoP" if denied.dpop else "Bearer"
        params = [f'resource_metadata="{PRM_URL}"']
        if denied.error:
            params.append(f'error="{denied.error}"')
            params.append(f'error_description="{denied.description}"')
        if denied.scope:
            params.append(f'scope="{denied.scope}"')
        if denied.dpop:
            params.append(f'algs="{" ".join(DPOP_ALGORITHMS)}"')
        return f"{scheme} " + ", ".join(params)

    async def deny(self, send, denied: Denied):
        body = json.dumps({
            "type": "https://harbor.example/problems/"
                    + ("unauthorized" if denied.status == 401
                       else "forbidden"),
            "title": "Unauthorized" if denied.status == 401 else "Forbidden",
            "status": denied.status,
            "detail": denied.description}, indent=2).encode()
        headers = [(b"content-type", b"application/problem+json"),
                   (b"content-length", str(len(body)).encode())]
        if denied.status == 401 or denied.error:
            headers.append((b"www-authenticate",
                            self.challenge(denied).encode()))
        await send({"type": "http.response.start", "status": denied.status,
                    "headers": headers})
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def json(send, status, value):
        body = json.dumps(value, indent=2).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length",
                                 str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    def log(scope, status, extra):
        print(f'"{scope["method"]} {scope["path"]} HTTP/'
              f'{scope["http_version"]}" {status} {extra}',
              file=sys.stderr, flush=True)


class Log:
    """One line per request that reached Chapter 3's app."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        status = {}

        async def capture(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)
        await self.inner(scope, receive, capture)
        caller = scope.get("state", {}).get("caller")
        if caller:
            TokenGate.log(scope, status.get("code"), f"caller={caller}")


def main():
    gate = TokenGate(Log(harbor_api.app))
    uvicorn.run(gate, host="127.0.0.1", port=8008, access_log=False,
                log_level="warning", server_header=False,
                headers=[("server", "harbor-ch08/1.0")])


if __name__ == "__main__":
    main()
