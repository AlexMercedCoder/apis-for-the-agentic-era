# Chapter 4: Remote Procedure Calls with gRPC, JSON-RPC, and tRPC

Harbor Books' `PlaceOrder` and `GetOrderStatus` as remote procedure calls in
three forms: gRPC with a Protocol Buffers contract and a generated stub,
JSON-RPC 2.0 over HTTP and over stdio from one handler, and tRPC with types
shared end to end in TypeScript. `measure.py` then sends the same order
through all five styles of Chapters 2 to 4 and counts the bytes.

## Requirements

- [uv](https://docs.astral.sh/uv/) (0.6.14 used); it installs Python 3.13
  (pinned in `.python-version`) and the packages in `pyproject.toml`:
  `grpcio==1.85.1`, `grpcio-tools==1.85.1`, `grpcio-reflection==1.85.1`,
  `protobuf==7.36.2`, `hyperframe==6.1.0`, `hpack==4.2.0` (locked in `uv.lock`)
- Node 24 (24.7.0 used) with `npm`; `trpc/package.json` pins
  `@trpc/server` and `@trpc/client` 11.19.0, `zod` 4.6.7, `typescript` 7.0.2,
  `@types/node` 24.19.2 (locked in `trpc/package-lock.json`). Node runs the
  `.ts` files directly.
- `curl` (7.81.0 used)
- [grpcurl](https://github.com/fullstorydev/grpcurl) 1.9.4:
  `go install github.com/fullstorydev/grpcurl/cmd/grpcurl@v1.9.4`.
  `run.sh` looks on `PATH`, then in `~/go/bin`, or set `GRPCURL`.

No account and no API key. Nothing leaves `127.0.0.1`. Run it from inside
the repository: the servers read `../harbor-data/`, and the comparison also
starts `../ch02-soap-apis/` and `../ch03-rest-apis/`.

```bash
./run.sh          # every listing, then the comparison; writes captures/
```

## Files

| File | What it is |
|---|---|
| `proto/harbor/orders/v1/orders.proto` | the gRPC contract; `run.sh` generates Python code from it into `gen/` |
| `harbor_orders.py` | order rules shared by the gRPC and JSON-RPC servers (validation, `request_id` replay, the 150 ms payment stand-in) |
| `harbor_grpc.py` | gRPC server on `127.0.0.1:8041`, reflection on |
| `client.py` | the generated stub: a 0.1 s deadline that fails, then a retry with the same `request_id` |
| `harbor_jsonrpc.py` | JSON-RPC 2.0 on `http://127.0.0.1:8042/rpc`, or `--stdio` |
| `requests/` | JSON-RPC requests for `curl`, the stdio session, the tRPC order body |
| `trpc/server.ts`, `trpc/orders.ts` | tRPC server on `127.0.0.1:8043` |
| `trpc/client.ts` | typed client built from `AppRouter` |
| `trpc/bad-client.ts` | a client with two type errors; `npx tsc --noEmit -p tsconfig.bad.json` rejects it |
| `measure.py` | the payload comparison across SOAP, REST, gRPC, JSON-RPC, and tRPC |
| `captures/` | the exact output the chapter prints |

## Expected output

`client.out`:

```text
DEADLINE_EXCEEDED after 0.1s: Deadline Exceeded
ord-5006: ORDER_STATUS_PLACED, 3797 cents
```

`payload-sizes.txt` (bytes; 20 runs per call after one warm-up call):

```text
call           style      req msg resp msg compact   wire       range
GetOrderStatus SOAP           740      466           1520        same
GetOrderStatus REST             0      388     319    577        same
GetOrderStatus gRPC            10       28            121     121-140
GetOrderStatus JSON-RPC        83      147     147    469        same
GetOrderStatus tRPC             0      133     133    432        same
PlaceOrder     REST           134      461     383    919        same
PlaceOrder     gRPC            73       69            225     225-273
PlaceOrder     JSON-RPC       213      298     298    751        same
PlaceOrder     tRPC           157      284     284    747        same
```

Message bytes are the payload each style defines (the SOAP envelope, the JSON
body, the JSON-RPC object, the protobuf message without gRPC's 5-byte prefix).
Wire bytes are everything one call put on a warm TCP connection, both
directions; for gRPC, the call's own HEADERS and DATA frames. Every HTTP/1.1
request comes from one Python client with only the headers its style needs.

## Notes

- The payment stand-in waits 150 ms so a short deadline expires inside it. The
  gRPC handler checks `context.is_active()` before it commits, so a call that
  missed its deadline creates no order.
- `watchOrder` (JSON-RPC, stdio only) and `WatchOrder` (gRPC) move a watched
  order to paid and then shipped on a 100 ms timer. That is a stand-in for
  Harbor's payment and shipping services.
- The tRPC server sets `isDev: false`. Without it, tRPC adds stack traces with
  your file paths to error responses unless `NODE_ENV=production`.
