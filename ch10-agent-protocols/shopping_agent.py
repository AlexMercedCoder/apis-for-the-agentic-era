"""Rosa's shopping agent hands an order to Harbor's fulfillment agent.

It speaks A2A 1.0's JSON-RPC binding with plain httpx, so every message is
visible: it reads the Agent Card, sends SendStreamingMessage with a data
part and a push-notification config, prints each streamed event, then reads
the finished task back with GetTask. A deterministic program stands in for
the model that would decide to delegate.

    uv run python shopping_agent.py [order_id]
"""

import json
import os
import secrets
import sys
import uuid

from pathlib import Path

import httpx

AGENT = "http://127.0.0.1:8011"
OUT = Path(os.environ.get("OUT", "captures"))
HEADERS = {"A2A-Version": "1.0", "Content-Type": "application/json"}


def rpc(method, params, rid):
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}


def describe(result):
    if "task" in result:
        t = result["task"]
        text = t["status"].get("message", {}).get("parts", [{}])[0]
        return (f"task {t['id'][:8]} {t['status']['state']} "
                f"{text.get('text', '')}").rstrip()
    if "statusUpdate" in result:
        u = result["statusUpdate"]
        text = u["status"].get("message", {}).get("parts", [{}])[0]
        return (f"status {u['status']['state']:<20} "
                f"{text.get('text', '')}")
    if "artifactUpdate" in result:
        a = result["artifactUpdate"]["artifact"]
        return f"artifact {a['name']} {json.dumps(a['parts'][0]['data'])}"
    return json.dumps(result)


def main(order_id):
    card = httpx.get(f"{AGENT}/.well-known/agent-card.json").json()
    (OUT / "agent-card.json").write_text(json.dumps(card, indent=2))
    skill = card["skills"][0]
    print(f"card: {card['name']}, skill {skill['id']}, "
          f"streaming={card['capabilities']['streaming']}, "
          f"push={card['capabilities']['pushNotifications']}")
    endpoint = card["supportedInterfaces"][0]["url"]
    push_token = secrets.token_urlsafe(24)     # one per task, single-purpose
    with (OUT / "push-tokens.txt").open("a") as f:
        f.write(push_token + "\n")
    params = {
        "message": {
            "messageId": str(uuid.uuid4()), "role": "ROLE_USER",
            "parts": [{"text": "Please ship this order for Rosa."},
                      {"data": {"order_id": order_id, "gift_wrap": True}}]},
        "configuration": {"taskPushNotificationConfig": {
            "url": "http://127.0.0.1:8012/a2a/push",
            "token": push_token}}}
    body = rpc("SendStreamingMessage", params, 1)
    shown = json.loads(json.dumps(body))
    shown["params"]["configuration"]["taskPushNotificationConfig"][
        "token"] = f"<{len(push_token)}-character token, this task only>"
    print("request: " + json.dumps(shown))
    task_id = None
    raw = (OUT / f"a2a-stream-{order_id}.txt").open("w")
    with httpx.stream("POST", endpoint, json=body, headers=HEADERS,
                      timeout=30) as resp:
        print(f"response: {resp.status_code} "
              f"{resp.headers['content-type']}")
        for line in resp.iter_lines():
            raw.write(line + "\n")
            if line.startswith("data:"):
                event = json.loads(line[5:])
                if "error" in event:
                    print("error: " + json.dumps(event["error"]))
                    continue
                result = event["result"]
                if task_id is None:
                    task_id = result.get("task", {}).get("id") or \
                        result.get("statusUpdate", {}).get("taskId")
                    with (OUT / "push-tasks.txt").open("a") as f:
                        f.write(f"{push_token} {task_id}\n")   # expect it
                print("  " + describe(result))
    raw.close()
    if task_id is None:
        return
    done = httpx.post(endpoint, headers=HEADERS, json=rpc(
        "GetTask", {"id": task_id}, 2)).json()["result"]
    print(f"GetTask: {done['status']['state']}, "
          f"{len(done.get('artifacts', []))} artifact")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "ord-5003")
