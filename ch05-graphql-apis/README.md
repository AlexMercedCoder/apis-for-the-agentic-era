# Chapter 5: GraphQL and Client-Shaped Queries

Harbor Books' storefront as a GraphQL API over the same seed data as
Chapter 3: a schema written in Python with Strawberry and pinned as SDL,
the `ord-5002` order screen as one query, errors inside a 200 OK, the N+1
problem and DataLoader batching, depth and cost limits, trusted documents,
introspection, and two ways to turn the API into tools for a model.
`measure.py` loads the same order screen from Chapter 3's REST API and
from this server and counts requests and bytes.

## Requirements

- [uv](https://docs.astral.sh/uv/) (0.6.14 used); it installs Python 3.13
  (pinned in `.python-version`) and the packages in `pyproject.toml`:
  `strawberry-graphql==0.341.2`, `uvicorn==0.54.0`, `starlette==1.10.0`,
  `httpx==0.28.1` (locked in `uv.lock` with graphql-core 3.3.2)
- `curl` (7.81.0 used)

No account and no API key. Nothing leaves `127.0.0.1`. Run it from inside
the repository: the server reads `../harbor-data/`, and `measure.py` also
starts `../ch03-rest-apis/` on port 8003.

```bash
./run.sh          # every listing, then the comparison; writes captures/
```

## Files

| File | What it is |
|---|---|
| `harbor_graphql.py` | the GraphQL API (Strawberry on uvicorn), `POST /graphql` on `127.0.0.1:8005`; options `--no-loaders`, `--trusted-only`, `--no-introspection`, `--export` (writes `schema.graphql`) |
| `schema.graphql` | the pinned SDL, Harbor's GraphQL contract |
| `store.py` | the data layer over `../harbor-data/`; every call is counted in the server log |
| `limits.py` | the cost rule (1 per field, lists multiply by `first`, 10 without one, 50 for a variable); run it on operation files to print their cost |
| `operations/*.graphql` | the storefront's operations: `OrderScreen`, `CatalogRatings`, `PlaceOrder` |
| `persist.py` | writes `trusted-documents.json`, each operation keyed by `sha256:` of its text |
| `body.py` | prints a request body for one operation, with `--id` to send the document ID |
| `introspect.py` | reads the schema from the running server and compares it with `schema.graphql` |
| `measure.py` | the order screen over REST (five `GET`s) and GraphQL (one `POST`, as text and as a document ID) |
| `graphql_tools.py` | agent tools (Messages API format): one curated tool per trusted document, and the generic `run_graphql` |
| `requests/*.json` | hand-written request bodies (partial error, typo, too deep, too costly, introspection) |
| `run.sh` | starts the server in four configurations, runs every listing, writes `captures/` |

## What the server does

- Limits, checked during validation: depth at most 6 (Strawberry's
  `QueryDepthLimiter`) and cost at most 1,000 (`limits.py`).
  `OrderScreen` costs 115, `CatalogRatings` 651, `PlaceOrder` 4.
- Errors: a missing order or book is `NOT_FOUND` with a `hint` in
  `extensions`, and the rest of the data still comes back. Strawberry
  0.341.2 answers every GraphQL error, including validation and limit
  errors, with 200 OK and `application/json`. The GraphQL over HTTP draft
  (Stage 2 as of November 11, 2026) asks for 4xx on request errors and
  294 on partial results when the client accepts
  `application/graphql-response+json`.
- Trusted documents: a body with `documentId` runs the matching entry of
  `trusted-documents.json`. With `--trusted-only`, a body with query text
  gets 400 Bad Request.
- `placeOrder` takes a `requestId`; a retry with the same ID returns the
  first order (`ord-5005`, 3,797 cents).
- The `orderStatus` subscription is in the schema and checked by
  `../verify.sh` through `schema.subscribe`; this chapter serves no
  subscription over HTTP (Chapter 6 covers server push).

Each request logs one line to stderr with its back-end calls, such as
`POST /graphql 200 op=CatalogRatings backend_calls=9 list_books=1
reviews_for=8` with `--no-loaders` and `backend_calls=2 list_books=1
reviews_for_many=1` with the default DataLoaders.

## Run it by hand

```bash
uv run python harbor_graphql.py                 # in one terminal
uv run python body.py OrderScreen '{"orderId": "ord-5002"}' > /tmp/q.json
curl -s -X POST http://127.0.0.1:8005/graphql \
  -H 'Content-Type: application/json' --data-binary @/tmp/q.json
curl -s -X POST http://127.0.0.1:8005/graphql \
  -H 'Content-Type: application/json' --data-binary @requests/partial.json
uv run python introspect.py
uv run python graphql_tools.py
uv run python limits.py operations/*.graphql
```

## Changing the schema or the operations

Edit `harbor_graphql.py`, then run `uv run python harbor_graphql.py
--export` and review the diff to `schema.graphql`. Edit or add a file in
`operations/`, then run `uv run python persist.py`. `../verify.sh` fails
if the served schema differs from `schema.graphql` or if
`trusted-documents.json` is out of date.

## Captures

Recorded November 11, 2026. `*.out` files hold the status line, headers
(without `date`), and body; `order-screen.err` holds `curl -v`'s trace.
`measure.out` and `order-screen-sizes.json` hold the comparison: 20 runs
after one warm-up per connection, identical every run. REST, five GETs:
984 body bytes, 36 values, 2,041 wire bytes. GraphQL, query text: 324
body bytes, 11 values, 968 wire bytes. GraphQL, document ID: 710 wire
bytes.
