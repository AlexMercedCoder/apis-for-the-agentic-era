"""Read Harbor's SSE stream and print it line by line, as received.

    uv run python sse_client.py ORDER_ID [--last-event-id N] [--until ID]

Stops after the event whose id is --until, closing the connection the way
a dropped network would. --last-event-id sends the Last-Event-ID header,
as a browser's EventSource does when it reconnects.
"""

import argparse

import httpx

p = argparse.ArgumentParser()
p.add_argument("order_id")
p.add_argument("--last-event-id")
p.add_argument("--until", type=int, required=True)
p.add_argument("--base", default="http://127.0.0.1:8006")
args = p.parse_args()

headers = {"accept": "text/event-stream"}
if args.last_event_id:
    headers["last-event-id"] = args.last_event_id
url = f"{args.base}/orders/{args.order_id}/events"
with httpx.stream("GET", url, headers=headers, timeout=10) as r:
    print(f"HTTP/1.1 {r.status_code} {r.reason_phrase}")
    for name in ("content-type", "cache-control", "x-accel-buffering"):
        print(f"{name}: {r.headers[name]}")
    print()
    done = False
    for line in r.iter_lines():
        print(line)
        if line == f"id: {args.until}":
            done = True
        if done and line == "":
            break
