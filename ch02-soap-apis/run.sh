#!/usr/bin/env bash
# Runs every Chapter 2 listing against a local Harbor SOAP service and
# writes the output to captures/. Needs curl and uv. No network access
# beyond 127.0.0.1, no API keys.
set -euo pipefail
cd "$(dirname "$0")"
PORT=8002
URL="http://127.0.0.1:$PORT/harbor/soap"
ACTION="http://harbor.example/orders"
OUT="${OUT:-captures}"
mkdir -p "$OUT"

uv sync -q
uv run python harbor_soap.py --port "$PORT" 2> "$OUT/server-log.txt" &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT

for _ in $(seq 1 50); do
  (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null && break
  sleep 0.1
done

post() {  # post <operation> <envelope file>
  curl -s -w '%{http_code}\n' "$URL" \
    -H 'Content-Type: text/xml; charset=utf-8' \
    -H "SOAPAction: \"$ACTION/$1\"" --data-binary "@$2"
}

curl -sv "$URL" -H 'Content-Type: text/xml; charset=utf-8' \
  -H "SOAPAction: \"$ACTION/GetOrderStatus\"" \
  --data-binary @requests/get-order-status.xml \
  > "$OUT/curl-get-order-status.out" 2> "$OUT/curl-get-order-status.err"
curl -sv "$URL" -H 'Content-Type: text/xml; charset=utf-8' \
  -H "SOAPAction: \"$ACTION/GetOrderStatus\"" \
  --data-binary @requests/get-order-status-missing.xml \
  > "$OUT/curl-order-not-found.out" 2> "$OUT/curl-order-not-found.err"
post GetBook requests/get-book.xml > "$OUT/curl-get-book.out"
post GetBook requests/must-understand.xml \
  > "$OUT/curl-must-understand.out"
grep -v -e '<wsse:Username>' -e '<wsse:Password' \
  -e 'Type=' requests/get-order-status.xml > "$OUT/no-credentials.xml"
post GetOrderStatus "$OUT/no-credentials.xml" > "$OUT/curl-no-token.out"
rm "$OUT/no-credentials.xml"

uv run python -m zeep harbor.wsdl > "$OUT/zeep-inspect.out"
uv run python client.py ord-5002 --history > "$OUT/client.out"
uv run python client.py ord-9999 > "$OUT/client-not-found.out"
uv run python client.py 5002 > "$OUT/client-bad-id.out"
uv run python soap_tool.py > "$OUT/soap-tool.out"

kill "$SERVER"; wait "$SERVER" 2>/dev/null || true
trap - EXIT
echo "captures written to $OUT/"
