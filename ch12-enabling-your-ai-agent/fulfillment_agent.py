"""Harbor's fulfillment agent with the checks Chapter 10's demo lacked.

An A2A 1.0 server (JSON-RPC binding, official SDK) on 127.0.0.1:8124,
published through the gateway at http://127.0.0.1:9080/a2a/fulfillment/.
Its one skill here, report-damage, records damage to one book on a
delivered order and returns a damage_case artifact. Harbor's refunds API
accepts a refund only for a case on record.

Chapter 11 left three checks to build, and this agent has them:
  1. every request needs a token for this agent's own audience (Chapter
     8's checks), and the card declares the scheme;
  2. the order must belong to the customer the token was delegated by,
     and a foreign order is answered like a missing one;
  3. GetTask, CancelTask, and SubscribeToTask answer only the person who
     created the task; anyone else gets "task not found".
The carrier lookup is a stand-in on a short timer; no model runs here.

    uv run python fulfillment_agent.py
"""

import asyncio
import contextvars
import json
import logging
import secrets
import sys

import jwt
import uvicorn
from a2a.helpers import new_task_from_user_message, new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (AgentCapabilities, AgentCard, AgentInterface,
                       AgentSkill, HTTPAuthSecurityScheme, Part,
                       SecurityRequirement, SecurityScheme)
from google.protobuf import struct_pb2
from opentelemetry.propagate import extract
from opentelemetry.trace import SpanKind
from google.protobuf.json_format import MessageToDict
from starlette.applications import Starlette

import common
import issuer

URL = issuer.FULFILLMENT_AUD               # the public URL is the audience
STEP = 0.3
CALLER: contextvars.ContextVar = contextvars.ContextVar("caller",
                                                       default=None)
OWNERS: dict[str, str] = {}                # task id -> sub that created it

tracer = common.tracer("fulfillment")
log = logging.getLogger("fulfillment")
logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(message)s")
for noisy in ("httpx", "a2a", "uvicorn.access"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

CARD = AgentCard(
    name="Harbor Books fulfillment agent",
    description="Ships paid Harbor Books orders and handles what goes "
                "wrong after delivery.",
    version="1.1.0",
    default_input_modes=["application/json"],
    default_output_modes=["application/json"],
    capabilities=AgentCapabilities(streaming=True),
    supported_interfaces=[AgentInterface(
        protocol_binding="JSONRPC", url=URL, protocol_version="1.0")],
    security_schemes={"harbor": SecurityScheme(
        http_auth_security_scheme=HTTPAuthSecurityScheme(
            scheme="Bearer", bearer_format="JWT",
            description="A delegated token for this agent's URL, from "
                        "Harbor's authorization server."))},
    security_requirements=[SecurityRequirement(schemes={"harbor": {}})],
    skills=[AgentSkill(
        id="report-damage", name="Report damage",
        description="Record that one book on a delivered order arrived "
                    "damaged. Send a data part with order_id, book_id, "
                    "and a short description. Returns a damage_case "
                    "artifact with case_id, return_required, and "
                    "refund_eligible.",
        tags=["fulfillment", "damage", "returns"],
        input_modes=["application/json"],
        output_modes=["application/json"],
        examples=['{"order_id": "ord-5001", "book_id": "bk-1001", '
                  '"description": "torn cover"}'])],
)
# The description is the customer's words relayed by an agent: staff and
# other agents read it as untrusted text, never as instructions.


def request_data(message):
    for part in message.parts:
        if part.HasField("data"):
            return MessageToDict(part.data)
    return {}


def value(obj):
    v = struct_pb2.Value()
    v.struct_value.update(obj)
    return v


class DamageExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, queue: EventQueue):
        record = context.current_task or new_task_from_user_message(
            context.message)
        if not context.current_task:
            await queue.enqueue_event(record)
        task = TaskUpdater(queue, record.id, record.context_id)
        claims = CALLER.get()
        ask = request_data(context.message)
        order_id, book_id = ask.get("order_id", ""), ask.get("book_id", "")
        order = common.ORDERS.get(order_id)
        if claims is None:
            await task.reject(new_text_message("No caller."))
            return
        OWNERS[record.id] = claims["sub"]
        log.info("task %s: report-damage %s %s for %s via %s",
                 record.id[:8], order_id, book_id, claims["sub"],
                 common.actors(claims)[0])
        if order is None or order["customer_id"] != claims["customer_id"]:
            await task.reject(new_text_message(f"No order {order_id}."))
            return
        if order["status"] != "delivered" or book_id not in {
                ln["book_id"] for ln in order["lines"]}:
            await task.reject(new_text_message(
                f"{book_id} isn't on a delivered order {order_id}."))
            return
        await task.start_work(new_text_message(
            f"Checking the carrier's delivery record for {order_id}"))
        await asyncio.sleep(STEP)
        found = common.cases()
        case_id = next((k for k, v in found.items()
                        if (v["order_id"], v["book_id"]) ==
                        (order_id, book_id)), None) or \
            f"dmg-{secrets.token_hex(4)}"           # random, not guessable
        found[case_id] = {"order_id": order_id, "book_id": book_id,
                          "description": ask.get("description", "")[:200],
                          "description_trust": "untrusted",
                          "reported_by": claims["sub"],
                          "via": common.actors(claims)}
        common.STATE.mkdir(exist_ok=True)
        common.CASES.write_text(json.dumps(found, indent=2))
        await task.add_artifact([Part(data=value({
            "case_id": case_id, "order_id": order_id, "book_id": book_id,
            "return_required": False, "refund_eligible": True}))],
            name="damage_case")
        await task.complete(new_text_message(
            f"Damage recorded as {case_id}; no return needed."))
        log.info("task %s: completed %s", record.id[:8], case_id)

    async def cancel(self, context: RequestContext, queue: EventQueue):
        await TaskUpdater(queue, context.task_id, context.context_id).cancel()


class Gate:
    """Token, then per-task ownership, before the SDK sees the request."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.inner(scope, receive, send)
        chunks, more = [], True
        while more:
            msg = await receive()
            chunks.append(msg.get("body", b""))
            more = msg.get("more_body", False)
        body = b"".join(chunks)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope["headers"]}
        auth = headers.get("authorization", "")
        try:
            claims = common.check_token(auth[7:], URL) \
                if auth.startswith("Bearer ") else None
        except jwt.PyJWTError as err:
            log.info("token refused: %s", type(err).__name__)
            claims = None
        if claims is None or not claims.get("act"):
            return await plain(send, 401, b"", [(
                b"www-authenticate", b'Bearer realm="harbor-fulfillment"')])
        try:
            call = json.loads(body)
        except ValueError:
            call = {}
        task_id = (call.get("params") or {}).get("id")
        if call.get("method") in ("GetTask", "CancelTask",
                                  "SubscribeToTask") and \
                OWNERS.get(task_id) != claims["sub"]:
            log.info("%s %s refused for %s", call.get("method"),
                     str(task_id)[:8], claims["sub"])
            error = {"jsonrpc": "2.0", "id": call.get("id"),
                     "error": {"code": -32001, "message": "Task not found"}}
            return await plain(send, 200, json.dumps(error).encode(),
                               [(b"content-type", b"application/json")])
        CALLER.set(claims)
        sent = False

        async def replay():
            nonlocal sent
            if sent:
                return await receive()
            sent = True
            return {"type": "http.request", "body": body,
                    "more_body": False}
        method = call.get("method", "-")
        with tracer.start_as_current_span(
                method, kind=SpanKind.SERVER, context=extract(headers),
                attributes={"a2a.method.name": method,
                            "a2a.protocol.version": "1.0",
                            "enduser.id": claims["sub"]}):
            await self.inner(scope, replay, send)


async def plain(send, status, body, headers):
    await send({"type": "http.response.start", "status": status,
                "headers": headers + [(b"content-length",
                                       str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def app():
    handler = DefaultRequestHandler(
        agent_executor=DamageExecutor(), task_store=InMemoryTaskStore(),
        agent_card=CARD)
    routes = create_agent_card_routes(CARD) + \
        create_jsonrpc_routes(handler, "/")
    return Gate(Starlette(routes=routes))


if __name__ == "__main__":
    uvicorn.run(app(), host="127.0.0.1", port=8124, log_level="warning")
