#!/usr/bin/env bash
# Runs every Chapter 9 listing and writes the output to captures/.
# Needs uv. Starts the stand-in payment provider on 127.0.0.1:8091 and
# Harbor's payment service on 127.0.0.1:8009. No network access beyond
# 127.0.0.1 after `uv sync`, no account, and every key is a placeholder.
set -euo pipefail
cd "$(dirname "$0")"
OUT="${OUT:-captures}"
mkdir -p "$OUT"
uv sync -q
PIDS=()
trap 'for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null || true; done' EXIT

start() {  # start <port> <log file> <command...>
  local port=$1 log=$2; shift 2
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    echo "port $port is already in use; stop that server first" >&2
    exit 1
  fi
  "$@" 2> "$OUT/$log" &
  PIDS+=($!)
  for _ in $(seq 1 100); do
    (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null && return
    sleep 0.1
  done
}

# The provider holds its first two charge responses for 1.5 seconds,
# longer than Harbor's 1-second client timeout, to lose two responses.
start 8091 provider.log uv run python provider.py --slow-first 2
start 8009 harbor.log uv run python harbor_shop.py

# 1. Checkout through the provider: tokenize, charge with a key, retry,
#    webhook, the same failure without a key, key misuse, refund twice.
uv run python checkout_demo.py --dump "$OUT/charge.http" \
  > "$OUT/checkout.out"

# 2. An agent checkout under ACP, and what the allowance refuses.
uv run python acp_demo.py --dump-dir "$OUT" > "$OUT/acp.out"

# 3. A paid request under x402, then a replay and a tampered payment.
uv run python x402_demo.py --dump "$OUT/x402.http" > "$OUT/x402.out"

# 4. Every ACP message sent or received matches the pinned contract.
uv run python check_contract.py "$OUT" > "$OUT/contract.out"

echo "captures written to $OUT/"
