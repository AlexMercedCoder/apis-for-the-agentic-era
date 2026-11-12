"""Harbor's support MCP server: two task tools, curated by hand.

Chapter 10's server generated one tool per REST operation. A support
agent needs fewer, larger tools shaped like its jobs:

  order_overview       runs Chapter 5's OrderScreen trusted document on the
                       GraphQL server and returns only what the task needs
                       (no reviews: they're untrusted text, Chapter 11)
  refund_damaged_copy  calls the refunds API through the gateway with a
                       token exchanged for that API (RFC 8693), so the API
                       sees Rosa, the assistant, and this server

Tokens come from this chapter's test issuer for the resource
http://127.0.0.1:9080/mcp. refund_damaged_copy needs orders:refund, which
only Rosa's approval mints; a read-only token gets Chapter 10's 403 step-up
challenge. The Idempotency-Key is the request_id the host put in the
approval's authorization_details, so one approval can refund once.

    uv run python support_mcp.py          # Streamable HTTP, 127.0.0.1:8122
"""

import json
import logging
import sys

import httpx
import jsonschema
import jwt
import mcp_types as types
import uvicorn
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.routes import create_protected_resource_routes
from mcp.server.auth.settings import AuthSettings
from mcp.server.lowlevel import Server
from mcp.shared.inbound import decode_header_value
from opentelemetry import propagate
from opentelemetry.trace import SpanKind
from urllib.parse import quote

import common
import issuer
import reliable

RESOURCE = issuer.MCP_AUD
GATEWAY = issuer.GATEWAY
GRAPHQL = "http://127.0.0.1:8005/graphql"
TRUSTED = json.loads((common.HERE.parent / "ch05-graphql-apis" /
                      "trusted-documents.json").read_text())
ORDER_SCREEN = next(k for k, v in TRUSTED.items()      # Chapter 5's
                    if "query OrderScreen(" in v)       # trusted document
PRM_URL = GATEWAY + "/.well-known/oauth-protected-resource/mcp"
SECRET = "not-a-real-secret-support-mcp"  # placeholder client secret

log = logging.getLogger("support-mcp")
logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(message)s")
for noisy in ("httpx", "mcp", "uvicorn.access"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
tracer = common.tracer("support-mcp")

ORDER_ID = {"type": "string", "pattern": "^ord-[0-9]{4}$",
            "description": "Harbor order ID, such as ord-5001"}
BOOK_ID = {"type": "string", "pattern": "^bk-[0-9]{4}$",
           "description": "Harbor book ID from the order's lines"}

TOOLS = [
    types.Tool(
        name="order_overview",
        description=(
            "Look up one of the signed-in customer's orders: status, "
            "total, and each line's book_id, title, quantity, and price "
            "in cents. Call it first for any question about an order. "
            "Read-only."),
        input_schema={"type": "object", "properties": {"order_id": ORDER_ID},
                      "required": ["order_id"],
                      "additionalProperties": False},
        output_schema={"type": "object", "properties": {
            "order_id": {"type": "string"},
            "status": {"type": "string"},
            "total_cents": {"type": "integer"},
            "lines": {"type": "array", "items": {"type": "object"}}},
            "required": ["order_id", "status", "lines"]},
        annotations=types.ToolAnnotations(read_only_hint=True,
                                          open_world_hint=False)),
    types.Tool(
        name="refund_damaged_copy",
        description=(
            "Refund the price paid for one book that arrived damaged on "
            "a delivered order. Needs the damage_case that Harbor's "
            "fulfillment agent returns from report-damage, and the "
            "customer's approval, which the host asks for. Each book on "
            "an order is refunded at most once. Changes data: issues a "
            "refund, confirmed later by Harbor."),
        input_schema={"type": "object", "properties": {
            "order_id": ORDER_ID, "book_id": BOOK_ID,
            "damage_case": {"type": "string",
                            "pattern": "^dmg-[0-9a-f]{8}$",
                            "description": "case_id from report-damage"}},
            "required": ["order_id", "book_id", "damage_case"],
            "additionalProperties": False},
        annotations=types.ToolAnnotations(
            read_only_hint=False, destructive_hint=False,
            idempotent_hint=True, open_world_hint=False)),
]
SCHEMAS = {t.name: t.input_schema for t in TOOLS}


Strict = common.Strict
BREAKER = reliable.Breaker()             # for the refunds upstream


class Verifier:
    async def verify_token(self, token):
        try:
            claims = common.check_token(token, RESOURCE)
        except jwt.PyJWTError as err:
            log.info("token refused: %s", type(err).__name__)
            return None
        return AccessToken(token=token, client_id=claims["azp"],
                           scopes=claims.get("scope", "").split(),
                           expires_at=claims["exp"], resource=RESOURCE,
                           subject=claims["sub"], claims=claims)


def tool_error(text, data=None):
    return types.CallToolResult(
        content=[types.TextContent(text=text)],
        structured_content=data, is_error=True)


def done(data):
    return types.CallToolResult(
        content=[types.TextContent(text=json.dumps(data))],
        structured_content=data)


async def order_overview(args, claims):
    order = common.ORDERS.get(args["order_id"])
    if order is None or order["customer_id"] != claims["customer_id"]:
        return tool_error(f"No order with id {args['order_id']}.")
    async with httpx.AsyncClient(timeout=5) as http:
        resp = await http.post(GRAPHQL, json={
            "documentId": ORDER_SCREEN, "operationName": "OrderScreen",
            "variables": {"orderId": order["id"]}})
    screen = resp.json()["data"]["order"]
    lines = [{"book_id": rec["book_id"], "title": gql["book"]["title"],
              "quantity": gql["quantity"],
              "price_cents": rec["unit_price_cents"]}
             for rec, gql in zip(order["lines"], screen["lines"])]
    return done({"order_id": screen["id"],
                 "status": screen["status"].lower(),
                 "total_cents": screen["totalCents"], "lines": lines})


async def exchange(token):
    async with httpx.AsyncClient(timeout=5) as http:
        resp = await http.post(f"{issuer.ISSUER}/token", data={
            "grant_type": issuer.EXCHANGE, "client_id": "harbor-support-mcp",
            "client_secret": SECRET, "subject_token": token,
            "subject_token_type":
                "urn:ietf:params:oauth:token-type:access_token",
            "audience": issuer.REFUNDS_AUD, "scope": "orders:refund"})
    resp.raise_for_status()
    return resp.json()["access_token"]


async def refund_damaged_copy(args, claims, raw_token):
    if "orders:refund" not in claims.get("scope", "").split():
        return tool_error("This needs the customer's approval for this "
                          "refund; the host asks for it.")
    approval = next((d for d in claims.get("authorization_details", [])
                     if d.get("order_id") == args["order_id"]
                     and d.get("book_id") == args["book_id"]), None)
    if approval is None:
        return tool_error("The customer approved a different refund; "
                          "ask again for this one.")
    key = f'"{approval["request_id"]}"'
    token = await exchange(raw_token)
    url = (f"{GATEWAY}/orders/{quote(args['order_id'], safe='')}"
           f"/refunds")
    body = {"book_id": args["book_id"], "damage_case": args["damage_case"]}

    async def send(timeout):
        headers = {"Authorization": f"Bearer {token}",
                   "Idempotency-Key": key}
        propagate.inject(headers)
        async with httpx.AsyncClient(timeout=timeout) as http:
            return await http.post(url, json=body, headers=headers)

    try:
        resp = await reliable.guarded(
            BREAKER, send, deadline=8.0, per_try=1.0,
            log=lambda m: log.info("refund %s", m))
    except reliable.Open:
        return tool_error("Harbor's refunds API is failing; try again in "
                          "a minute.")
    except (reliable.GaveUp, httpx.HTTPError) as err:
        log.info("refund gave up: %s", err)
        return tool_error("Harbor's refunds API didn't answer, so the "
                          "refund isn't confirmed. Retrying with the same "
                          "approval is safe and can't refund twice.")
    replay = resp.headers.get("idempotent-replayed") == "true"
    log.info("refund %s %s -> %s%s", args["order_id"], args["book_id"],
             resp.status_code, " replayed" if replay else "")
    data = resp.json()
    if resp.status_code >= 400:
        text = data.get("detail", "Refused.")
        if data.get("hint"):
            text += " " + data["hint"]
        return tool_error(text, {"problem": data})
    return done(data)


async def list_tools(ctx, params):
    return types.ListToolsResult(tools=TOOLS)


async def call_tool(ctx, params):
    name, args = params.name, dict(params.arguments or {})
    if name not in SCHEMAS:
        raise ValueError(f"unknown tool {name}")
    token = get_access_token()
    claims = token.claims
    meta = dict(params.meta or {})
    parent = propagate.extract({k: v for k, v in meta.items()
                                if k in ("traceparent", "tracestate")})
    with tracer.start_as_current_span(
            f"tools/call {name}", kind=SpanKind.SERVER, context=parent,
            attributes={"mcp.method.name": "tools/call",
                        "gen_ai.tool.name": name,
                        "gen_ai.operation.name": "execute_tool",
                        "mcp.protocol.version": "2026-07-28"}) as span:
        error = jsonschema.exceptions.best_match(
            Strict(SCHEMAS[name]).iter_errors(args))
        if error is not None:
            log.info("tools/call %s caller=%s refused: %s", name,
                     claims["customer_id"], error.message[:60])
            return tool_error(f"Invalid arguments: {error.message}")
        try:
            if name == "order_overview":
                result = await order_overview(args, claims)
            else:
                result = await refund_damaged_copy(args, claims, token.token)
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as err:
            log.info("tools/call %s upstream error: %s", name,
                     type(err).__name__)
            result = tool_error("Harbor couldn't answer that request.")
        if result.is_error:
            span.set_attribute("error.type", "tool_error")
        log.info("tools/call %s caller=%s act=%s%s", name,
                 claims["customer_id"], ",".join(common.actors(claims)),
                 " error" if result.is_error else "")
        return result


server = Server("harbor-support", version="1.0.0",
                instructions="Harbor Books customer support: look up a "
                             "signed-in customer's order and refund a "
                             "damaged copy with their approval.",
                on_list_tools=list_tools, on_call_tool=call_tool)


class StepUp:
    """Chapter 10's 403 step-up, for a refund with a read-only token."""

    def __init__(self, inner, verifier):
        self.inner, self.verifier = inner, verifier

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/mcp":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope["headers"]}
            auth = headers.get("authorization", "")
            tool = decode_header_value(headers.get("mcp-name"))
            if (headers.get("mcp-method") == "tools/call"
                    and tool == "refund_damaged_copy"
                    and auth.startswith("Bearer ")):
                token = await self.verifier.verify_token(auth[7:])
                if token and "orders:refund" not in token.scopes:
                    log.info("tools/call %s caller=%s 403 needs "
                             "orders:refund", tool,
                             token.claims["customer_id"])
                    challenge = ('Bearer error="insufficient_scope", '
                                 'scope="orders:read orders:refund", '
                                 f'resource_metadata="{PRM_URL}"')
                    await send({"type": "http.response.start", "status": 403,
                                "headers": [(b"www-authenticate",
                                             challenge.encode()),
                                            (b"content-length", b"0")]})
                    await send({"type": "http.response.body", "body": b""})
                    return
        await self.inner(scope, receive, send)


def app():
    verifier = Verifier()
    asgi = server.streamable_http_app(
        streamable_http_path="/mcp", json_response=True,
        auth=AuthSettings(issuer_url=issuer.ISSUER,
                          resource_server_url=RESOURCE,
                          required_scopes=["orders:read"],
                          validate_token_resource=False),
        token_verifier=verifier)
    for route in reversed(create_protected_resource_routes(
            resource_url=RESOURCE, authorization_servers=[issuer.ISSUER],
            scopes_supported=["orders:read"],
            resource_name="Harbor Books support MCP server")):
        asgi.router.routes.insert(0, route)
    return StepUp(asgi, verifier)


if __name__ == "__main__":
    uvicorn.run(app(), host="127.0.0.1", port=8122, log_level="warning")
