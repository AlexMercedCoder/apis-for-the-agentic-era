#!/usr/bin/env bash
# Runs every Chapter 10 listing and writes the output to captures/.
# Needs Docker (Compose v2) and uv. Starts Keycloak 26.8.2 on
# 127.0.0.1:8080 from a clean state with the harbor realm, Chapter 3's REST
# API on 127.0.0.1:8003, Harbor's MCP server on 127.0.0.1:8010, Harbor's
# fulfillment agent (A2A) on 127.0.0.1:8011, and the shopping agent's push
# receiver on 127.0.0.1:8012, then runs the MCP client and the A2A hand-off.
# Set KEEP=1 to leave Keycloak running afterward.
set -euo pipefail
cd "$(dirname "$0")"
export OUT="${OUT:-captures}"
mkdir -p "$OUT"
uv sync -q
(cd ../ch03-rest-apis && uv sync -q)
PIDS=()
cleanup() {
  for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
  if [ "${KEEP:-0}" != 1 ]; then
    docker compose down -v -t 2 >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

for port in 8003 8010 8011 8012; do
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    echo "port $port is already in use; stop that server first" >&2; exit 1
  fi
done
wait_port() {
  for _ in $(seq 1 100); do
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && return 0; sleep 0.1
  done
  echo "nothing listening on $1" >&2; exit 1
}

# 1. Keycloak with the harbor realm (resource indicators on).
docker compose down -v -t 2 >/dev/null 2>&1 || true
docker compose up -d >/dev/null 2>&1
for _ in $(seq 1 120); do
  curl -sf http://127.0.0.1:8080/realms/master >/dev/null && break
  sleep 1
done
uv run -q python setup_realm.py > "$OUT/setup.out"

# 2. Chapter 3's REST API, fresh seed data, and the servers built on it.
(cd ../ch03-rest-apis && exec uv run -q python harbor_api.py) \
  > "$OUT/rest-api.log" 2>&1 & PIDS+=($!)
wait_port 8003
: > "$OUT/push-tokens.txt"; : > "$OUT/push-tasks.txt"
uv run -q python harbor_mcp.py 2> "$OUT/mcp-server.log" & PIDS+=($!)
uv run -q python fulfillment_agent.py 2> "$OUT/fulfillment.log" & PIDS+=($!)
uv run -q python push_receiver.py 2> "$OUT/push-receiver.log" & PIDS+=($!)
wait_port 8010; wait_port 8011; wait_port 8012

# 3. MCP: discovery, sign-in, and a session.
uv run -q python mcp_client.py > "$OUT/mcp-client.out"
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8010/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Authorization: Bearer not-a-real-token' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  > "$OUT/mcp-bad-token.out"

# 4. A2A: a hand-off that ships, one that's rejected, and a repeat; then
#    a push with a token the receiver never minted, and one with another
#    task's token.
uv run -q python shopping_agent.py ord-5003 > "$OUT/a2a-ship.out"
cp "$OUT/a2a-stream-ord-5003.txt" "$OUT/a2a-stream-ship.txt"
uv run -q python shopping_agent.py ord-5002 > "$OUT/a2a-reject.out"
uv run -q python shopping_agent.py ord-5003 > "$OUT/a2a-repeat.out"
sleep 0.5
FIRST=$(head -1 "$OUT/push-tokens.txt")
push() {
  curl -s -o /dev/null -w '%{http_code}\n' -X POST \
    http://127.0.0.1:8012/a2a/push -H 'Content-Type: application/json' \
    -H "X-A2A-Notification-Token: $1" \
    -d '{"statusUpdate":{"taskId":"forged-task","status":{"state":"TASK_STATE_COMPLETED"}}}'
}
{ echo "unknown token: $(push not-a-real-push-token)";
  echo "another task's token: $(push "$FIRST")"; } > "$OUT/push-forged.out"

# 5. The listings as printed in the chapter.
uv run -q python show.py > "$OUT/listings.txt"
echo "captures written to $OUT/"
