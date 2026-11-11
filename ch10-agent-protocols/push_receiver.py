"""The shopping agent's push-notification receiver (A2A 1.0).

The shopping agent mints a fresh, single-purpose token for each task it
hands off and registers it here (in this demo, by appending it to
$OUT/push-tokens.txt, because the two run as separate processes; in a real
deployment the receiver is part of the shopping agent). The fulfillment
agent POSTs each task update with that token. The receiver:

- refuses a token it didn't mint (401), compared in constant time;
- accepts a token only for the task the shopping agent created with it,
  which the shopping agent records (in $OUT/push-tasks.txt) as soon as the
  stream names the task; an update that arrives before that is held (202)
  and checked once the task is known; a push for any other task gets 403,
  so one task's token can't speak for another (A2A 1.0 section 4.3.3);
- processes each update once, keyed by task, state, and status time,
  because deliveries can repeat (A2A 1.0 section 4.3).

A production receiver also uses HTTPS and asks the agent for
`authentication` credentials in the push config (A2A 1.0 section 13.2).

    uv run python push_receiver.py        # 127.0.0.1:8012
"""

import asyncio
import contextlib
import hmac
import logging
import os
import sys
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

OUT = Path(os.environ.get("OUT", "captures"))
TOKENS = OUT / "push-tokens.txt"
TASKS = OUT / "push-tasks.txt"
held = {}                                # token -> updates
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


def expected_task(token):
    """The task the shopping agent created with this token, if it knows yet."""
    if TASKS.exists():
        for line in TASKS.read_text().splitlines():
            minted_for, _, task_id = line.partition(" ")
            if hmac.compare_digest(minted_for.encode(), token.encode()):
                return task_id
    return None


def update_of(body):
    if "task" in body:
        return {"taskId": body["task"]["id"], "status": body["task"]["status"]}
    return body.get("statusUpdate") or body.get("artifactUpdate", {})


def process(update):
    task_id = update.get("taskId", "")
    if "artifact" in update:
        log.info("204 task %s artifact %s", task_id[:8],
                 update["artifact"].get("name"))
        return
    status = update["status"]
    key = (task_id, status["state"], status.get("timestamp"))
    if key in seen:
        log.info("204 task %s %s duplicate, ignored", key[0][:8], key[1])
        return
    seen.add(key)
    log.info("204 task %s %s", key[0][:8], key[1])


def release(token, expected):
    """Process held updates for the expected task; drop any others."""
    for earlier in held.pop(token, []):
        if earlier.get("taskId") == expected:
            process(earlier)
        else:
            log.info("dropped a held update for task %s",
                     earlier.get("taskId", "")[:8])


async def sweep():
    """Release held updates as soon as the shopping agent records the task."""
    while True:
        for token in list(held):
            expected = expected_task(token)
            if expected is not None:
                release(token, expected)
        await asyncio.sleep(0.2)


@contextlib.asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(sweep())
    yield
    task.cancel()


async def push(request):
    token = minted(request.headers.get("x-a2a-notification-token", ""))
    if token is None:
        log.info("401 push with a token this receiver never minted")
        return Response(status_code=401)
    update = update_of(await request.json())
    expected = expected_task(token)
    if expected is None:
        # The agent can post before the shopping agent has read the task ID
        # from its stream. Hold the update until the task is known.
        held.setdefault(token, []).append(update)
        log.info("202 task %s held until the task is known",
                 update.get("taskId", "")[:8])
        return Response(status_code=202)
    release(token, expected)
    if update.get("taskId") != expected:
        log.info("403 push for task %s with another task's token",
                 update.get("taskId", "")[:8])
        return Response(status_code=403)
    process(update)
    return Response(status_code=204)


app = Starlette(routes=[Route("/a2a/push", push, methods=["POST"])],
                lifespan=lifespan)

if __name__ == "__main__":
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    uvicorn.run(app, host="127.0.0.1", port=8012, log_level="warning")
