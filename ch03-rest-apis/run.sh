#!/usr/bin/env bash
# Runs every Chapter 3 listing against a local Harbor REST API and writes
# the output to captures/. Needs curl and uv. No network access beyond
# 127.0.0.1, no API keys.
set -euo pipefail
cd "$(dirname "$0")"
PORT=8003
B="http://127.0.0.1:$PORT"
OUT="${OUT:-captures}"
KEY='"6f1c2a9e-3b4d-4e5f-8a7b-9c0d1e2f3a4b"'
mkdir -p "$OUT"

uv sync -q
uv run python harbor_api.py --port "$PORT" 2> "$OUT/server-log.txt" &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT

for _ in $(seq 1 100); do
  (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null && break
  sleep 0.1
done

cap() {  # cap <name> <curl args...>: body to .out, wire trace to .err
  local name=$1; shift
  curl -sv "$@" > "$OUT/$name.out" 2> "$OUT/$name.err"
}

# Reading: a book, the same book conditionally, a missing book, pages.
cap get-book "$B/books/bk-1001"
ETAG=$(grep -i '< etag:' "$OUT/get-book.err" | cut -d' ' -f3 | tr -d '\r')
cap get-book-conditional -H "If-None-Match: $ETAG" "$B/books/bk-1001"
cap get-book-missing "$B/books/bk-9999"
cap search-limit-5000 "$B/books?limit=5000"
cap search-page "$B/books?limit=2"
cap list-reviews "$B/books/bk-1001/reviews"

# Writing: place an order, retry it with the same key, misuse the key.
cap place-order -X POST "$B/orders" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $KEY" --data-binary @requests/place-order.json
cap place-order-retry -X POST "$B/orders" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $KEY" --data-binary @requests/place-order.json
cap place-order-key-reused -X POST "$B/orders" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: $KEY" \
  --data-binary @requests/place-order-changed.json
cap place-order-no-key -X POST "$B/orders" \
  -H 'Content-Type: application/json' \
  --data-binary @requests/place-order.json
cap place-order-out-of-stock -X POST "$B/orders" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: "0b7e4c1d-5a2f-4c3e-9d8b-7a6f5e4d3c2b"' \
  --data '{"customer_id": "cus-002", "lines": [{"book_id": "bk-1004", "quantity": 1}]}'
cap get-order "$B/orders/ord-5005"

# Cancelling is a PUT, so repeating it is safe.
cap cancel-order -X PUT "$B/orders/ord-5003/cancellation" \
  -H 'Content-Type: application/json' \
  --data '{"reason": "customer_request"}'
cap cancel-order-again -X PUT "$B/orders/ord-5003/cancellation" \
  -H 'Content-Type: application/json' \
  --data '{"reason": "customer_request"}'
cap cancel-shipped -X PUT "$B/orders/ord-5002/cancellation" \
  -H 'Content-Type: application/json' \
  --data '{"reason": "customer_request"}'

# HTTP's own errors, as problem details.
cap delete-book -X DELETE "$B/books/bk-1001"
cap accept-xml -H 'Accept: application/xml' "$B/books/bk-1001"

# The contract: the server's document must match the pinned openapi.json.
curl -s "$B/openapi.json" > "$OUT/served-openapi.json"
if uv run python -c "
import json, sys
served = json.load(open(sys.argv[1]))
pinned = json.load(open('openapi.json'))
sys.exit(0 if served == pinned else 1)" "$OUT/served-openapi.json"; then
  echo "served /openapi.json matches the pinned openapi.json" \
    > "$OUT/openapi-drift.out"
else
  echo "DRIFT: served /openapi.json differs from openapi.json" \
    > "$OUT/openapi-drift.out"
fi
rm "$OUT/served-openapi.json"

uv run python show_operation.py post /orders > "$OUT/openapi-place-order.out"
uv run python openapi_tools.py > "$OUT/tools.out"
uv run python openapi_tools.py place_order > "$OUT/tool-place-order.out"

kill "$SERVER"; wait "$SERVER" 2>/dev/null || true
trap - EXIT
echo "captures written to $OUT/"
