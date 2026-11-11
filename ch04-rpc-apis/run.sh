#!/usr/bin/env bash
# Runs every Chapter 4 listing and writes the output to captures/:
# Harbor's order service over gRPC (127.0.0.1:8041), JSON-RPC 2.0 over
# HTTP (127.0.0.1:8042) and stdio, and tRPC (127.0.0.1:8043), then the
# payload comparison across Chapters 2 to 4. Needs uv, Node 24, curl, and
# grpcurl 1.9.4. No network access beyond 127.0.0.1, no API keys.
set -euo pipefail
cd "$(dirname "$0")"
OUT="${OUT:-captures}"
GRPCURL="${GRPCURL:-$(command -v grpcurl || echo "$HOME/go/bin/grpcurl")}"
RID='6f1c2a9e-3b4d-4e5f-8a7b-9c0d1e2f3a4b'
mkdir -p "$OUT"
PIDS=()
trap 'kill "${PIDS[@]}" 2>/dev/null || true' EXIT

wait_port() {
  for _ in $(seq 1 100); do
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && return
    sleep 0.1
  done
  echo "nothing listening on $1" >&2; exit 1
}

uv sync -q
(cd trpc && npm ci --silent --no-fund --no-audit)
rm -rf gen && mkdir gen
uv run python -m grpc_tools.protoc -Iproto --python_out=gen \
  --grpc_python_out=gen --pyi_out=gen proto/harbor/orders/v1/orders.proto

# --- gRPC ------------------------------------------------------------
uv run python harbor_grpc.py 2> "$OUT/grpc-server-log.txt" &
PIDS+=($!)
wait_port 8041
G=("$GRPCURL" -plaintext)
SVC=harbor.orders.v1.OrderService
ORDER="{\"customer_id\": \"cus-001\", \"lines\": [
  {\"book_id\": \"bk-1002\", \"quantity\": 1},
  {\"book_id\": \"bk-1008\", \"quantity\": 2}], \"request_id\": \"$RID\"}"
"${G[@]}" 127.0.0.1:8041 list > "$OUT/grpcurl-list.out"
"${G[@]}" 127.0.0.1:8041 describe $SVC > "$OUT/grpcurl-describe.out"
"${G[@]}" -d '{"order_id": "ord-5002"}' 127.0.0.1:8041 \
  $SVC/GetOrderStatus > "$OUT/grpcurl-get-status.out"
"${G[@]}" -d '{"order_id": "ord-9999"}' 127.0.0.1:8041 \
  $SVC/GetOrderStatus > "$OUT/grpcurl-not-found.out" 2>&1 || true
"${G[@]}" -max-time 0.1 -d "$ORDER" 127.0.0.1:8041 \
  $SVC/PlaceOrder > "$OUT/grpcurl-deadline.out" 2>&1 || true
sleep 0.3
"${G[@]}" -d "$ORDER" 127.0.0.1:8041 $SVC/PlaceOrder \
  > "$OUT/grpcurl-place-order.out"
"${G[@]}" -d "$ORDER" 127.0.0.1:8041 $SVC/PlaceOrder \
  > "$OUT/grpcurl-place-order-retry.out"
"${G[@]}" -d '{"order_id": "ord-5005"}' 127.0.0.1:8041 \
  $SVC/WatchOrder > "$OUT/grpcurl-watch.out"
uv run python client.py > "$OUT/client.out"
kill "${PIDS[0]}"; wait "${PIDS[0]}" 2>/dev/null || true

# --- JSON-RPC 2.0 over HTTP ------------------------------------------
uv run python harbor_jsonrpc.py 2> "$OUT/jsonrpc-server-log.txt" &
PIDS+=($!)
wait_port 8042
rpc() {  # rpc <name> <request file>: response body, then the status
  curl -s -w '\n%{http_code}\n' http://127.0.0.1:8042/rpc \
    -H 'Content-Type: application/json' --data-binary "@$2" \
    > "$OUT/jsonrpc-$1.out"
}
rpc place-order requests/place-order.json
rpc get-status requests/get-status.json
rpc not-found requests/not-found.json
rpc bad-params requests/bad-params.json
rpc parse-error requests/parse-error.txt
rpc batch requests/batch.json
rpc notification requests/notification.json
kill "${PIDS[1]}"; wait "${PIDS[1]}" 2>/dev/null || true

# --- JSON-RPC 2.0 over stdio: the same handler, another transport -----
uv run python harbor_jsonrpc.py --stdio < requests/stdio-session.jsonl \
  > "$OUT/jsonrpc-stdio.out" 2> "$OUT/jsonrpc-stdio-log.txt"

# --- tRPC --------------------------------------------------------------
(cd trpc && exec node server.ts) 2> "$OUT/trpc-server-log.txt" &
PIDS+=($!)
wait_port 8043
(cd trpc && node client.ts) > "$OUT/trpc-client.out"
curl -s -w '\n%{http_code}\n' 'http://127.0.0.1:8043/getOrderStatus?input=%7B%22order_id%22%3A%22ord-9999%22%7D' \
  > "$OUT/trpc-not-found.out"
curl -sv -X POST http://127.0.0.1:8043/placeOrder \
  -H 'Content-Type: application/json' \
  --data-binary @requests/trpc-place-order.json \
  > "$OUT/trpc-place-order.out" 2> "$OUT/trpc-place-order.err"
kill "${PIDS[2]}"; wait "${PIDS[2]}" 2>/dev/null || true
(cd trpc && npx tsc --noEmit -p .) > "$OUT/trpc-typecheck.out" 2>&1
(cd trpc && npx tsc --noEmit -p tsconfig.bad.json) \
  > "$OUT/trpc-typecheck-bad.out" 2>&1 || true

# --- The same order in five styles -----------------------------------
uv run python measure.py --runs 20 --out "$OUT" > /dev/null

trap - EXIT
echo "captures written to $OUT/"
