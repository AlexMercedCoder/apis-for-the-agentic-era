"""The shopping agent's push-notification receiver (A2A 1.0).

The shopping agent mints a fresh, single-purpose token for each task it
hands off and registers it here (in this demo, by appending it to
$OUT/push-tokens.txt, because the two run as separate processes; in a real
deployment the receiver is part of the shopping agent). The fulfillment
agent POSTs each task update with that token. The receiver:

- refuses a token it didn't mint (401), compared in constant time;
- binds each token to the first task ID it arrives with, and refuses that
  token for any other task (403), so one task's token can't speak for
  another;
- processes each update once, keyed by task, state, and status time,
  because deliveries can repeat (A2A 1.0 section 4.3).

A production receiver also uses HTTPS and asks the agent for
`authentication` credentials in the push config (A2A 1.0 section 13.2).

    uv run python push_receiver.py        # 127.0.0.1:8012
"""

import hmac
import logging
import os
import sys
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

TOKENS = Path(os.environ.get("OUT", "captures")) / "push-tokens.txt"
bound = {}                               # token -> task ID
seen = set()
log = logging.getLogger("receiver")
logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(message)s")


def minted(given):
    known = TOKENS.read_text().split() if TOKENS.exists() else []
    match = None
    for token in known:
        if hmac.compare_digest(given.encode(), token.encode()):
            match = token
    return match


async def push(request):
    token = minted(request.headers.get("x-a2a-notification-token", ""))
    if token is None:
        log.info("401 push with a token this receiver never minted")
        return Response(status_code=401)
    body = await request.json()
    if "task" in body:
        update = {"taskId": body["task"]["id"],
                  "status": body["task"]["status"]}
    else:
        update = body.get("statusUpdate") or body.get("artifactUpdate", {})
    task_id = update.get("taskId", "")
    if bound.setdefault(token, task_id) != task_id:
        log.info("403 push for task %s with another task's token",
                 task_id[:8])
        return Response(status_code=403)
    if "artifact" in update:
        log.info("204 task %s artifact %s", task_id[:8],
                 update["artifact"].get("name"))
        return Response(status_code=204)
    status = update["status"]
    key = (task_id, status["state"], status.get("timestamp"))
    if key in seen:
        log.info("204 task %s %s duplicate, ignored", key[0][:8], key[1])
        return Response(status_code=204)
    seen.add(key)
    log.info("204 task %s %s", key[0][:8], key[1])
    return Response(status_code=204)


app = Starlette(routes=[Route("/a2a/push", push, methods=["POST"])])

if __name__ == "__main__":
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    uvicorn.run(app, host="127.0.0.1", port=8012, log_level="warning")
