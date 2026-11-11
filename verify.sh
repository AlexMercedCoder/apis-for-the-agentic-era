#!/usr/bin/env bash
# Runs every example in this repository that needs no network access
# beyond 127.0.0.1 and no API keys, and checks the output the book prints.
# Usage: ./verify.sh    (needs bash, curl, and uv)
set -uo pipefail
cd "$(dirname "$0")"
ROOT=$(pwd)
PASS=0
FAIL=0
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

check() {
  local name=$1; shift
  if "$@" > "$TMP/last.log" 2>&1; then
    echo "[OK]   $name"; PASS=$((PASS + 1))
  else
    echo "[FAIL] $name"; sed 's/^/       /' "$TMP/last.log"
    FAIL=$((FAIL + 1))
  fi
}

# --- harbor-data: shared seed data -----------------------------------
check "harbor-data: JSON parses and order totals add up" \
  python3 - "$ROOT/harbor-data" <<'PY'
import json, sys
from pathlib import Path
d = Path(sys.argv[1])
books = {b["id"]: b for b in json.loads((d / "books.json").read_text())}
customers = {c["id"] for c in json.loads((d / "customers.json").read_text())}
for o in json.loads((d / "orders.json").read_text()):
    assert o["customer_id"] in customers, o["id"]
    assert o["status"] in {"placed", "paid", "shipped", "delivered",
                           "cancelled"}, o["id"]
    total = 0
    for line in o["lines"]:
        assert line["book_id"] in books, (o["id"], line["book_id"])
        total += line["quantity"] * line["unit_price_cents"]
    assert total == o["total_cents"], o["id"]
PY

# --- Chapter 1 ---------------------------------------------------------
CH01="$TMP/ch01"
check "ch01: run.sh completes" env OUT="$CH01" ch01-what-is-an-api/run.sh
check "ch01: curl gets 200 OK with JSON" \
  bash -c "grep -q '< HTTP/1.1 200 OK' '$CH01/curl-get-book.err' &&
           grep -q '< Content-Type: application/json' '$CH01/curl-get-book.err' &&
           grep -q '\"title\": \"Moby-Dick\"' '$CH01/curl-get-book.out'"
check "ch01: unknown book returns 404 with a hint" \
  bash -c "tail -1 '$CH01/curl-get-missing-book.out' | grep -qx 404 &&
           grep -q '\"error\": \"book_not_found\"' '$CH01/curl-get-missing-book.out'"
check "ch01: Python client prints the book" \
  grep -qx 'Moby-Dick by Herman Melville: \$14.99, 7 in stock' "$CH01/client.out"
check "ch01: stand-in emits a get_book tool_use block" \
  bash -c "grep -q '\"stop_reason\": \"tool_use\"' '$CH01/agent.out' &&
           grep -q '\"name\": \"get_book\"' '$CH01/agent.out' &&
           grep -q '\"is_error\": false' '$CH01/agent.out'"
check "ch01: all three callers sent the same request line" \
  bash -c "[ \$(grep -c '\"GET /books/bk-1001 HTTP/1.1\" 200' '$CH01/server.log') -eq 3 ]"

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
