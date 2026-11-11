#!/usr/bin/env bash
# Runs every Chapter 1 listing against a local Harbor REST server and
# writes the output to captures/. Needs curl and uv. No network access
# beyond 127.0.0.1, no API keys.
set -euo pipefail
cd "$(dirname "$0")"
PORT=8000
OUT="${OUT:-captures}"
mkdir -p "$OUT"

uv sync -q
uv run python harbor_rest.py --port "$PORT" 2> "$OUT/server-log.txt" &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT

for _ in $(seq 1 50); do
  (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null && break
  sleep 0.1
done

curl -sv "http://127.0.0.1:$PORT/books/bk-1001" \
  > "$OUT/curl-get-book.out" 2> "$OUT/curl-get-book.err"
curl -s -w '%{http_code}\n' "http://127.0.0.1:$PORT/books/bk-9999" \
  > "$OUT/curl-get-missing-book.out"
uv run python client.py bk-1001 > "$OUT/client.out"
uv run python agent.py > "$OUT/agent.out"

kill "$SERVER"; wait "$SERVER" 2>/dev/null || true
trap - EXIT
echo "captures written to $OUT/"
