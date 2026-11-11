"""Chapter 3's Harbor API, twice, sharing one in-memory store.

  * 127.0.0.1:8003 is Chapter 3's app with Chapter 3's logger, unchanged.
    Harbor's MCP server calls it on the loopback address, as in Chapter 10.
  * 127.0.0.1:8111 is the same app behind a token gate, the upstream the
    gateway sends /books and /orders to. The gate checks the token again
    (the gateway's jwt-auth plugin checks signature and expiry but not
    issuer or audience), checks scopes, and enforces ownership: a token
    sees only its own customer's orders, and every other order gets the
    same 404 a missing one gets. Its log line is JSON, with the raw,
    still-encoded path, so a request can't forge a second log line.

At startup it adds planted-review.json (rev-004, a review carrying an
instruction aimed at AI assistants) to the reviews Chapter 3 loaded.

    uv run python harbor_backend.py
"""

import asyncio
import json
import sys
import time
from pathlib import Path

import jwt
import uvicorn

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "ch03-rest-apis"))
import harbor_api  # noqa: E402  Chapter 3's app, unchanged

ISSUER = "http://127.0.0.1:8110"
AUDIENCE = "http://127.0.0.1:9080"
JWKS = jwt.PyJWKClient(ISSUER + "/jwks")

harbor_api.store.reviews.append(
    json.loads((HERE / "planted-review.json").read_text()))


def needs(method, path):
    if not path.startswith("/orders"):
        return None                                  # the catalog
    return "orders:read" if method == "GET" else "orders:write"


class Gate:
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        started, status, claims = time.time(), {}, {}

        async def capture(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            claims.update(self.check(scope))
            await self.owner_first(scope, claims)
            if scope["method"] == "POST" and scope["path"] == "/orders":
                receive = await self.same_customer(receive, claims)
            await self.inner(scope, receive, capture)
        except PermissionError as denied:
            code, text = denied.args
            await self.reply(capture, code, text)
        self.log(scope, status.get("code"), claims, started)

    def check(self, scope):
        headers = dict(scope["headers"])
        auth = headers.get(b"authorization", b"").decode("latin-1")
        if not auth.startswith("Bearer "):
            raise PermissionError(401, "token required")
        token = auth[7:]
        try:
            if jwt.get_unverified_header(token).get("typ") != "at+jwt":
                raise PermissionError(401, "not an access token")
            key = JWKS.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"],
                                issuer=ISSUER, audience=AUDIENCE)
        except jwt.PyJWTError:
            raise PermissionError(401, "invalid token") from None
        scope_needed = needs(scope["method"], scope["path"])
        if scope_needed and scope_needed not in claims["scope"].split():
            raise PermissionError(403, f"needs {scope_needed}")
        return claims

    async def owner_first(self, scope, claims):
        parts = scope["path"].split("/")
        if len(parts) < 3 or parts[1] != "orders" or not parts[2]:
            return
        order = harbor_api.store.orders.get(parts[2])
        if order is None or order["customer_id"] != claims.get(
                "customer_id"):
            raise PermissionError(404, "no such order")

    @staticmethod
    async def same_customer(receive, claims):
        chunks, more = [], True
        while more:
            message = await receive()
            chunks.append(message.get("body", b""))
            more = message.get("more_body", False)
        body = b"".join(chunks)
        try:
            customer = json.loads(body).get("customer_id")
        except (ValueError, AttributeError):
            customer = None
        if customer != claims.get("customer_id"):
            raise PermissionError(403, "customer_id is not the token's")

        async def replay():
            return {"type": "http.request", "body": body,
                    "more_body": False}
        return replay

    @staticmethod
    async def reply(send, code, text):
        body = json.dumps({"type": "about:blank", "status": code,
                           "title": text}).encode()
        await send({"type": "http.response.start", "status": code,
                    "headers": [(b"content-type",
                                 b"application/problem+json"),
                                (b"content-length",
                                 str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    def log(scope, code, claims, started):
        raw = scope.get("raw_path", b"").decode("latin-1")
        print(json.dumps({"method": scope["method"], "path": raw,
                          "status": code, "sub": claims.get("sub"),
                          "azp": claims.get("azp"),
                          "ms": round((time.time() - started) * 1000)}),
              file=sys.stderr, flush=True)


async def main():
    plain = uvicorn.Server(uvicorn.Config(
        harbor_api.LogMiddleware(harbor_api.app), host="127.0.0.1",
        port=8003, access_log=False, log_level="warning"))
    gated = uvicorn.Server(uvicorn.Config(
        Gate(harbor_api.app), host="127.0.0.1", port=8111,
        access_log=False, log_level="warning"))
    await asyncio.gather(plain.serve(), gated.serve())


if __name__ == "__main__":
    asyncio.run(main())
