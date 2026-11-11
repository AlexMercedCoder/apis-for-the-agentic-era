"""Harbor's order events as an SSE stream and over a WebSocket.

    uv run python harbor_stream.py [--port 8006]

GET /orders/{order_id}/events   text/event-stream, honors Last-Event-ID
GET /ws                         WebSocket; send {"type": "watch",
                                "order_id": "ord-5003"} to subscribe

Like the earlier Harbor demos, this server has no authentication: anyone
who can reach it can watch any order. A real stream authenticates the
caller as any other request does and checks that the caller may see the
order. The WebSocket route does check Origin, because a browser sends
cookies on a cross-site upgrade: a request with an Origin outside
ALLOWED_ORIGINS is refused with 403 before the upgrade.

The first watcher of a paid order starts a stand-in warehouse that ships
it SHIP_AFTER seconds later, so every client sees order.shipped arrive
live. Order state lives in memory and starts from ../harbor-data/.
"""

import argparse
import asyncio
import json

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from events import OrderLog

ALLOWED_ORIGINS = {"http://127.0.0.1:8006", "http://localhost:8006"}
SHIP_AFTER = 0.3
KEEPALIVE = 15
orders = OrderLog()
warehouse: dict[str, asyncio.Task] = {}


def start_warehouse(oid: str) -> None:
    if orders.status.get(oid) == "paid" and oid not in warehouse:
        async def ship() -> None:
            await asyncio.sleep(SHIP_AFTER)
            await orders.append(oid, "shipped")
        warehouse[oid] = asyncio.create_task(ship())


def sse(event_id: int, kind: str, data: dict) -> str:
    return (f"id: {event_id}\nevent: {kind}\n"
            f"data: {json.dumps(data, separators=(',', ':'))}\n\n")


async def events(request: Request):
    oid = request.path_params["order_id"]
    if oid not in orders.events:
        return JSONResponse({"error": "order_not_found"}, status_code=404)
    last = request.headers.get("last-event-id", "0")
    last_id = int(last) if last.isdigit() else 0
    start_warehouse(oid)

    async def stream():
        yield "retry: 3000\n\n"
        source = orders.after(oid, last_id)
        while True:
            try:
                eid, kind, data = await asyncio.wait_for(
                    anext(source), KEEPALIVE)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            yield sse(eid, kind, data)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"cache-control": "no-cache",
                                      "x-accel-buffering": "no"})


async def ws(socket: WebSocket) -> None:
    origin = socket.headers.get("origin")
    if origin is not None and origin not in ALLOWED_ORIGINS:
        await socket.close(code=1008)
        return
    await socket.accept()
    try:
        msg = await socket.receive_json()
        oid = msg.get("order_id")
        if msg.get("type") != "watch" or oid not in orders.events:
            await socket.send_json({"type": "error",
                                    "detail": "send watch with an order_id"})
            await socket.close(code=1008)
            return
        start_warehouse(oid)

        async def push() -> None:
            async for eid, kind, data in orders.after(oid, 0):
                await socket.send_text(json.dumps(
                    {"id": eid, "type": kind, "data": data},
                    separators=(",", ":")))

        pusher = asyncio.create_task(push())
        try:
            while True:
                await socket.receive_text()
        finally:
            pusher.cancel()
    except WebSocketDisconnect:
        return


app = Starlette(routes=[
    Route("/orders/{order_id}/events", events),
    WebSocketRoute("/ws", ws),
])

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8006)
    args = p.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning",
                ws="websockets-sansio")
