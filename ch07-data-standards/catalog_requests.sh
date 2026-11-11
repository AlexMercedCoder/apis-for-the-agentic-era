#!/usr/bin/env bash
# The Iceberg REST catalog with curl: a token, GET /v1/config, and a
# load-table request asking for vended credentials. Output goes to the
# directory in $1 with tokens and secrets replaced by placeholders.
set -euo pipefail
cd "$(dirname "$0")"
OUT=${1:-captures}
BASE=http://127.0.0.1:8181/api/catalog
TOKEN=$(curl -s "$BASE/v1/oauth/tokens" \
  -d grant_type=client_credentials -d client_id=root \
  -d client_secret=not-a-real-secret -d scope=PRINCIPAL_ROLE:ALL |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
curl -sv -H "Authorization: Bearer $TOKEN" \
  "$BASE/v1/config?warehouse=harbor" 2> "$OUT/config.err" |
  python3 -m json.tool | uv run -q python redact.py > "$OUT/config.json"
uv run -q python redact.py < "$OUT/config.err" > "$OUT/config.err.tmp"
mv "$OUT/config.err.tmp" "$OUT/config.err"
curl -sv -H "Authorization: Bearer $TOKEN" \
  -H "X-Iceberg-Access-Delegation: vended-credentials" \
  "$BASE/v1/harbor/namespaces/harbor/tables/order_lines" \
  2> "$OUT/load-table.err" |
  python3 -m json.tool | uv run -q python redact.py > "$OUT/load-table.json"
uv run -q python redact.py < "$OUT/load-table.err" > "$OUT/load-table.err.tmp"
mv "$OUT/load-table.err.tmp" "$OUT/load-table.err"
