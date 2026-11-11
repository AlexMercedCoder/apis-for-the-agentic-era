"""A subscriber's webhook receiver for Harbor's order.shipped event.

POST /hooks/harbor on 127.0.0.1:8061. It verifies the signature and the
timestamp, answers 401 to anything that fails, and remembers each
webhook-id it has processed so a retried or replayed delivery is
acknowledged without being processed twice.

--fail-first N answers the first N genuine deliveries with 503, standing in
for a receiver that is briefly down, so the sender's retry shows up.
The seen-ID set is in memory; a real receiver stores it with the work it
does, in the same transaction.
"""

import argparse
import json
import sys

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from webhooks import SECRET, InvalidWebhook, verify

state = {"fail_remaining": 0, "seen": set()}


def log(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


async def hook(request: Request) -> Response:
    body = await request.body()
    try:
        msg_id = verify(SECRET, request.headers, body)
    except InvalidWebhook as e:
        log(f"401 rejected: {e}")
        return JSONResponse({"error": "invalid_webhook", "detail": str(e)},
                            status_code=401)
    if state["fail_remaining"] > 0:
        state["fail_remaining"] -= 1
        log(f"503 {msg_id} (receiver busy, stand-in)")
        return Response(status_code=503)
    if msg_id in state["seen"]:
        log(f"204 {msg_id} duplicate, already processed")
        return Response(status_code=204)
    event = json.loads(body)
    state["seen"].add(msg_id)
    log(f"204 {msg_id} processed {event['type']} "
        f"{event['data']['order_id']}")
    return Response(status_code=204)


app = Starlette(routes=[Route("/hooks/harbor", hook, methods=["POST"])])

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8061)
    p.add_argument("--fail-first", type=int, default=0)
    args = p.parse_args()
    state["fail_remaining"] = args.fail_first
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
