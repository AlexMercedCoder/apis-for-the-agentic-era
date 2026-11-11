"""The shopping agent's push-notification receiver (A2A 1.0).

The fulfillment agent POSTs each task update here with the token the
shopping agent chose when it asked for pushes. The receiver refuses a
missing or wrong token, and processes each update once, keyed by task,
state, and status time, because deliveries can repeat (A2A 1.0 § 4.3).

    uv run python push_receiver.py        # 127.0.0.1:8012
"""

import hmac
import logging
import sys

import uvicorn
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

TOKEN = "not-a-real-push-token"         # placeholder; one per task in real use
seen = set()
log = logging.getLogger("receiver")
logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(message)s")


async def push(request):
    given = request.headers.get("x-a2a-notification-token", "")
    if not hmac.compare_digest(given.encode(), TOKEN.encode()):
        log.info("401 push with a wrong token")
        return Response(status_code=401)
    body = await request.json()
    update = body.get("statusUpdate") or {}
    if "task" in body:
        update = {"taskId": body["task"]["id"],
                  "status": body["task"]["status"]}
    if not update:
        art = body.get("artifactUpdate", {})
        log.info("204 task %s artifact %s", art.get("taskId", "")[:8],
                 art.get("artifact", {}).get("name"))
        return Response(status_code=204)
    status = update["status"]
    key = (update["taskId"], status["state"], status.get("timestamp"))
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
