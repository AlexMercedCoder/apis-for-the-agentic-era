#!/usr/bin/env bash
# Runs every Chapter 5 listing against a local Harbor GraphQL server and
# writes the output to captures/. Needs curl and uv. Starts the server
# four times (default, --no-loaders, --trusted-only, --no-introspection),
# then runs the REST-versus-GraphQL comparison, which also starts
# Chapter 3's REST API. No network access beyond 127.0.0.1, no API keys.
set -euo pipefail
cd "$(dirname "$0")"
PORT=8005
URL="http://127.0.0.1:$PORT/graphql"
OUT="${OUT:-captures}"
mkdir -p "$OUT"
uv sync -q
SERVER=""
trap '[ -n "$SERVER" ] && kill $SERVER 2>/dev/null || true' EXIT

start() {  # start <log name> [server options]
  local log=$1; shift
  uv run python harbor_graphql.py --port "$PORT" "$@" 2> "$OUT/$log" &
  SERVER=$!
  for _ in $(seq 1 100); do
    (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null && return
    sleep 0.1
  done
}

stop() {
  kill "$SERVER"; wait "$SERVER" 2>/dev/null || true; SERVER=""
}

post() {  # post <name> <body file>: status line and body to <name>.out
  curl -s -i -X POST "$URL" -H 'Content-Type: application/json' \
    --data-binary @"$2" | grep -v '^date:' > "$OUT/$1.out"
}

body() {  # body <file> <operation> <variables JSON> [--id]
  local file=$1; shift
  uv run python body.py "$@" > "$file"
}

TMP=$(mktemp -d)

# The contract: the pinned SDL, and the trusted documents' IDs.
uv run python harbor_graphql.py --export
uv run python persist.py > "$OUT/persist.out"
uv run python limits.py operations/*.graphql > "$OUT/costs.out"

# 1. Default server: DataLoader batching, depth and cost limits on.
start server-log.txt
body "$TMP/order-screen.json" OrderScreen '{"orderId": "ord-5002"}'
curl -sv -X POST "$URL" -H 'Content-Type: application/json' \
  --data-binary @"$TMP/order-screen.json" \
  > "$OUT/order-screen.out" 2> "$OUT/order-screen.err"
post partial-error requests/partial.json
post validation-error requests/typo.json
post too-deep requests/too-deep.json
post too-costly requests/too-costly.json
body "$TMP/ratings.json" CatalogRatings '{"first": 8}'
post catalog-ratings-batched "$TMP/ratings.json"
body "$TMP/place.json" PlaceOrder \
  "{\"input\": $(cat requests/place-order-input.json)}"
post place-order "$TMP/place.json"
post place-order-retry "$TMP/place.json"
uv run python introspect.py > "$OUT/introspect.out"
uv run python introspect.py --print > "$OUT/introspected-schema.graphql"
stop

# 2. Without loaders: the same CatalogRatings query, one call per book.
start server-log-no-loaders.txt --no-loaders
post catalog-ratings-n-plus-1 "$TMP/ratings.json"
body "$TMP/order-screen.json" OrderScreen '{"orderId": "ord-5002"}'
post order-screen-no-loaders "$TMP/order-screen.json"
stop

# 3. Trusted documents only: text is refused, a known ID runs.
start server-log-trusted.txt --trusted-only
post trusted-text-refused requests/partial.json
body "$TMP/order-screen-id.json" OrderScreen '{"orderId": "ord-5002"}' --id
cp "$TMP/order-screen-id.json" "$OUT/order-screen-id-request.json"
post trusted-id "$TMP/order-screen-id.json"
stop

# 4. Introspection off, as a first-party production server might run.
start server-log-no-introspection.txt --no-introspection
post introspection-off requests/schema-types.json
stop

# REST versus GraphQL for the order screen, and the tools for a model.
uv run python measure.py --out "$OUT" > "$OUT/measure.out"
uv run python graphql_tools.py > "$OUT/tools.out"
uv run python graphql_tools.py order_screen > "$OUT/tool-order-screen.out"
uv run python graphql_tools.py run_graphql > "$OUT/tool-run-graphql.out"
uv run python graphql_tools.py --sizes > "$OUT/tool-sizes.out"
uv run python graphql_tools.py --check "$(python3 -c 'import json; print(json.load(open("requests/agent-mutation.json"))["query"])')" > "$OUT/check-mutation.out"
uv run python graphql_tools.py --check "$(python3 -c 'import json; print(json.load(open("requests/partial.json"))["query"])')" > "$OUT/check-query.out"

rm -rf "$TMP"
echo "captures written to $OUT/"
