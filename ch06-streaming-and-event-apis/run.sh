#!/usr/bin/env bash
# Runs every Chapter 6 listing and writes the output to captures/.
# Needs uv and Node 24 (for the AsyncAPI parser). Starts the webhook
# receiver on 127.0.0.1:8061 and the stream server on 127.0.0.1:8006.
# No network access beyond 127.0.0.1 after `uv sync` and `npm ci`,
# no API keys.
set -euo pipefail
cd "$(dirname "$0")"
OUT="${OUT:-captures}"
mkdir -p "$OUT"
uv sync -q
(cd validate && npm ci --silent)
SERVER=""
trap '[ -n "$SERVER" ] && kill $SERVER 2>/dev/null || true' EXIT

start() {  # start <port> <log file> <command...>
  local port=$1 log=$2; shift 2
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    echo "port $port is already in use; stop that server first" >&2
    exit 1
  fi
  "$@" 2> "$OUT/$log" &
  SERVER=$!
  for _ in $(seq 1 100); do
    (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null && return
    sleep 0.1
  done
}

stop() {
  kill "$SERVER"; wait "$SERVER" 2>/dev/null || true; SERVER=""
}

# 1. Webhooks: a delivery that fails once and is retried, then replayed,
#    a forgery, and a stale replay.
start 8061 receiver.log uv run python receiver.py --fail-first 1
uv run python sender.py --allow-local-http --dump "$OUT/delivery.http" --repeat \
  > "$OUT/sender.out"
uv run python sender.py --allow-local-http --once \
  --secret "whsec_$(printf 'an-attacker-guessed-this-key' | base64)" \
  > "$OUT/forged.out" || true
uv run python sender.py --allow-local-http --once --timestamp-offset -600 \
  > "$OUT/stale.out" || true
stop

# The sender refuses unsafe targets before sending anything.
{ uv run python sender.py --url https://169.254.169.254/latest/meta-data/
  uv run python sender.py --url https://127.0.0.1:8061/hooks/harbor
  uv run python sender.py --url http://hooks.example.com/harbor
} > "$OUT/unsafe-targets.out"

# 2. The contract: AsyncAPI parses, and the delivery matches it.
(cd validate && node validate.mjs ../asyncapi.yaml) \
  > "$OUT/asyncapi-validate.out"
uv run python check_contract.py "$OUT/delivery.http" > "$OUT/contract.out"

# 3. SSE: read to event 2, drop, reconnect with Last-Event-ID: 2.
start 8006 stream-server.log uv run python harbor_stream.py
uv run python sse_client.py ord-5003 --until 2 > "$OUT/sse-first.out"
sleep 0.5
uv run python sse_client.py ord-5003 --last-event-id 2 --until 3 \
  > "$OUT/sse-resume.out"
stop

# 4. WebSocket: the upgrade and the frames, on a fresh server.
start 8006 stream-server-ws.log uv run python harbor_stream.py
uv run python ws_raw.py > "$OUT/websocket.out"
uv run python ws_raw.py --origin https://evil.example \
  > "$OUT/websocket-bad-origin.out"
stop

echo "captures written to $OUT/"
