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
check "ch01: host rejects a book_id the schema forbids" \
  bash -c "cd ch01-what-is-an-api && uv run -q python -c \"
import agent
for bad in ('../admin', 'bk-1001\\n'):
    result, is_error = agent.run_tool('get_book', {'book_id': bad})
    assert is_error and result['error'] == 'invalid_argument', (bad, result)
\""
check "ch01: all three callers sent the same request line" \
  bash -c "[ \$(grep -c '\"GET /books/bk-1001 HTTP/1.1\" 200' '$CH01/server-log.txt') -eq 3 ]"

# --- Chapter 2 ---------------------------------------------------------
CH02="$TMP/ch02"
check "ch02: run.sh completes" env OUT="$CH02" ch02-soap-apis/run.sh
check "ch02: GetOrderStatus returns 200 with a shipped order" \
  bash -c "grep -q '< HTTP/1.1 200 OK' '$CH02/curl-get-order-status.err' &&
           grep -q '> SOAPAction: \"http://harbor.example/orders/GetOrderStatus\"' '$CH02/curl-get-order-status.err' &&
           grep -q '<h:status>shipped</h:status>' '$CH02/curl-get-order-status.out'"
check "ch02: unknown order is a soap:Client fault with a hint, as a 500" \
  bash -c "grep -q '< HTTP/1.1 500 Internal Server Error' '$CH02/curl-order-not-found.err' &&
           grep -q '<faultcode>soap:Client</faultcode>' '$CH02/curl-order-not-found.out' &&
           grep -q 'Order IDs run from ord-5001 to ord-5004.' '$CH02/curl-order-not-found.out'"
check "ch02: an unknown mustUnderstand header fails the message" \
  grep -q '<faultcode>soap:MustUnderstand</faultcode>' "$CH02/curl-must-understand.out"
check "ch02: GetOrderStatus without a UsernameToken is refused" \
  grep -q '<faultcode>wsse:FailedAuthentication</faultcode>' "$CH02/curl-no-token.out"
check "ch02: zeep reads both operations from the WSDL" \
  bash -c "grep -q 'GetOrderStatus(orderId: ns0:OrderId)' '$CH02/zeep-inspect.out' &&
           grep -q 'GetBook(bookId: ns0:BookId)' '$CH02/zeep-inspect.out'"
check "ch02: generated client prints the order and the fault hint" \
  bash -c "grep -qx 'ord-5002: shipped since 2026-11-03 09:41:52+00:00, 48.97 USD' '$CH02/client.out' &&
           grep -qx 'Hint: Order IDs run from ord-5001 to ord-5004.' '$CH02/client-not-found.out' &&
           grep -q 'does not match ord-\[0-9\]{4}' '$CH02/client-bad-id.out'"
check "ch02: every operation is a POST to one URL" \
  bash -c "[ \$(grep -c '\"POST /harbor/soap HTTP/1.1\"' '$CH02/server-log.txt') -eq 10 ] &&
           grep -q 'action=GetBook' '$CH02/server-log.txt'"
check "ch02: tool schema carries the WSDL's pattern and statuses" \
  bash -c "cd ch02-soap-apis && uv run -q python -c \"
import soap_tool as t
p = t.TOOL['input_schema']['properties']['order_id']['pattern']
assert p == '^ord-[0-9]{4}\$', p
assert 'placed, paid, shipped, delivered, cancelled' in t.TOOL['description']
assert 'WS-Security' not in t.TOOL['description']
for bad in ('5002', 'ord-5002\\n', '../ord-5002'):
    r, err = t.run_tool({'order_id': bad})
    assert err and r['error'] == 'invalid_argument', (bad, r)
\""
check "ch02: tool turns the SOAP fault into JSON the model can act on" \
  bash -c "grep -q '\"error\": \"order_not_found\"' '$CH02/soap-tool.out' &&
           grep -q '\"hint\": \"Order IDs run from ord-5001 to ord-5004.\"' '$CH02/soap-tool.out'"

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
