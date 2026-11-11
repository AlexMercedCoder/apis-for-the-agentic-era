"""Harbor's MCP server, generated from Chapter 3's pinned OpenAPI document.

Every operation in ../ch03-rest-apis/openapi.json becomes an MCP tool: its
operationId is the tool's name, its summary and description the tool's
description, its parameters and request body the tool's input schema, and
its HTTP method the tool's annotations. The server is an OAuth 2.0
resource server (Chapter 8): it publishes Protected Resource Metadata,
accepts only Keycloak tokens issued for this MCP endpoint, and fills the
caller's customer ID from the token instead of letting the model send one.
It calls Chapter 3's REST API on 127.0.0.1:8003 and never forwards the
caller's token there.

    uv run python harbor_mcp.py          # Streamable HTTP on 127.0.0.1:8010
"""

import base64
import copy
import hashlib
import hmac
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path

import httpx
import jwt
import mcp_types as types
import uvicorn
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.routes import create_protected_resource_routes
from mcp.server.auth.settings import AuthSettings
from mcp.server.lowlevel import Server

ISSUER = "http://127.0.0.1:8080/realms/harbor"
RESOURCE = "http://127.0.0.1:8010/mcp"
REST = "http://127.0.0.1:8003"
SPEC = json.loads((Path(__file__).parent.parent / "ch03-rest-apis" /
                   "openapi.json").read_text())
# Seals requestState for confirmations. A placeholder; load a real key from
# a secret store and rotate it.
STATE_KEY = os.environ.get("HARBOR_STATE_KEY",
                           "not-a-real-key-for-request-state").encode()
FROM_TOKEN = {"customer_id"}          # filled from the token, never the model
HOST_HEADERS = {"Idempotency-Key"}    # filled by this server, never the model
OWNED = {"get_order_status", "get_cancellation", "cancel_order"}

log = logging.getLogger("harbor-mcp")
logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(message)s")
for noisy in ("httpx", "mcp", "uvicorn.access"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


# -- tools generated from the OpenAPI document --------------------------------

def resolve(node):
    """Inline every local $ref so each tool has one self-contained schema."""
    if isinstance(node, dict):
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            return resolve(copy.deepcopy(SPEC["components"]["schemas"][name]))
        return {k: resolve(v) for k, v in node.items() if k != "title"}
    if isinstance(node, list):
        return [resolve(v) for v in node]
    return node


def annotations(method):
    """RFC 9110's method promises, as MCP tool annotation hints."""
    if method == "get":
        return types.ToolAnnotations(read_only_hint=True,
                                     open_world_hint=False)
    return types.ToolAnnotations(read_only_hint=False,
                                 destructive_hint=method in ("put", "delete"),
                                 idempotent_hint=method in ("put", "delete"),
                                 open_world_hint=False)


def build_tools():
    tools, routes = [], {}
    for path, item in SPEC["paths"].items():
        for method, op in item.items():
            props, required, where = {}, [], {}
            for param in op.get("parameters", []):
                if param["name"] in HOST_HEADERS:
                    continue
                props[param["name"]] = resolve(param["schema"])
                if "description" in param:
                    props[param["name"]]["description"] = param["description"]
                if param.get("required"):
                    required.append(param["name"])
                where[param["name"]] = param["in"]
            body = op.get("requestBody", {}).get("content", {}).get(
                "application/json", {}).get("schema")
            if body:
                body = resolve(body)
                for name, schema in body["properties"].items():
                    if name in FROM_TOKEN:
                        continue
                    props[name] = schema
                    where[name] = "body"
                required += [n for n in body.get("required", [])
                             if n not in FROM_TOKEN]
            name = op["operationId"]
            text = op.get("summary", "")
            if op.get("description"):
                text += ". " + op["description"]
            tools.append(types.Tool(
                name=name, description=text,
                input_schema={"type": "object", "properties": props,
                              "required": required,
                              "additionalProperties": False},
                annotations=annotations(method)))
            routes[name] = (method, path, where, bool(body))
    return tools, routes


TOOLS, ROUTES = build_tools()


# -- tokens ------------------------------------------------------------------

class KeycloakVerifier:
    """Chapter 8's checks: pinned RS256, at+jwt, issuer, this audience."""

    def __init__(self):
        meta = httpx.get(f"{ISSUER}/.well-known/openid-configuration").json()
        self.keys = jwt.PyJWKClient(meta["jwks_uri"])

    async def verify_token(self, token):
        try:
            if jwt.get_unverified_header(token).get("typ") != "at+jwt":
                return None
            key = self.keys.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"],
                                audience=RESOURCE, issuer=ISSUER)
        except jwt.PyJWTError as err:
            log.info("token refused: %s", type(err).__name__)
            return None
        return AccessToken(token=token, client_id=claims["azp"],
                           scopes=claims.get("scope", "").split(),
                           expires_at=claims["exp"], resource=RESOURCE,
                           subject=claims["sub"], claims=claims)


def caller():
    token = get_access_token()
    return token.claims["customer_id"], set(token.scopes), token.subject


# -- confirmations: multi round-trip requests ---------------------------------

def seal(payload):
    raw = json.dumps(payload, sort_keys=True).encode()
    tag = hmac.new(STATE_KEY, raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw + tag).decode()


def unseal(state):
    blob = base64.urlsafe_b64decode(state.encode())
    raw, tag = blob[:-32], blob[-32:]
    if not hmac.compare_digest(
            tag, hmac.new(STATE_KEY, raw, hashlib.sha256).digest()):
        raise ValueError("requestState failed its integrity check")
    return json.loads(raw)


def digest(name, args):
    text = json.dumps([name, args], sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()


async def confirm(name, args, subject, summary, params):
    """Ask the person first; return (approved, idempotency key) or a result.

    The first call returns InputRequiredResult with a form-mode elicitation
    and a sealed requestState that binds the person, the exact arguments,
    an expiry, and the Idempotency-Key minted now, so every retry of this
    confirmation reuses one key.
    """
    if params.request_state is None:
        state = seal({"sub": subject, "call": digest(name, args),
                      "exp": int(time.time()) + 300,
                      "key": f'"{uuid.uuid4()}"'})
        ask = types.ElicitRequest(params=types.ElicitRequestFormParams(
            message=summary,
            requested_schema={"type": "object", "properties": {
                "confirm": {"type": "boolean",
                            "title": "Go ahead?"}},
                "required": ["confirm"]}))
        return types.InputRequiredResult(
            input_requests={"confirm": ask}, request_state=state)
    state = unseal(params.request_state)
    if (state["sub"] != subject or state["call"] != digest(name, args)
            or state["exp"] < time.time()):
        raise ValueError("requestState is for another person, call, or time")
    answer = (params.input_responses or {}).get("confirm")
    approved = (isinstance(answer, types.ElicitResult)
                and answer.action == "accept"
                and (answer.content or {}).get("confirm") is True)
    return approved, state["key"]


# -- the handlers --------------------------------------------------------------

def tool_error(text):
    return types.CallToolResult(
        content=[types.TextContent(text=text)], is_error=True)


async def rest(method, path, **kw):
    async with httpx.AsyncClient(base_url=REST, timeout=10) as http:
        return await http.request(method, path, **kw)


async def summarize(name, args, customer):
    if name == "cancel_order":
        return f"Cancel {args['order_id']} and refund it?"
    total, titles = 0, []
    for line in args["lines"]:
        book = (await rest("GET", f"/books/{line['book_id']}")).json()
        total += book.get("price_cents", 0) * line["quantity"]
        titles.append(f"{line['quantity']} x {book.get('title', '?')}")
    return f"Order {', '.join(titles)} for {total} cents?"


async def list_tools(ctx, params):
    return types.ListToolsResult(tools=TOOLS)


async def call_tool(ctx, params):
    name, args = params.name, dict(params.arguments or {})
    if name not in ROUTES:
        raise ValueError(f"unknown tool {name}")
    customer, scopes, subject = caller()
    method, path, where, has_body = ROUTES[name]
    needed = "orders:read" if method == "get" else "orders:write"
    if needed not in scopes:
        log.info("tools/call %s caller=%s refused: needs %s",
                 name, customer, needed)
        return tool_error(f"This token lacks {needed}; ask the person to "
                          f"grant it.")
    if name in OWNED:
        order = await rest("GET", f"/orders/{args.get('order_id', '')}")
        if order.status_code != 200 or \
                order.json().get("customer_id") != customer:
            log.info("tools/call %s caller=%s %s not theirs", name,
                     customer, args.get("order_id"))
            return tool_error(f"No order with id {args.get('order_id')}.")
    headers = {}
    if method != "get":
        outcome = await confirm(name, args, subject,
                                await summarize(name, args, customer),
                                params)
        if isinstance(outcome, types.InputRequiredResult):
            log.info("tools/call %s caller=%s input_required", name,
                     customer)
            return outcome
        approved, key = outcome
        if not approved:
            log.info("tools/call %s caller=%s declined", name, customer)
            return tool_error("The person declined; nothing was changed.")
        if name == "place_order":
            headers["Idempotency-Key"] = key
    url = path.format(**{k: args.pop(k) for k in list(args)
                         if where.get(k) == "path"})
    query = {k: args.pop(k) for k in list(args) if where.get(k) == "query"}
    body = None
    if has_body:
        body = dict(args)
        if name == "place_order":
            body["customer_id"] = customer
    resp = await rest(method.upper(), url, params=query, json=body,
                      headers=headers)
    log.info("tools/call %s caller=%s -> %s %s %s", name, customer,
             method.upper(), url, resp.status_code)
    data = resp.json() if resp.content else {}
    return types.CallToolResult(
        content=[types.TextContent(text=json.dumps(data))],
        structured_content=data, is_error=resp.status_code >= 400)


BOOK = types.ResourceTemplate(
    uri_template="harbor://books/{book_id}", name="book",
    description="One Harbor book as JSON, from GET /books/{book_id}.",
    mime_type="application/json")


async def list_resource_templates(ctx, params):
    return types.ListResourceTemplatesResult(resource_templates=[BOOK])


async def list_resources(ctx, params):
    return types.ListResourcesResult(resources=[])


async def read_resource(ctx, params):
    uri = str(params.uri)
    if not uri.startswith("harbor://books/"):
        raise ValueError(f"unknown resource {uri}")
    resp = await rest("GET", f"/books/{uri.rsplit('/', 1)[1]}")
    if resp.status_code != 200:
        raise ValueError(f"no book at {uri}")
    return types.ReadResourceResult(contents=[types.TextResourceContents(
        uri=uri, mime_type="application/json", text=resp.text)])


PROMPT = types.Prompt(
    name="where_is_my_order",
    description="Answer a customer's question about one of their orders.",
    arguments=[types.PromptArgument(name="order_id", required=True)])


async def list_prompts(ctx, params):
    return types.ListPromptsResult(prompts=[PROMPT])


async def get_prompt(ctx, params):
    order_id = (params.arguments or {}).get("order_id", "")
    text = (f"The customer asks where order {order_id} is. Call "
            f"get_order_status with that ID, then answer in two sentences: "
            f"the status, and what happens next. Never guess a status.")
    return types.GetPromptResult(
        description=PROMPT.description,
        messages=[types.PromptMessage(role="user",
                                      content=types.TextContent(text=text))])


server = Server(
    "harbor-books", version="1.0.0",
    instructions="Harbor Books: the catalog and the signed-in customer's "
                 "orders. Tools that change data ask the person first.",
    on_list_tools=list_tools, on_call_tool=call_tool,
    on_list_resources=list_resources,
    on_list_resource_templates=list_resource_templates,
    on_read_resource=read_resource,
    on_list_prompts=list_prompts, on_get_prompt=get_prompt)


def app():
    asgi = server.streamable_http_app(
        streamable_http_path="/mcp", json_response=True,
        auth=AuthSettings(issuer_url=ISSUER, resource_server_url=RESOURCE,
                          required_scopes=["orders:read"],
                          validate_token_resource=False),
        token_verifier=KeycloakVerifier())
    # Publish both scopes in the Protected Resource Metadata (RFC 9728),
    # ahead of the SDK's default document, which lists only the required one.
    for route in reversed(create_protected_resource_routes(
            resource_url=RESOURCE, authorization_servers=[ISSUER],
            scopes_supported=["orders:read", "orders:write"],
            resource_name="Harbor Books MCP server")):
        asgi.router.routes.insert(0, route)
    return asgi


if __name__ == "__main__":
    uvicorn.run(app(), host="127.0.0.1", port=8010, log_level="warning")
