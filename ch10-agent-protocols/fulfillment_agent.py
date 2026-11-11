"""Harbor's fulfillment agent, an A2A 1.0 server built on the official SDK.

It publishes an Agent Card at /.well-known/agent-card.json and serves the
JSON-RPC binding at http://127.0.0.1:8011/. One skill, arrange-delivery,
takes an order ID (and an optional gift-wrap flag) and moves the task
through working to completed, with a shipment artifact. The packing steps
are a stand-in on a short timer; no model runs here, and an A2A agent
doesn't need one to speak the protocol.

Push notifications go only to URLs on an allow-list (Chapter 6's lesson on
senders that post wherever a subscriber says). The allow-list stands in for
the SDK's validate_push_notification_url, which refuses loopback, private,
and link-local addresses and would refuse this demo's 127.0.0.1 receiver;
an agent with many customers uses that validator, not a list.

Not production-ready on purpose, and the chapter says so: the card declares
no securitySchemes, and the agent doesn't check that the caller may act
for the order's customer or limit GetTask to the task's creator. A real
fulfillment agent requires a token for its own audience (Chapter 8's token
exchange), checks the order's customer before touching the order, and
returns tasks only to the caller that created them.

    uv run python fulfillment_agent.py
"""

import asyncio
import logging
import sys

import httpx
import uvicorn
from a2a.helpers import new_task_from_user_message, new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import (BasePushNotificationSender,
                              InMemoryPushNotificationConfigStore,
                              InMemoryTaskStore, TaskUpdater)
from a2a.types import (AgentCapabilities, AgentCard, AgentInterface,
                       AgentSkill, Part, TaskState)
from google.protobuf.json_format import MessageToDict
from starlette.applications import Starlette

URL = "http://127.0.0.1:8011/"
PUSH_ALLOWED = {"http://127.0.0.1:8012/a2a/push"}   # demo receivers only
STEP = 0.4                                          # stand-in packing time

log = logging.getLogger("fulfillment")
logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(message)s")
for noisy in ("httpx", "a2a", "uvicorn.access"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

CARD = AgentCard(
    name="Harbor Books fulfillment agent",
    description="Arranges delivery of paid Harbor Books orders: picks, "
                "packs, optionally gift-wraps, and books the carrier.",
    version="1.0.0",
    default_input_modes=["text/plain", "application/json"],
    default_output_modes=["application/json"],
    capabilities=AgentCapabilities(streaming=True, push_notifications=True),
    supported_interfaces=[AgentInterface(
        protocol_binding="JSONRPC", url=URL, protocol_version="1.0")],
    skills=[AgentSkill(
        id="arrange-delivery", name="Arrange delivery",
        description="Ship one paid order. Send a data part with order_id "
                    "and, optionally, gift_wrap: true.",
        tags=["fulfillment", "shipping"],
        input_modes=["application/json"],
        output_modes=["application/json"],
        examples=['{"order_id": "ord-5003", "gift_wrap": true}'])],
)

PAID = {"ord-5003"}          # paid orders waiting to ship, from harbor-data


def request_data(message):
    for part in message.parts:
        if part.HasField("data"):
            return MessageToDict(part.data)
    return {}


class FulfillmentExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, queue: EventQueue):
        record = context.current_task or new_task_from_user_message(
            context.message)
        if not context.current_task:
            await queue.enqueue_event(record)       # state: submitted
        task = TaskUpdater(queue, record.id, record.context_id)
        ask = request_data(context.message)
        order_id = ask.get("order_id", "")
        log.info("task %s: arrange-delivery %s", record.id[:8], ask)
        if order_id not in PAID:
            await task.reject(new_text_message(
                f"{order_id or 'No order'} isn't a paid order waiting to "
                f"ship."))
            return
        PAID.discard(order_id)              # a repeated request can't ship twice
        await task.start_work(new_text_message(f"Picking {order_id}"))
        await asyncio.sleep(STEP)
        packed = "Packed and gift-wrapped" if ask.get("gift_wrap") \
            else "Packed"
        await task.update_status(TaskState.TASK_STATE_WORKING,
                                 new_text_message(f"{packed} {order_id}"))
        await asyncio.sleep(STEP)
        await task.add_artifact(
            [Part(data=_value({"order_id": order_id, "carrier": "demo-post",
                               "tracking": f"DP{order_id[-4:]}0042",
                               "gift_wrap": bool(ask.get("gift_wrap"))}))],
            name="shipment")
        await task.complete(new_text_message(f"{order_id} handed to the "
                                             f"carrier"))
        log.info("task %s: completed", record.id[:8])

    async def cancel(self, context: RequestContext, queue: EventQueue):
        await TaskUpdater(queue, context.task_id, context.context_id).cancel()


def _value(obj):
    from google.protobuf import struct_pb2
    value = struct_pb2.Value()
    value.struct_value.update(obj)
    return value


async def allowed(url: str) -> bool:
    ok = url in PUSH_ALLOWED
    if not ok:
        log.info("push refused: %s is not on the allow-list", url)
    return ok


def app():
    configs = InMemoryPushNotificationConfigStore()
    handler = DefaultRequestHandler(
        agent_executor=FulfillmentExecutor(), task_store=InMemoryTaskStore(),
        agent_card=CARD, push_config_store=configs,
        push_sender=BasePushNotificationSender(
            httpx.AsyncClient(timeout=5), configs, push_url_validator=allowed),
        push_url_validator=allowed)
    routes = create_agent_card_routes(CARD) + \
        create_jsonrpc_routes(handler, "/")
    return Starlette(routes=routes)


if __name__ == "__main__":
    uvicorn.run(app(), host="127.0.0.1", port=8011, log_level="warning")
