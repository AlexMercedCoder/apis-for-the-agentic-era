#!/usr/bin/env bash
# Runs every Chapter 11 listing and writes the output to captures/.
# Needs Docker (Compose v2), uv, and Node 24. Starts the test issuer on
# 127.0.0.1:8110, Chapter 3's API on 127.0.0.1:8003 and behind a token
# gate on 127.0.0.1:8111, Chapter 10's MCP server on 127.0.0.1:8010, and
# Apache APISIX 3.19.0 on 127.0.0.1:9080 in front of them.
# Takes about 90 seconds, most of it waiting out one rate-limit window.
set -euo pipefail
cd "$(dirname "$0")"
export OUT="${OUT:-captures}"
mkdir -p "$OUT/gateway"; chmod 777 "$OUT/gateway"
export GATEWAY_LOGS="$(cd "$OUT/gateway" && pwd)"
rm -f "$OUT"/gateway/*.jsonl "$OUT"/gateway/*.log
uv sync -q
(cd ../ch03-rest-apis && uv sync -q)
(cd governance && npm ci --silent)
PIDS=()
cleanup() {
  for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
  docker compose down -t 2 >/dev/null 2>&1 || true
}
trap cleanup EXIT
for port in 8003 8010 8110 8111 9080; do
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    echo "port $port is already in use; stop that server first" >&2; exit 1
  fi
done
wait_port() {
  for _ in $(seq 1 150); do
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && return 0; sleep 0.2
  done
  echo "nothing listening on $1" >&2; exit 1
}
strip() { sed "s#$(pwd)/governance/##g"; }

# 1. Governance: lint the pinned and the proposed contract, then diff them.
(cd governance && python3 propose_change.py >/dev/null &&
 cp ../../ch03-rest-apis/openapi.json pinned-openapi.json)
(cd governance && { npx spectral lint pinned-openapi.json \
   -r harbor-ruleset.yaml -f text; echo " (exit $?)"; }) 2>&1 | strip \
   > "$OUT/lint-pinned.out" || true
(cd governance && { npx spectral lint proposed-openapi.json \
   -r harbor-ruleset.yaml -f text; echo " (exit $?)"; }) 2>&1 | strip \
   > "$OUT/lint-proposed.out" || true
(cd governance && { npx spectral lint \
   ../../ch06-streaming-and-event-apis/asyncapi.yaml \
   -r asyncapi-ruleset.yaml -f text; echo " (exit $?)"; }) 2>&1 \
   | sed "s#$(cd .. && pwd)/##g" > "$OUT/lint-asyncapi.out" || true
{ docker run --rm -v "$(pwd)/governance:/specs:ro" tufin/oasdiff:v1.37.0 \
    breaking /specs/pinned-openapi.json /specs/proposed-openapi.json \
    --fail-on ERR; echo "exit $?"; } > "$OUT/breaking.out" 2>&1 || true

# 2. Harbor's servers and the gateway.
uv run -q python render.py
uv run -q python issuer.py serve & PIDS+=($!)
wait_port 8110
uv run -q python harbor_backend.py 2> "$OUT/backend.log" & PIDS+=($!)
wait_port 8003; wait_port 8111
uv run -q python mcp_server.py 2> "$OUT/mcp-server.log" & PIDS+=($!)
wait_port 8010
docker compose up -d >/dev/null 2>&1
wait_port 9080; sleep 2
GW=http://127.0.0.1:9080
STORE=$(uv run -q python issuer.py mint harbor-storefront)
AGENT=$(uv run -q python issuer.py mint harbor-assistant)

# 3. Per-caller policies, a deprecated path, and object-level checks.
{ echo "no token: $(curl -s -o /dev/null -w '%{http_code}' $GW/books/bk-1001)"
  echo "agent token on the REST route: $(curl -s -w ' %{http_code}' \
    -H "Authorization: Bearer $AGENT" $GW/books/bk-1001)"
  echo "storefront token:"
  curl -s -i -H "Authorization: Bearer $STORE" $GW/books/bk-1001 \
    | grep -iE '^(HTTP|x-ratelimit|server|x-request-id)'
} > "$OUT/gateway-callers.out"
curl -s -i -H "Authorization: Bearer $STORE" $GW/v0/books/bk-1001 \
  | grep -iE '^(HTTP|deprecation|sunset|link)' > "$OUT/deprecated.out"
{ echo "Chapter 3's API, called directly:"
  curl -s -w ' %{http_code}\n' http://127.0.0.1:8003/orders/ord-5002 \
    | cut -c1-60
  echo "Rosa's storefront token, through the gateway:"
  curl -s -w ' %{http_code}\n' -H "Authorization: Bearer $STORE" \
    $GW/orders/ord-5002
} > "$OUT/bola.out"

# 4. The agent's rate limit: 10 requests a minute, then 429.
call_book() {
  curl -s -D - -o /dev/null -X POST $GW/mcp \
    -H "Authorization: Bearer $AGENT" -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -H 'MCP-Protocol-Version: 2026-07-28' -H 'Mcp-Method: tools/call' \
    -H 'Mcp-Name: get_book' -d '{"jsonrpc": "2.0", "id": 1,
      "method": "tools/call", "params": {"name": "get_book",
      "arguments": {"book_id": "bk-1001"}, "_meta": {
      "io.modelcontextprotocol/protocolVersion": "2026-07-28",
      "io.modelcontextprotocol/clientCapabilities": {}}}}'
}
codes=""
for _ in $(seq 1 10); do
  codes+="$(call_book | head -1 | cut -d' ' -f2) "
done
echo "requests 1 to 10: $codes" > "$OUT/rate-limit.out"
call_book | grep -iE '^(HTTP|x-ratelimit|content-type)' \
  | tr -d '\r' >> "$OUT/rate-limit.out"
reset=$(grep -i x-ratelimit-reset "$OUT/rate-limit.out" | awk '{print $2}')
sleep "$(python3 -c "import math; print(math.ceil(float('$reset')) + 1)")"

# 5. The planted review, the controls, and the audit trail.
uv run -q python agent_host.py > "$OUT/agent-host.out"

# 6. Log injection: a path with an encoded newline.
EVIL='/books/bk-1001%0A%22DELETE%20/orders/ord-5001%20HTTP/1.1%22%20204'
EVIL+='%0A%22GET%20/books/bk-1001'
curl -s -o /dev/null http://127.0.0.1:8003$EVIL
curl -s -o /dev/null -H "Authorization: Bearer $STORE" $GW$EVIL
sleep 1

# 7. Inventory, from the contract, the backend, and the gateway's log.
uv run -q python inventory.py "$OUT/gateway/access.jsonl" \
  > "$OUT/inventory.out"
cp "$OUT/gateway/access.jsonl" "$OUT/gateway-access.jsonl"
if grep -rqE 'eyJ[A-Za-z0-9_-]{10,}' "$OUT" --exclude-dir=gateway; then
  echo "a token reached captures/" >&2; exit 1
fi
echo "captures written to $OUT/"
