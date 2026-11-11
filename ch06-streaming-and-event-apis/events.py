"""The order events Harbor publishes, shared by every delivery style.

order_event() is the event's data, the same object whether it travels in a
webhook body, an SSE data field, or a WebSocket text frame. The event's
type (order.paid, order.shipped) carries the status.
OrderLog keeps each order's events in sequence, so a stream that
reconnects can ask for everything after the last ID it saw.
"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

SEED = Path(__file__).resolve().parent.parent / "harbor-data" / "orders.json"
FLOW = ["placed", "paid", "shipped", "delivered"]


def order_event(order_id: str, status: str, at: str | None = None) -> dict:
    data = {"order_id": order_id}
    if status == "shipped" and at:
        data["shipped_at"] = at
    return data


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class OrderLog:
    """Events per order: (id, type, data), ids counting from 1."""

    def __init__(self) -> None:
        orders = json.loads(SEED.read_text())
        self.events: dict[str, list[tuple[int, str, dict]]] = {}
        self.changed: dict[str, asyncio.Condition] = {}
        self.status = {o["id"]: o["status"] for o in orders}
        for oid, status in self.status.items():
            steps = FLOW[:FLOW.index(status) + 1] if status in FLOW else []
            self.events[oid] = [
                (i, f"order.{s}", order_event(oid, s))
                for i, s in enumerate(steps, 1)]

    def condition(self, oid: str) -> asyncio.Condition:
        return self.changed.setdefault(oid, asyncio.Condition())

    async def append(self, oid: str, status: str) -> None:
        log = self.events[oid]
        log.append((len(log) + 1, f"order.{status}",
                    order_event(oid, status, now())))
        self.status[oid] = status
        cond = self.condition(oid)
        async with cond:
            cond.notify_all()

    async def after(self, oid: str, last_id: int):
        """Yield events after last_id, waiting for new ones as they come."""
        sent = last_id
        while True:
            for event in self.events[oid]:
                if event[0] > sent:
                    sent = event[0]
                    yield event
            cond = self.condition(oid)
            async with cond:
                if len(self.events[oid]) <= sent:
                    await cond.wait()
