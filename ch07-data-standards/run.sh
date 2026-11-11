#!/usr/bin/env bash
# Runs every Chapter 7 listing and writes the output to captures/.
# Needs Docker (Compose v2) and uv. Starts RustFS on 127.0.0.1:9000 and
# Apache Polaris on 127.0.0.1:8181 from a clean state, the logging proxy
# on 127.0.0.1:8180, and Harbor's Flight SQL server on 127.0.0.1:8071.
# Set KEEP=1 to leave the containers running afterward.
set -euo pipefail
cd "$(dirname "$0")"
OUT="${OUT:-captures}"
mkdir -p "$OUT"
uv sync -q
mkdir -p gen
uv run -q python -m grpc_tools.protoc -Iproto --python_out=gen \
  proto/FlightSql.proto
PIDS=()
cleanup() {
  for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
  if [ "${KEEP:-0}" != 1 ]; then docker compose down -v -t 2 -q 2>/dev/null \
    || docker compose down -v -t 2 >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT

for port in 8071 8180; do
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    echo "port $port is already in use; stop that server first" >&2
    exit 1
  fi
done
wait_port() {
  for _ in $(seq 1 200); do
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && return
    sleep 0.1
  done
  echo "nothing listening on $1" >&2; exit 1
}

# 1. A clean object store and catalog, a bucket, a catalog, an agent.
docker compose down -v -t 2 >/dev/null 2>&1 || true
docker compose up -d --wait >/dev/null 2>&1
rm -f agent-credentials.json
uv run -q python setup_catalog.py > "$OUT/setup.out"

# 2. Load the order history and race two writers, through the proxy.
rm -f "$OUT/catalog-log.jsonl"
uv run -q python logproxy.py "$OUT/catalog-log.jsonl" & PIDS+=($!)
wait_port 8180
PYICEBERG_MAX_WORKERS=2 uv run -q python load_orders.py > "$OUT/load.out"
uv run -q python setup_catalog.py --grant harbor.order_lines >> "$OUT/setup.out"
uv run -q python commit_conflict.py > "$OUT/conflict.out" 2>&1
uv run -q python extract_commits.py "$OUT/catalog-log.jsonl" "$OUT" \
  > "$OUT/commits.out"

# 3. The REST catalog with curl: config and a load with vended credentials.
./catalog_requests.sh "$OUT"

# 4. Flight SQL: the plain Flight client, ADBC, the agent's tool, and
#    the agent principal's limits.
uv run -q python flight_sql_server.py 2> "$OUT/flight-server.log" & PIDS+=($!)
wait_port 8071
uv run -q python flight_client.py > "$OUT/flight-client.out"
uv run -q python adbc_client.py > "$OUT/adbc-client.out"
uv run -q python agent_tool.py --definition > "$OUT/tool-definition.json"
uv run -q python agent_tool.py > "$OUT/agent-tool.out"
uv run -q python agent_access.py > "$OUT/agent-access.out"
echo "captures written to $OUT/"
