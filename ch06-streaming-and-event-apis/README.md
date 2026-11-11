# Chapter 6: Streaming and Event-Driven APIs

Harbor Books' `order.shipped` event for `ord-5003`, delivered three ways:
as a signed webhook (Standard Webhooks 1.0.0) to a receiver that rejects
forged and stale deliveries and processes a retry once, as WebSocket text
frames after an upgrade written by hand over a bare socket, and as a
Server-Sent Events stream that is dropped and resumed with
`Last-Event-ID`. `asyncapi.yaml` describes the event in AsyncAPI 3.1.0.

## Requirements

- [uv](https://docs.astral.sh/uv/) (0.6.14 used); it installs Python 3.13
  (pinned in `.python-version`) and the packages in `pyproject.toml`:
  `starlette==1.10.0`, `uvicorn==0.54.0`, `websockets==17.4`,
  `httpx==0.28.1`, `pyyaml==6.0.3`, `jsonschema==4.26.0` (locked in
  `uv.lock`)
- Node 24, for `@asyncapi/parser` 3.6.6 in `validate/` (locked in
  `validate/package-lock.json`)

No account and no API key. The webhook secret in `webhooks.py` is a
placeholder (`whsec_` plus the base64 of `not-a-real-secret-for-harbor-hooks`);
a real one is random, per subscription, and kept out of Git. Nothing
leaves `127.0.0.1` after `uv sync` and `npm ci`.

```bash
./run.sh          # every listing; writes captures/
```

`run.sh` stops if port 8006 or 8061 is already in use.

## Files

| File | What it is |
|---|---|
| `webhooks.py` | `sign()` and `verify()` per Standard Webhooks 1.0.0: HMAC-SHA256 over `id.timestamp.body`, `v1,` signatures, constant-time compare, 300-second timestamp tolerance |
| `receiver.py` | a subscriber's receiver, `POST /hooks/harbor` on `127.0.0.1:8061`; 401 for a bad signature or stale timestamp, 204 for a processed or duplicate delivery; `--fail-first N` answers 503 to the first N genuine deliveries |
| `sender.py` | Harbor's sender: delivers `order.shipped` for `ord-5003`, retries with the same `webhook-id` after 1, 2, and 4 seconds (a stand-in for a schedule of minutes and hours); `--dump`, `--repeat` (replay), `--secret` (forgery), `--timestamp-offset` (stale), `--once` |
| `events.py` | the event data shared by every style, and a per-order event log with ids from 1 |
| `harbor_stream.py` | `GET /orders/{order_id}/events` (SSE, honors `Last-Event-ID`) and `/ws` (WebSocket) on `127.0.0.1:8006`; a stand-in warehouse ships a paid order 0.3 s after the first watcher |
| `sse_client.py` | reads the SSE stream line by line, stops after a given id, sends `Last-Event-ID` with `--last-event-id` |
| `ws_raw.py` | a WebSocket client on a bare socket that prints the upgrade and every frame's header bits |
| `asyncapi.yaml` | the AsyncAPI 3.1.0 document for `order.shipped` |
| `check_contract.py` | validates a captured delivery's headers and body against `asyncapi.yaml` |
| `validate/validate.mjs` | parses and validates `asyncapi.yaml` with `@asyncapi/parser` |
| `run.sh` | runs everything below and writes `captures/` |

## What the run shows

```text
503 msg_... (receiver busy, stand-in)
204 msg_... processed order.shipped ord-5003
204 msg_... duplicate, already processed
401 rejected: no signature matches
401 rejected: timestamp over 300 seconds from now
```

The SSE client reads events 1 (`order.placed`) and 2 (`order.paid`),
drops the connection, reconnects with `Last-Event-ID: 2`, and receives
only event 3 (`order.shipped`). The WebSocket client gets
`Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=`, RFC 6455's own
example value, then the same three events as text frames, and both sides
close with code 1000.

## Run it by hand

```bash
uv run python receiver.py --fail-first 1     # in one terminal
uv run python sender.py --repeat             # in another
uv run python sender.py --once --timestamp-offset -600

uv run python harbor_stream.py               # in one terminal
uv run python sse_client.py ord-5003 --until 2
uv run python sse_client.py ord-5003 --last-event-id 2 --until 3
curl -N http://127.0.0.1:8006/orders/ord-5003/events
uv run python ws_raw.py
```

## Captures

Recorded November 11, 2026. Webhook IDs, timestamps, signatures, and
`shipped_at` change on every run. `delivery.http` is the first attempt
as sent, with the JSON body indented (it is sent compact).
`receiver.log`, `sender.out`, `forged.out`, `stale.out` hold the webhook
runs; `asyncapi-validate.out` and `contract.out` the contract checks;
`sse-first.out`, `sse-resume.out`, and `websocket.out` the streams.
