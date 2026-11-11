# Chapter 3: REST and the Resource-Oriented Web

Harbor Books' REST API: the catalog, its reviews, and orders as resources,
HTTP's own methods, status codes, caching, and conditional requests, every
error as RFC 9457 problem details, an `Idempotency-Key` on `POST /orders`,
and the OpenAPI document that later chapters use as the API's source of
truth. The same document is turned into agent tools.

## Requirements

- `curl` (the captures were made with 7.81.0)
- [uv](https://docs.astral.sh/uv/) (0.6.14 used); it installs Python 3.13
  (pinned in `.python-version`) and the pinned packages in `pyproject.toml`: `fastapi==0.162.0`,
  `uvicorn==0.54.0`, `httpx==0.28.1`, `pyyaml==6.0.3` (locked in `uv.lock`
  with pydantic 2.14.3 and starlette 1.10.0)

No account and no API key. Nothing leaves `127.0.0.1`. Run it from inside
the repository: the server reads `../harbor-data/`, and every restart begins
from that seed state.

## Files

| File | What it is |
|---|---|
| `harbor_api.py` | the API (FastAPI) on `127.0.0.1:8003`; `--export` rewrites `openapi.json` |
| `openapi.json` | the pinned OpenAPI 3.1.0 document, Harbor's REST contract |
| `show_operation.py` | prints one operation from `openapi.json` as YAML |
| `openapi_tools.py` | builds agent tools (Messages API format) from `openapi.json`; the host, not the model, supplies `Idempotency-Key` |
| `requests/*.json` | order bodies for `curl` |
| `run.sh` | starts the server, runs every listing, writes `captures/`, stops the server |
| `captures/` | the exact output the chapter prints |

## Resources

| Path | Methods | Operation |
|---|---|---|
| `/books` | `GET` (`q`, `limit` 1 to 50, `cursor`) | `search_catalog` |
| `/books/{book_id}` | `GET` (`ETag`, `If-None-Match`, `Cache-Control: max-age=60`) | `get_book` |
| `/books/{book_id}/reviews` | `GET` | `list_reviews` |
| `/orders` | `POST` with `Idempotency-Key` | `place_order` |
| `/orders/{order_id}` | `GET` (with `links`) | `get_order_status` |
| `/orders/{order_id}/cancellation` | `GET`, `PUT` (idempotent) | `get_cancellation`, `cancel_order` |

Every error body is `application/problem+json` with `type`
(`https://harbor.example/problems/<slug>`), `title`, `status`, `detail`,
`instance`, and Harbor's extension members `hint` and `errors`.

`Idempotency-Key` follows the IETF httpapi draft
(draft-ietf-httpapi-idempotency-key-header-08, October 2026): missing key
400, a key that isn't a lowercase UUID in double quotes 400, same key with a different body 422, same key while the first request
is still running 409, same key and body within 24 hours replays the first
response, and older keys are forgotten. Keys are scoped to the body's
`customer_id` (the composite key of draft-08 section 5), but the caller
asserts that ID, so the scope protects nothing until Chapter 8
authenticates the caller. Keys live in memory, so a restart forgets them;
a real service stores them in the same transaction as the order.

## Run it

```bash
./run.sh
```

Or by hand, with the server in one terminal:

```bash
uv run python harbor_api.py
```

and the callers in another:

```bash
curl -v http://127.0.0.1:8003/books/bk-1001
curl -v -H 'If-None-Match: "96405a6e95542fd1"' \
  http://127.0.0.1:8003/books/bk-1001          # 304 Not Modified
curl -s http://127.0.0.1:8003/books/bk-9999     # 404 problem details
curl -s 'http://127.0.0.1:8003/books?limit=5000'  # 422
curl -v -X POST http://127.0.0.1:8003/orders \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: "6f1c2a9e-3b4d-4e5f-8a7b-9c0d1e2f3a4b"' \
  --data-binary @requests/place-order.json       # run it twice
uv run python show_operation.py post /orders
uv run python openapi_tools.py place_order
```

The server's log (`captures/server-log.txt` after `./run.sh`) marks each
`POST /orders` as `idempotency=new` or `idempotency=replay`, and each
cancellation as `cancellation=new` or `cancellation=existing`.

## Changing the API

Edit `harbor_api.py`, then run `uv run python harbor_api.py --export` and
review the diff to `openapi.json`. `run.sh` fetches `/openapi.json` from the
running server and writes `captures/openapi-drift.out`; `../verify.sh` fails
if the served document and the pinned file differ, so code and contract
can't drift apart unnoticed.

## Captures

Recorded November 10, 2026 (the `Date` headers read November 11 GMT). Each
`*.err` holds `curl -v`'s request and response headers and each `*.out` the
body. `../verify.sh` reruns everything into a temporary folder and checks
the lines the chapter prints.
