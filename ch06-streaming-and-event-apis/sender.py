"""Harbor's webhook sender: delivers order.shipped for ord-5003, signed.

    uv run python sender.py [--url URL] [--dump FILE] [--repeat]
                            [--secret S] [--timestamp-offset N] [--once]

Each attempt keeps the same webhook-id and is signed again with a fresh
timestamp. On anything but a 2xx it waits and tries again. The demo
schedule (0, 1, 2, 4 seconds) is a stand-in: Standard Webhooks suggests
5 seconds, 5 minutes, 30 minutes, then hours, with jitter, over days.
A 410 Gone stops delivery for good.

--repeat sends the identical signed request a second time (a replay).
--secret signs with another key (a forgery). --timestamp-offset shifts
the timestamp (a stale replay). --once makes a single attempt.

Before any attempt the sender checks the target, because a subscriber can
register any URL: it must be https, and every address the host resolves
to must be public (no loopback, private, link-local, or metadata ranges).
--allow-local-http permits http://127.0.0.1 for this demo only. The check
runs again at send time, since DNS can change after registration, and
httpx does not follow redirects, so a 3xx counts as a failure. A
production sender also routes deliveries through an egress proxy that
enforces the same rule. Retry-After is not honored in this demo.
"""

import argparse
import ipaddress
import json
import socket
import time
import uuid
from datetime import datetime, timezone

import httpx

from events import order_event
from webhooks import SECRET, sign

URL = "http://127.0.0.1:8061/hooks/harbor"
SCHEDULE = [0, 1, 2, 4]


class UnsafeTarget(Exception):
    pass


def check_target(url: str, allow_local_http: bool) -> None:
    parsed = httpx.URL(url)
    local = allow_local_http and parsed.host == "127.0.0.1"
    if parsed.scheme != "https" and not local:
        raise UnsafeTarget(f"{parsed.scheme}:// is not https")
    infos = socket.getaddrinfo(parsed.host, parsed.port or 443)
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if local and addr.is_loopback:
            continue
        if not addr.is_global:
            raise UnsafeTarget(f"{parsed.host} resolves to {addr}, "
                               "which is not a public address")


def build(secret: str, msg_id: str, body: bytes, offset: int) -> dict:
    ts = str(int(time.time()) + offset)
    return {
        "content-type": "application/json",
        "webhook-id": msg_id,
        "webhook-timestamp": ts,
        "webhook-signature": sign(secret, msg_id, ts, body),
    }


def dump(path: str, url: str, headers: dict, body: bytes) -> None:
    path_part = "/" + url.split("/", 3)[3]
    lines = [f"POST {path_part} HTTP/1.1"]
    lines += [f"{k}: {v}" for k, v in headers.items()]
    lines += ["", json.dumps(json.loads(body), indent=2)]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=URL)
    p.add_argument("--dump")
    p.add_argument("--repeat", action="store_true")
    p.add_argument("--secret", default=SECRET)
    p.add_argument("--timestamp-offset", type=int, default=0)
    p.add_argument("--once", action="store_true")
    p.add_argument("--allow-local-http", action="store_true")
    args = p.parse_args()

    shipped_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    event = {"type": "order.shipped", "timestamp": shipped_at,
             "data": order_event("ord-5003", "shipped", shipped_at)}
    body = json.dumps(event, separators=(",", ":")).encode()
    msg_id = "msg_" + uuid.uuid4().hex[:24]
    schedule = SCHEDULE[:1] if args.once else SCHEDULE

    with httpx.Client(timeout=15) as client:
        for attempt, delay in enumerate(schedule, 1):
            time.sleep(delay)
            try:
                check_target(args.url, args.allow_local_http)
            except UnsafeTarget as e:
                print(f"refused {args.url}: {e}")
                return
            headers = build(args.secret, msg_id, body,
                            args.timestamp_offset)
            if args.dump and attempt == 1:
                dump(args.dump, args.url, headers, body)
            r = client.post(args.url, content=body, headers=headers)
            note = f" {r.text}" if r.text else ""
            print(f"attempt {attempt} after {delay}s: {r.status_code}{note}")
            if r.is_success:
                if args.repeat:
                    r = client.post(args.url, content=body, headers=headers)
                    print(f"replay of the same request: {r.status_code}")
                return
            if r.status_code == 410:
                print("410 Gone: subscription disabled, no more attempts")
                return
    print(f"gave up on {msg_id} after {len(schedule)} attempts")


if __name__ == "__main__":
    main()
