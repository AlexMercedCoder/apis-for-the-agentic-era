#!/usr/bin/env bash
# Runs every Chapter 8 listing and writes the output to captures/.
# Needs Docker (Compose v2) and uv. Starts Keycloak 26.8.2 on
# 127.0.0.1:8080 from a clean state, creates the harbor realm, starts
# Harbor's protected API on 127.0.0.1:8008, and runs the clients.
# Set KEEP=1 to leave Keycloak running afterward.
set -euo pipefail
cd "$(dirname "$0")"
OUT="${OUT:-captures}"
mkdir -p "$OUT"
uv sync -q
PIDS=()
cleanup() {
  for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
  if [ "${KEEP:-0}" != 1 ]; then
    docker compose down -v -t 2 >/dev/null 2>&1 || true
  fi
  rm -f tokens.json
}
trap cleanup EXIT

if (exec 3<>"/dev/tcp/127.0.0.1/8008") 2>/dev/null; then
  echo "port 8008 is already in use; stop that server first" >&2; exit 1
fi

# 1. A clean Keycloak and the harbor realm.
docker compose down -v -t 2 >/dev/null 2>&1 || true
docker compose up -d >/dev/null 2>&1
for _ in $(seq 1 120); do
  curl -sf http://127.0.0.1:8080/realms/master >/dev/null && break
  sleep 1
done
uv run -q python setup_realm.py > "$OUT/setup.out"
curl -s http://127.0.0.1:8080/realms/harbor/.well-known/openid-configuration \
  | python3 -m json.tool > "$OUT/discovery.json"

# 2. Harbor's protected API.
uv run -q python harbor_secured.py 2> "$OUT/api.log" & PIDS+=($!)
for _ in $(seq 1 100); do
  (exec 3<>"/dev/tcp/127.0.0.1/8008") 2>/dev/null && break; sleep 0.1
done
curl -s http://127.0.0.1:8008/.well-known/oauth-protected-resource \
  > "$OUT/protected-resource.json"

# 3. The flows. A reader token (orders:read only) first, then Rosa's.
uv run -q python code_flow.py rosa "openid orders:read" reader --quiet
uv run -q python code_flow.py rosa > "$OUT/code-flow.out"
uv run -q python code_flow.py rosa \
  "openid delegation:client:harbor-assistant" assistant-grant --quiet
uv run -q python refresh_demo.py > "$OUT/refresh.out"
uv run -q python call_api.py > "$OUT/call-api.out"
uv run -q python fulfillment.py > "$OUT/fulfillment.out"
uv run -q python agent_exchange.py > "$OUT/agent-exchange.out"
sleep 0.2
echo "captures written to $OUT/"
