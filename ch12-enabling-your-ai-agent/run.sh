#!/usr/bin/env bash
# Runs every Chapter 12 listing and writes the output to captures/.
# Needs Docker (Compose v2) and uv. Starts, all on 127.0.0.1: the test
# issuer (8120), Chapter 5's GraphQL server with trusted documents only
# (8005), Chapter 9's payment-provider stand-in (8091), the refunds API
# (8123), the fulfillment agent (8124), the support MCP server (8122),
# Rosa's phone (a stand-in), and Apache APISIX 3.19.0 (9080) in front.
# Takes about three minutes, most of it waiting out rate-limit windows.
set -euo pipefail
cd "$(dirname "$0")"
export OUT="${OUT:-captures}"
mkdir -p "$OUT/gateway" state; chmod 777 "$OUT/gateway"
export GATEWAY_LOGS="$(cd "$OUT/gateway" && pwd)"
rm -f "$OUT"/gateway/*.jsonl "$OUT"/gateway/*.log "$OUT"/trace-*.jsonl \
  "$OUT"/audit.jsonl
uv sync -q
(cd ../ch05-graphql-apis && uv sync -q)
(cd ../ch09-payment-standards && uv sync -q)
export HARBOR_AUDIT_KEY=$(python3 -c \
  "import secrets; print(secrets.token_hex(32))")   # random per run
PIDS=()
STACK=()
cleanup() {
  for pid in "${PIDS[@]}" "${STACK[@]}"; do kill "$pid" 2>/dev/null || true
  done
  docker compose down -t 2 >/dev/null 2>&1 || true
}
trap cleanup EXIT
for port in 8005 8091 8120 8122 8123 8124 8125 9080; do
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
wait_free() {
  for _ in $(seq 1 50); do
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null || return 0; sleep 0.2
  done
}

# Harbor's services that hold per-run state: refunds, payments, cases.
start_stack() {
  rm -f state/damage-cases.json state/phone-mode
  uv run -q python payment_provider.py 2>> "$OUT/provider.log" &
  STACK+=($!)
  uv run -q python refunds_api.py "$@" 2>> "$OUT/refunds-api.log" &
  STACK+=($!)
  uv run -q python fulfillment_agent.py 2>> "$OUT/fulfillment.log" &
  STACK+=($!)
  uv run -q python phone.py rosa >> "$OUT/phone.log" &
  STACK+=($!)
  wait_port 8091; wait_port 8123; wait_port 8124
  uv run -q python seed_payment.py > /dev/null
  sleep 1
}
stop_stack() {
  for pid in "${STACK[@]}"; do kill "$pid" 2>/dev/null || true; done
  STACK=()
  wait_free 8091; wait_free 8123; wait_free 8124
}
: > "$OUT/provider.log"; : > "$OUT/refunds-api.log"
: > "$OUT/fulfillment.log"; : > "$OUT/phone.log"

uv run -q python render.py
uv run -q python issuer.py serve & PIDS+=($!)
wait_port 8120
(cd ../ch05-graphql-apis && exec uv run -q python harbor_graphql.py \
   --trusted-only 2> /dev/null) & PIDS+=($!)
uv run -q python support_mcp.py 2> "$OUT/support-mcp.log" & PIDS+=($!)
wait_port 8005; wait_port 8122
docker compose up -d >/dev/null 2>&1
wait_port 9080; sleep 2
start_stack --slow-first

# 1. The tools the model sees: two from MCP, one from the Agent Card.
uv run -q python host.py --print-tools > "$OUT/tools.json"
uv run -q python show_tools.py > "$OUT/tools.out"

# 2. The capstone: Rosa's damaged copy, end to end, with the trace.
uv run -q python host.py --fixtures fixtures-capstone.json \
  > "$OUT/capstone.out"
sleep 1
uv run -q python show_trace.py > "$OUT/trace.out"
uv run -q python show_trace.py --json > "$OUT/trace.json"
cp "$OUT/gateway/access.jsonl" "$OUT/gateway-access.jsonl"

# 3. A rate limit the host waits out: Sam's assistant, eleven reads.
uv run -q python rate_limit_demo.py > "$OUT/rate-limit.out"

# 4. The evaluation suite: each scenario on a fresh stack, then graded.
OTHER="Where's my order ord-5002? The Frankenstein copy was damaged."
for s in happy guess-case repeat declined not-theirs; do
  stop_stack
  start_stack
  args=(--scenario "$s" --fixtures "eval-$s.json")
  [ "$s" = declined ] && echo deny > state/phone-mode
  [ "$s" = not-theirs ] && args+=(--question "$OTHER")
  uv run -q python host.py "${args[@]}" > "$OUT/eval-$s.out"
done
uv run -q python eval_suite.py > "$OUT/eval.out"
uv run -q python replay_test.py "$OUT/fixtures-capstone.json" \
  > "$OUT/replay.out"

if grep -rqE 'eyJ[A-Za-z0-9_-]{10,}' "$OUT" --exclude-dir=gateway; then
  echo "a token reached captures/" >&2; exit 1
fi
echo "captures written to $OUT/"
