#!/usr/bin/env bash
# Runs every example in this repository that needs no network access
# beyond 127.0.0.1 and no API keys, and checks the output the book prints.
# Usage: ./verify.sh    (needs bash, curl, uv; Chapters 4 and 6 also need
# Node 24, Chapter 4 needs grpcurl 1.9.4, and Chapter 7 needs Docker)
set -uo pipefail
cd "$(dirname "$0")"
ROOT=$(pwd)
PASS=0
FAIL=0
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

check() {
  local name=$1; shift
  if "$@" > "$TMP/last.log" 2>&1; then
    echo "[OK]   $name"; PASS=$((PASS + 1))
  else
    echo "[FAIL] $name"; sed 's/^/       /' "$TMP/last.log"
    FAIL=$((FAIL + 1))
  fi
}

# --- harbor-data: shared seed data -----------------------------------
check "harbor-data: JSON parses and order totals add up" \
  python3 - "$ROOT/harbor-data" <<'PY'
import json, sys
from pathlib import Path
d = Path(sys.argv[1])
books = {b["id"]: b for b in json.loads((d / "books.json").read_text())}
customers = {c["id"] for c in json.loads((d / "customers.json").read_text())}
for o in json.loads((d / "orders.json").read_text()):
    assert o["customer_id"] in customers, o["id"]
    assert o["status"] in {"placed", "paid", "shipped", "delivered",
                           "cancelled"}, o["id"]
    total = 0
    for line in o["lines"]:
        assert line["book_id"] in books, (o["id"], line["book_id"])
        total += line["quantity"] * line["unit_price_cents"]
    assert total == o["total_cents"], o["id"]
for r in json.loads((d / "reviews.json").read_text()):
    assert r["book_id"] in books and r["customer_id"] in customers, r["id"]
    assert 1 <= r["rating"] <= 5, r["id"]
PY

# --- Chapter 1 ---------------------------------------------------------
CH01="$TMP/ch01"
check "ch01: run.sh completes" env OUT="$CH01" ch01-what-is-an-api/run.sh
check "ch01: curl gets 200 OK with JSON" \
  bash -c "grep -q '< HTTP/1.1 200 OK' '$CH01/curl-get-book.err' &&
           grep -q '< Content-Type: application/json' '$CH01/curl-get-book.err' &&
           grep -q '\"title\": \"Moby-Dick\"' '$CH01/curl-get-book.out'"
check "ch01: unknown book returns 404 with a hint" \
  bash -c "tail -1 '$CH01/curl-get-missing-book.out' | grep -qx 404 &&
           grep -q '\"error\": \"book_not_found\"' '$CH01/curl-get-missing-book.out'"
check "ch01: Python client prints the book" \
  grep -qx 'Moby-Dick by Herman Melville: \$14.99, 7 in stock' "$CH01/client.out"
check "ch01: stand-in emits a get_book tool_use block" \
  bash -c "grep -q '\"stop_reason\": \"tool_use\"' '$CH01/agent.out' &&
           grep -q '\"name\": \"get_book\"' '$CH01/agent.out' &&
           grep -q '\"is_error\": false' '$CH01/agent.out'"
check "ch01: host rejects a book_id the schema forbids" \
  bash -c "cd ch01-what-is-an-api && uv run -q python -c \"
import agent
for bad in ('../admin', 'bk-1001\\n'):
    result, is_error = agent.run_tool('get_book', {'book_id': bad})
    assert is_error and result['error'] == 'invalid_argument', (bad, result)
\""
check "ch01: all three callers sent the same request line" \
  bash -c "[ \$(grep -c '\"GET /books/bk-1001 HTTP/1.1\" 200' '$CH01/server-log.txt') -eq 3 ]"

# --- Chapter 2 ---------------------------------------------------------
CH02="$TMP/ch02"
check "ch02: run.sh completes" env OUT="$CH02" ch02-soap-apis/run.sh
check "ch02: GetOrderStatus returns 200 with a shipped order" \
  bash -c "grep -q '< HTTP/1.1 200 OK' '$CH02/curl-get-order-status.err' &&
           grep -q '> SOAPAction: \"http://harbor.example/orders/GetOrderStatus\"' '$CH02/curl-get-order-status.err' &&
           grep -q '<h:status>shipped</h:status>' '$CH02/curl-get-order-status.out'"
check "ch02: unknown order is a soap:Client fault with a hint, as a 500" \
  bash -c "grep -q '< HTTP/1.1 500 Internal Server Error' '$CH02/curl-order-not-found.err' &&
           grep -q '<faultcode>soap:Client</faultcode>' '$CH02/curl-order-not-found.out' &&
           grep -q 'Order IDs run from ord-5001 to ord-5004.' '$CH02/curl-order-not-found.out'"
check "ch02: an unknown mustUnderstand header fails the message" \
  grep -q '<faultcode>soap:MustUnderstand</faultcode>' "$CH02/curl-must-understand.out"
check "ch02: GetOrderStatus without a UsernameToken is refused" \
  grep -q '<faultcode>wsse:FailedAuthentication</faultcode>' "$CH02/curl-no-token.out"
check "ch02: zeep reads both operations from the WSDL" \
  bash -c "grep -q 'GetOrderStatus(orderId: ns0:OrderId)' '$CH02/zeep-inspect.out' &&
           grep -q 'GetBook(bookId: ns0:BookId)' '$CH02/zeep-inspect.out'"
check "ch02: generated client prints the order and the fault hint" \
  bash -c "grep -qx 'ord-5002: shipped since 2026-11-03 09:41:52+00:00, 48.97 USD' '$CH02/client.out' &&
           grep -qx '  hint: Order IDs run from ord-5001 to ord-5004.' '$CH02/client-not-found.out' &&
           grep -q 'does not match ord-\[0-9\]{4}' '$CH02/client-bad-id.out' &&
           grep -qx '  field: orderId' '$CH02/client-bad-id.out'"
check "ch02: every operation is a POST to one URL; no WSDL fetches" \
  bash -c "[ \$(grep -c '\"POST /harbor/soap HTTP/1.1\"' '$CH02/server-log.txt') -eq 10 ] &&
           grep -q 'action=GetBook' '$CH02/server-log.txt' &&
           ! grep -q 'GET /harbor/soap?wsdl' '$CH02/server-log.txt'"
check "ch02: tool schema carries the WSDL's pattern and statuses" \
  bash -c "cd ch02-soap-apis && uv run -q python -c \"
import soap_tool as t
p = t.TOOL['input_schema']['properties']['order_id']['pattern']
assert p == '^ord-[0-9]{4}\$', p
assert 'placed, paid, shipped, delivered, cancelled' in t.TOOL['description']
assert 'WS-Security' not in t.TOOL['description']
for bad in ('5002', 'ord-5002\\n', '../ord-5002'):
    r, err = t.run_tool({'order_id': bad})
    assert err and r['error'] == 'invalid_argument', (bad, r)
\""
check "ch02: tool turns the SOAP fault into JSON the model can act on" \
  bash -c "grep -q '\"error\": \"order_not_found\"' '$CH02/soap-tool.out' &&
           grep -q '\"hint\": \"Order IDs run from ord-5001 to ord-5004.\"' '$CH02/soap-tool.out'"

# --- Chapter 3 ---------------------------------------------------------
CH03="$TMP/ch03"
check "ch03: run.sh completes" env OUT="$CH03" ch03-rest-apis/run.sh
check "ch03: GET a book returns 200 with an ETag and Cache-Control" \
  bash -c "grep -q '< HTTP/1.1 200 OK' '$CH03/get-book.err' &&
           grep -qi '< etag: \"' '$CH03/get-book.err' &&
           grep -qi '< cache-control: max-age=60' '$CH03/get-book.err'"
check "ch03: If-None-Match with that ETag returns 304 and no body" \
  bash -c "grep -q '< HTTP/1.1 304 Not Modified' '$CH03/get-book-conditional.err' &&
           [ ! -s '$CH03/get-book-conditional.out' ]"
check "ch03: unknown book is a 404 problem details object with a hint" \
  bash -c "grep -q '< HTTP/1.1 404 Not Found' '$CH03/get-book-missing.err' &&
           grep -qi '< content-type: application/problem+json' '$CH03/get-book-missing.err' &&
           grep -q '\"type\": \"https://harbor.example/problems/book-not-found\"' '$CH03/get-book-missing.out' &&
           grep -q 'This catalog has IDs bk-1001 to bk-1008.' '$CH03/get-book-missing.out'"
check "ch03: limit=5000 is refused with a 422 naming the parameter" \
  bash -c "grep -q '< HTTP/1.1 422' '$CH03/search-limit-5000.err' &&
           grep -q '\"parameter\": \"limit\"' '$CH03/search-limit-5000.out'"
check "ch03: pages carry a next URL" \
  grep -q '"next": "/books?limit=2&cursor=bk-1002"' "$CH03/search-page.out"
check "ch03: POST /orders creates ord-5005; the retry replays it" \
  bash -c "grep -qi '< location: /orders/ord-5005' '$CH03/place-order.err' &&
           cmp -s '$CH03/place-order.out' '$CH03/place-order-retry.out' &&
           [ \$(grep -c 'idempotency=new' '$CH03/server-log.txt') -eq 1 ] &&
           [ \$(grep -c 'idempotency=replay' '$CH03/server-log.txt') -eq 1 ]"
check "ch03: a reused key with a new body is 422; no key or a bad one is 400" \
  bash -c "grep -q 'idempotency-key-reused' '$CH03/place-order-key-reused.out' &&
           grep -q '< HTTP/1.1 400 Bad Request' '$CH03/place-order-no-key.err' &&
           grep -q 'idempotency-key-missing' '$CH03/place-order-no-key.out' &&
           grep -q 'idempotency-key-invalid' '$CH03/place-order-bad-key.out'"
check "ch03: out-of-stock order is a 409 problem" \
  grep -q '"type": "https://harbor.example/problems/out-of-stock"' \
  "$CH03/place-order-out-of-stock.out"
check "ch03: order links offer cancel; PUT cancellation twice is the same" \
  bash -c "grep -q '\"cancel\": {\"href\": \"/orders/ord-5005/cancellation\", \"method\": \"PUT\"}' '$CH03/get-order.out' &&
           cmp -s '$CH03/cancel-order.out' '$CH03/cancel-order-again.out' &&
           grep -q 'order-not-cancellable' '$CH03/cancel-shipped.out' &&
           grep -q 'cancellation=new' '$CH03/server-log.txt' &&
           grep -q 'cancellation=existing' '$CH03/server-log.txt'"
check "ch03: DELETE on a book is 405 with Allow; Accept: XML is 406" \
  bash -c "grep -q '< HTTP/1.1 405 Method Not Allowed' '$CH03/delete-book.err' &&
           grep -qi '< allow: GET' '$CH03/delete-book.err' &&
           grep -q '< HTTP/1.1 406 Not Acceptable' '$CH03/accept-xml.err'"
check "ch03: the served OpenAPI document matches the pinned openapi.json" \
  grep -qx 'served /openapi.json matches the pinned openapi.json' \
  "$CH03/openapi-drift.out"
check "ch03: the place_order operation declares Idempotency-Key and Problem" \
  bash -c "grep -q 'operationId: place_order' '$CH03/openapi-place-order.out' &&
           grep -q 'name: Idempotency-Key' '$CH03/openapi-place-order.out' &&
           grep -q 'application/problem+json' '$CH03/openapi-place-order.out'"
check "ch03: tools come from openapi.json; the host owns Idempotency-Key" \
  bash -c "cd ch03-rest-apis && uv run -q python -c \"
import json, openapi_tools as t
assert json.load(open('openapi.json'))['openapi'].startswith('3.1'), 'oas'
names = [x['name'] for x in t.TOOLS]
assert names == ['search_catalog', 'get_book', 'list_reviews',
                 'place_order', 'get_order_status', 'get_cancellation',
                 'cancel_order'], names
po = t.for_model(next(x for x in t.TOOLS if x['name'] == 'place_order'))
assert 'Idempotency-Key' not in json.dumps(po['input_schema']), po
assert po['input_schema']['required'] == ['customer_id', 'lines']
assert set(po) == {'name', 'description', 'input_schema'}
ask = [x['name'] for x in t.TOOLS if x['x-harbor']['needs_approval']]
assert ask == ['place_order', 'cancel_order'], ask
import harbor_api as h, re as r
assert h.store.keys == {}, 'keys start empty'
k = '6f1c2a9e-3b4d-4e5f-8a7b-9c0d1e2f3a4b'
assert r.fullmatch(h.KEY_PATTERN, chr(34) + k + chr(34))
assert not r.fullmatch(h.KEY_PATTERN, k)
\""
check "ch03: weak ETags match, next URLs are encoded, keys must be UUIDs" \
  bash -c "cd ch03-rest-apis && uv run -q python -c \"
from fastapi.testclient import TestClient
import harbor_api as h
c = TestClient(h.app)
etag = c.get('/books/bk-1001').headers['etag']
assert c.get('/books/bk-1001',
             headers={'If-None-Match': 'W/' + etag}).status_code == 304
assert c.get('/books?limit=1&q=the ').json()['next'].endswith('q=the+')
body = {'customer_id': 'cus-001',
        'lines': [{'book_id': 'bk-1002', 'quantity': 1}]}
bad = chr(34) + 'a' * 16 + chr(34)
r = c.post('/orders', json=body, headers={'Idempotency-Key': bad})
assert r.status_code == 400, r.status_code
\""

# --- Chapter 4 ---------------------------------------------------------
CH04="$TMP/ch04"
check "ch04: run.sh completes" env OUT="$CH04" ch04-rpc-apis/run.sh
check "ch04: reflection describes OrderService and its NO_SIDE_EFFECTS reads" \
  bash -c "grep -qx 'harbor.orders.v1.OrderService' '$CH04/grpcurl-list.out' &&
           [ \$(grep -c 'option idempotency_level = NO_SIDE_EFFECTS;' '$CH04/grpcurl-describe.out') -eq 2 ]"
check "ch04: gRPC GetOrderStatus and NOT_FOUND with a hint" \
  bash -c "grep -q '\"status\": \"ORDER_STATUS_SHIPPED\"' '$CH04/grpcurl-get-status.out' &&
           grep -q 'Code: NotFound' '$CH04/grpcurl-not-found.out' &&
           grep -q 'Order IDs run from ord-5001 to ord-5004.' '$CH04/grpcurl-not-found.out'"
check "ch04: a missed deadline creates no order; the retry does, once" \
  bash -c "grep -q 'Code: DeadlineExceeded' '$CH04/grpcurl-deadline.out' &&
           [ \$(grep -c 'PlaceOrder DEADLINE_EXCEEDED order not created' '$CH04/grpc-server-log.txt') -eq 2 ] &&
           grep -qx 'PlaceOrder OK new ord-5005' '$CH04/grpc-server-log.txt' &&
           grep -qx 'PlaceOrder OK replay ord-5005' '$CH04/grpc-server-log.txt' &&
           cmp -s '$CH04/grpcurl-place-order.out' '$CH04/grpcurl-place-order-retry.out'"
check "ch04: generated client prints the deadline error, then the order" \
  bash -c "grep -qx 'DEADLINE_EXCEEDED after 0.1s: Deadline Exceeded' '$CH04/client.out' &&
           grep -qx 'ord-5006: ORDER_STATUS_PLACED, 3797 cents' '$CH04/client.out'"
check "ch04: WatchOrder streams placed, paid, shipped" \
  bash -c "[ \"\$(grep -o 'ORDER_STATUS_[A-Z]*' '$CH04/grpcurl-watch.out' | tr '\n' ' ')\" = 'ORDER_STATUS_PLACED ORDER_STATUS_PAID ORDER_STATUS_SHIPPED ' ]"
check "ch04: JSON-RPC result, -32602, -32700, and a batch of two" \
  bash -c "grep -q '\"result\":{\"order_id\":\"ord-5005\"' '$CH04/jsonrpc-place-order.out' &&
           grep -q '\"code\":-32602' '$CH04/jsonrpc-bad-params.out' &&
           grep -q '\"code\":-32700,\"message\":\"Parse error\"},\"id\":null' '$CH04/jsonrpc-parse-error.out' &&
           head -1 '$CH04/jsonrpc-batch.out' | python3 -c 'import json,sys; r=json.load(sys.stdin); assert [x[\"id\"] for x in r]==[\"a\",\"b\"] and r[1][\"error\"][\"code\"]==-32601'"
check "ch04: a placeOrder notification gets 204 and still places an order" \
  bash -c "[ \"\$(tr -d '\n' < '$CH04/jsonrpc-notification.out')\" = 204 ] &&
           grep -qx 'http placeOrder notification ok new ord-5006' '$CH04/jsonrpc-server-log.txt'"
check "ch04: stdio carries server notifications after the response" \
  bash -c "grep -q '\"method\":\"orderStatusChanged\",\"params\":{\"order_id\":\"ord-5005\",\"status\":\"paid\"}' '$CH04/jsonrpc-stdio.out' &&
           grep -q '\"status\":\"shipped\"' '$CH04/jsonrpc-stdio.out'"
check "ch04: handle() follows the spec for notification-only and empty batches" \
  bash -c "cd ch04-rpc-apis && uv run -q python -c \"
import json, harbor_jsonrpc as j
T = j.HttpTransport
n = {'jsonrpc': '2.0', 'method': 'getOrderStatus',
     'params': {'order_id': 'ord-5002'}}
assert j.handle(json.dumps([n, n]), T) is None
assert json.loads(j.handle('[]', T))['error']['code'] == -32600
assert json.loads(j.handle(json.dumps({'jsonrpc': '2.0', 'method': 'x',
                                        'id': 7}), T))['id'] == 7
\""
check "ch04: tRPC client, 404 without a stack, and the type checks" \
  bash -c "grep -qx 'ord-5005 placed 3797' '$CH04/trpc-client.out' &&
           grep -q '\"code\":-32004' '$CH04/trpc-not-found.out' &&
           ! grep -q stack '$CH04/trpc-not-found.out' &&
           [ ! -s '$CH04/trpc-typecheck.out' ] &&
           grep -q 'error TS2322' '$CH04/trpc-typecheck-bad.out' &&
           grep -q 'error TS2561' '$CH04/trpc-typecheck-bad.out'"
check "ch04: payload sizes match the chapter's table" \
  python3 - "$CH04/payload-sizes.json" <<'PY'
import json, sys
rows = {(r["call"], r["style"]): r
        for r in json.load(open(sys.argv[1]))["rows"]}
want = {("GetOrderStatus", "SOAP"): (740, 466, 1520),
        ("GetOrderStatus", "REST"): (0, 388, 577),
        ("GetOrderStatus", "gRPC"): (10, 28, 134),
        ("GetOrderStatus", "JSON-RPC"): (83, 147, 469),
        ("GetOrderStatus", "tRPC"): (0, 133, 432),
        ("PlaceOrder", "REST"): (134, 461, 919),
        ("PlaceOrder", "gRPC"): (73, 69, 238),
        ("PlaceOrder", "JSON-RPC"): (213, 298, 751),
        ("PlaceOrder", "tRPC"): (157, 284, 747)}
for key, (req, resp, wire) in want.items():
    r = rows[key]
    got = (r["request_message"], r["response_message"], r["wire_median"])
    assert got == (req, resp, wire), (key, got)
PY

# --- Chapter 5 ---------------------------------------------------------
CH05="$TMP/ch05"
check "ch05: run.sh completes" env OUT="$CH05" ch05-graphql-apis/run.sh
check "ch05: pinned SDL and trusted documents match the code" \
  bash -c "cd ch05-graphql-apis && git diff --quiet -- schema.graphql trusted-documents.json &&
           grep -qx 'served schema matches the pinned schema.graphql' '$CH05/introspect.out' &&
           grep -q '^introspection: 29184 bytes, 23 types$' '$CH05/introspect.out'"
check "ch05: OrderScreen returns ord-5002 with both books, by text and by ID" \
  bash -c "tail -1 '$CH05/order-screen.out' | grep -q '\"title\":\"Frankenstein\"' &&
           tail -1 '$CH05/order-screen.out' | grep -q '\"title\":\"Dracula\"' &&
           [ \"\$(tail -1 '$CH05/order-screen.out' | wc -c)\" -eq 324 ] &&
           cmp -s <(tail -1 '$CH05/order-screen.out' | tr -d '\n') <(tail -1 '$CH05/trusted-id.out' | tr -d '\n') &&
           grep -q 'Content-Length: 418' '$CH05/order-screen.err'"
check "ch05: errors arrive inside 200 OK" \
  bash -c "head -1 '$CH05/partial-error.out' | grep -q '200 OK' &&
           grep -q '\"b\":null' '$CH05/partial-error.out' &&
           grep -q '\"hint\":\"Order IDs run from ord-5001 to ord-5004.\"' '$CH05/partial-error.out' &&
           head -1 '$CH05/validation-error.out' | grep -q '200 OK' &&
           grep -q \"Did you mean 'status'?\" '$CH05/validation-error.out'"
check "ch05: depth and cost limits refuse the attack queries" \
  bash -c "grep -q \"'anonymous' exceeds maximum operation depth of 6\" '$CH05/too-deep.out' &&
           grep -q 'Query cost 130051 exceeds the limit of 1000.' '$CH05/too-costly.out' &&
           grep -qx 'OrderScreen      cost 115' '$CH05/costs.out' &&
           grep -qx 'CatalogRatings   cost 651' '$CH05/costs.out'"
check "ch05: N+1 without loaders, batched with them" \
  bash -c "grep -qx 'POST /graphql 200 op=CatalogRatings backend_calls=9 list_books=1 reviews_for=8' '$CH05/server-log-no-loaders.txt' &&
           grep -qx 'POST /graphql 200 op=CatalogRatings backend_calls=2 list_books=1 reviews_for_many=1' '$CH05/server-log.txt' &&
           grep -q 'op=OrderScreen backend_calls=5' '$CH05/server-log-no-loaders.txt' &&
           grep -q 'op=OrderScreen backend_calls=3' '$CH05/server-log.txt' &&
           cmp -s <(tail -1 '$CH05/catalog-ratings-batched.out') <(tail -1 '$CH05/catalog-ratings-n-plus-1.out')"
check "ch05: trusted-only refuses text, introspection can be turned off" \
  bash -c "head -1 '$CH05/trusted-text-refused.out' | grep -q '400 Bad Request' &&
           grep -q 'This server runs only trusted documents; send a documentId.' '$CH05/trusted-text-refused.out' &&
           grep -q 'GraphQL introspection has been disabled' '$CH05/introspection-off.out'"
check "ch05: placeOrder retry with the same requestId returns ord-5005" \
  bash -c "grep -q '\"id\":\"ord-5005\",\"status\":\"PLACED\",\"totalCents\":3797' '$CH05/place-order.out' &&
           cmp -s '$CH05/place-order.out' '$CH05/place-order-retry.out'"
check "ch05: orderStatus subscription streams placed, paid, shipped" \
  bash -c "cd ch05-graphql-apis && uv run -q python -W ignore -c \"
import asyncio, harbor_graphql as h
async def main():
    gen = await h.build_schema().subscribe(
        'subscription { orderStatus(orderId: \\\"ord-5003\\\") { status } }')
    return [r.data['orderStatus']['status'] async for r in gen]
assert asyncio.run(main()) == ['PLACED', 'PAID', 'SHIPPED']
\""
check "ch05: order screen sizes match the chapter's table" \
  python3 - "$CH05/order-screen-sizes.json" <<'PY'
import json, sys
rows = {r["name"]: r for r in json.load(open(sys.argv[1]))["rows"]}
want = {"REST, five GETs": (5, 984, 802, 36, 2041),
        "GraphQL, query text": (1, 324, 324, 11, 968),
        "GraphQL, document ID": (1, 324, 324, 11, 710)}
for name, exp in want.items():
    r = rows[name]
    got = (r["requests"], r["body_bytes"], r["compact_bytes"],
           r["leaf_values"], r["wire_median"])
    assert got == exp, (name, got)
PY
check "ch05: curated tools from trusted documents, place_order asks first" \
  bash -c "grep -q '^  place_order      sha256:90fda13f8339...  ask first$' '$CH05/tools.out' &&
           grep -q '^  order_screen     sha256:50d69ec78f7e...$' '$CH05/tools.out' &&
           grep -q '^  run_graphql      queries only, depth <= 6, cost <= 1000$' '$CH05/tools.out'"
check "ch05: run_graphql's host check refuses a mutation, and tool sizes match" \
  bash -c "grep -qx 'not sent: run_graphql runs queries only, and this document holds a mutation' '$CH05/check-mutation.out' &&
           grep -qx 'sent: queries only' '$CH05/check-query.out' &&
           grep -q 'schema.graphql (SDL)         1,699 bytes' '$CH05/tool-sizes.out' &&
           grep -q 'three curated tools (JSON)   1,063 bytes' '$CH05/tool-sizes.out'"

# --- Chapter 6 ---------------------------------------------------------
CH06="$TMP/ch06"
check "ch06: run.sh completes" env OUT="$CH06" ch06-streaming-and-event-apis/run.sh
check "ch06: webhook retried after 503, replay deduplicated, forgery and stale rejected" \
  bash -c "grep -Eq '^503 msg_[0-9a-f]{24} \(receiver busy, stand-in\)$' '$CH06/receiver.log' &&
           grep -Eq '^204 msg_[0-9a-f]{24} processed order.shipped ord-5003$' '$CH06/receiver.log' &&
           grep -Eq '^204 msg_[0-9a-f]{24} duplicate, already processed$' '$CH06/receiver.log' &&
           grep -qx '401 rejected: no signature matches' '$CH06/receiver.log' &&
           grep -qx '401 rejected: timestamp over 300 seconds from now' '$CH06/receiver.log' &&
           [ \"\$(grep -c 'msg_' '$CH06/receiver.log')\" -eq 3 ] &&
           [ \"\$(awk '{print \$2}' '$CH06/receiver.log' | grep msg_ | sort -u | wc -l)\" -eq 1 ] &&
           grep -qx 'attempt 2 after 1s: 204' '$CH06/sender.out'"
check "ch06: signature matches Standard Webhooks' construction" \
  bash -c "cd ch06-streaming-and-event-apis && uv run -q python -c \"
import base64, hashlib, hmac, webhooks
mac = hmac.new(b'k' * 24, b'msg_1.1700000000.{}', hashlib.sha256).digest()
s = 'whsec_' + base64.b64encode(b'k' * 24).decode()
assert webhooks.sign(s, 'msg_1', '1700000000', b'{}') == 'v1,' + base64.b64encode(mac).decode()
h = {'webhook-id': 'msg_1', 'webhook-timestamp': '1700000000',
     'webhook-signature': 'v1,bad v1,' + base64.b64encode(mac).decode()}
assert webhooks.verify(s, h, b'{}', now=1700000100) == 'msg_1'
\""
check "ch06: malformed headers fail verification instead of crashing" \
  bash -c "cd ch06-streaming-and-event-apis && uv run -q python -c \"
import webhooks
for h in ({'webhook-id': 'm', 'webhook-timestamp': '²', 'webhook-signature': 'v1,x'},
          {'webhook-id': 'm', 'webhook-timestamp': '1700000000', 'webhook-signature': 'v1,é'},
          {'webhook-id': 'm', 'webhook-timestamp': '0' * 5000 + '1', 'webhook-signature': 'v1,x'}):
    try:
        webhooks.verify(webhooks.SECRET, h, b'{}', now=1700000000)
    except webhooks.InvalidWebhook:
        pass
    else:
        raise SystemExit('accepted a malformed delivery')
\""
check "ch06: sender refuses non-https and non-public webhook targets" \
  bash -c "grep -qx 'refused https://169.254.169.254/latest/meta-data/: 169.254.169.254 resolves to 169.254.169.254, which is not a public address' '$CH06/unsafe-targets.out' &&
           grep -q '^refused https://127.0.0.1:8061/hooks/harbor: .*not a public address$' '$CH06/unsafe-targets.out' &&
           grep -qx 'refused http://hooks.example.com/harbor: http:// is not https' '$CH06/unsafe-targets.out'"
check "ch06: WebSocket upgrade from a foreign Origin gets 403" \
  grep -qx 'HTTP/1.1 403 Forbidden' "$CH06/websocket-bad-origin.out"
check "ch06: AsyncAPI document valid, delivery matches it" \
  bash -c "grep -qx '../asyncapi.yaml: valid AsyncAPI 3.1.0, operations: sendOrderShipped (send on order.shipped)' '$CH06/asyncapi-validate.out' &&
           grep -q 'matches order.shipped in AsyncAPI 3.1.0' '$CH06/contract.out'"
check "ch06: SSE resumes after Last-Event-ID 2 with event 3 only" \
  bash -c "grep -qx 'content-type: text/event-stream; charset=utf-8' '$CH06/sse-first.out' &&
           grep -qx 'x-accel-buffering: no' '$CH06/sse-first.out' &&
           grep -qx 'id: 2' '$CH06/sse-first.out' && ! grep -qx 'id: 3' '$CH06/sse-first.out' &&
           grep -qx 'id: 3' '$CH06/sse-resume.out' && ! grep -qx 'id: 2' '$CH06/sse-resume.out' &&
           grep -qx 'event: order.shipped' '$CH06/sse-resume.out'"
check "ch06: WebSocket upgrade, masked client frames, close 1000" \
  bash -c "grep -qx 'HTTP/1.1 101 Switching Protocols' '$CH06/websocket.out' &&
           grep -qx 'Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=' '$CH06/websocket.out' &&
           grep -qx 'client -> server  FIN=1 opcode=0x1 (text) MASK=1 len=38' '$CH06/websocket.out' &&
           grep -q 'server -> client  FIN=1 opcode=0x1 (text) MASK=0 len=98' '$CH06/websocket.out' &&
           [ \"\$(grep -c 'close code 1000' '$CH06/websocket.out')\" -eq 2 ]"

# --- Chapter 7 ---------------------------------------------------------
# Needs Docker with Compose v2 (images apache/polaris:1.8.0 and
# rustfs/rustfs:1.0.5); takes about three minutes.
CH07="$TMP/ch07"
check "ch07: run.sh completes" env OUT="$CH07" ch07-data-standards/run.sh
check "ch07: catalog config names the harbor prefix and lists endpoints" \
  python3 -c "
import json, sys
d = json.load(open('$CH07/config.json'))
assert d['overrides']['prefix'] == 'harbor'
assert 'POST /v1/{prefix}/namespaces/{namespace}/tables/{table}' in d['endpoints']"
check "ch07: load table vends table-scoped credentials, secrets redacted" \
  bash -c "grep -q '> X-Iceberg-Access-Delegation: vended-credentials' '$CH07/load-table.err' &&
           grep -q '\"s3.session-token\": \"<s3.session-token>\"' '$CH07/load-table.json' &&
           grep -q 'tables/order_lines/credentials' '$CH07/load-table.json' &&
           ! grep -rqE 'Bearer [A-Za-z0-9._-]{12,}' '$CH07'"
check "ch07: history loads in two commits (6,656 and 6 lines)" \
  bash -c "grep -q ' append 6656 rows$' '$CH07/load.out' && grep -q ' append 6 rows$' '$CH07/load.out'"
check "ch07: stale commit gets 409 with assert-ref-snapshot-id, then retries" \
  bash -c "grep -q '409  /api/catalog/v1/harbor/namespaces/harbor/tables/order_lines' '$CH07/commits.out' &&
           grep -q 'Requirement failed: branch main has changed' '$CH07/commit-4.json' &&
           grep -q '\"type\": \"assert-ref-snapshot-id\"' '$CH07/commit-4.json' &&
           grep -q 'retrying (1/4)' '$CH07/conflict.out' &&
           grep -q '^writer B committed snapshot' '$CH07/conflict.out'"
check "ch07: Flight SQL query returns 2,916 rows in batches of 2,048 and 868" \
  bash -c "grep -qx 'total_records: 2916  total_bytes: 127213' '$CH07/flight-client.out' &&
           grep -qx 'DoGet batch 1: 2048 rows' '$CH07/flight-client.out' &&
           grep -qx 'DoGet batch 2: 868 rows' '$CH07/flight-client.out'"
check "ch07: ADBC aggregate returns 8 books, bk-1003 first" \
  bash -c "grep -qx 'Table 8 rows' '$CH07/adbc-client.out' &&
           grep -qx 'bk-1003   153    351747' '$CH07/adbc-client.out'"
check "ch07: agent tool result is 512 bytes against 173,831 of raw JSON" \
  bash -c "grep -qx 'tool result: 512 bytes of JSON, 8 rows' '$CH07/agent-tool.out' &&
           grep -qx 'raw lines:   874 rows, 173,831 bytes of JSON, 65,808 bytes as Arrow IPC' '$CH07/agent-tool.out'"
check "ch07: agent principal reads, catalog and store refuse its writes" \
  bash -c "grep -qx 'load table: ok, 6665 rows read' '$CH07/agent-access.out' &&
           grep -q 'is not authorized' '$CH07/agent-access.out' &&
           grep -q 'ACCESS_DENIED' '$CH07/agent-access.out'"

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
