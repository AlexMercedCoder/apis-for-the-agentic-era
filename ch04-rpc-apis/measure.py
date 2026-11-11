"""Measure one Harbor order in five styles: SOAP (Chapter 2), REST
(Chapter 3), gRPC, JSON-RPC 2.0, and tRPC (this chapter).

    uv run python measure.py [--runs 20] [--out captures]

Starts all five servers from their seed data, puts a byte-counting TCP
proxy in front of each, and makes the same two calls in each style:
GetOrderStatus for ord-5002, and PlaceOrder for one copy of bk-1002 and
two of bk-1008 for cus-001. PlaceOrder runs once and is then retried
with the same request ID (or Idempotency-Key) on every later run, so
every run carries the same order. Chapter 2's WSDL has no PlaceOrder,
so SOAP has only the status call.

Two numbers per call:

- message bytes: the encoded payload each style defines (the SOAP
  envelope, the JSON body, the JSON-RPC object, the protobuf message
  without gRPC's 5-byte prefix). A GET carries its arguments in the URL,
  so its request message is 0 bytes.
- wire bytes: everything the call put on the TCP connection, both
  directions, after the connection was open and warmed up by one call.
  For HTTP/1.1 that is the request and response, headers included. For
  gRPC over HTTP/2 it is the call's own HEADERS and DATA frames (with
  their 9-byte frame headers), not connection-level frames such as
  SETTINGS, PING, or WINDOW_UPDATE.

Every HTTP/1.1 request goes out from one Python client (http.client)
with only the headers its style requires. Response headers are each
server's own. Writes payload-sizes.json and payload-sizes.txt, plus
grpc-frames.txt, the decoded HTTP/2 frames of one PlaceOrder call.
"""

import argparse
import http.client
import json
import os
import socket
import statistics
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "gen"))

import grpc  # noqa: E402
from hpack import Decoder  # noqa: E402
from hyperframe.frame import Frame  # noqa: E402

from harbor.orders.v1 import orders_pb2 as pb  # noqa: E402
from harbor.orders.v1 import orders_pb2_grpc as pb_grpc  # noqa: E402

EXAMPLES = HERE.parent
ORDER = {"customer_id": "cus-001",
         "lines": [{"book_id": "bk-1002", "quantity": 1},
                   {"book_id": "bk-1008", "quantity": 2}]}
SOAP_ENVELOPE = (EXAMPLES / "ch02-soap-apis" / "requests" /
                 "get-order-status.xml").read_bytes()
SOAP_ACTION = '"http://harbor.example/orders/GetOrderStatus"'
PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"

SERVERS = {  # style: (port, command, working directory)
    "SOAP": (8002, ["uv", "run", "-q", "python", "harbor_soap.py",
                    "--port", "8002"], EXAMPLES / "ch02-soap-apis"),
    "REST": (8003, ["uv", "run", "-q", "python", "harbor_api.py",
                    "--port", "8003"], EXAMPLES / "ch03-rest-apis"),
    "gRPC": (8041, ["uv", "run", "-q", "python", "harbor_grpc.py"], HERE),
    "JSON-RPC": (8042, ["uv", "run", "-q", "python", "harbor_jsonrpc.py"],
                 HERE),
    "tRPC": (8043, ["node", "server.ts"], HERE / "trpc"),
}
PROXY_OFFSET = 100


class CountingProxy:
    """Forwards 127.0.0.1:listen to 127.0.0.1:target and keeps every
    byte, per connection and direction."""

    def __init__(self, listen, target):
        self.target = target
        self.conns = []  # [{"c2s": bytearray, "s2c": bytearray}]
        self.lock = threading.Lock()
        self.sock = socket.create_server(("127.0.0.1", listen))
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self):
        while True:
            client, _ = self.sock.accept()
            upstream = socket.create_connection(("127.0.0.1", self.target))
            conn = {"c2s": bytearray(), "s2c": bytearray()}
            with self.lock:
                self.conns.append(conn)
            for src, dst, key in ((client, upstream, "c2s"),
                                  (upstream, client, "s2c")):
                threading.Thread(target=self.pump,
                                 args=(src, dst, conn[key]),
                                 daemon=True).start()

    def pump(self, src, dst, buf):
        try:
            while data := src.recv(65536):
                with self.lock:
                    buf.extend(data)
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def totals(self):
        with self.lock:
            return (sum(len(c["c2s"]) for c in self.conns),
                    sum(len(c["s2c"]) for c in self.conns))


def wait_for_port(port, seconds=30):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"nothing listening on {port}")


def settle(proxy, before, timeout=2.0):
    """Wait until the proxy has stopped receiving bytes."""
    last, still = proxy.totals(), 0
    end = time.time() + timeout
    while time.time() < end and still < 3:
        time.sleep(0.01)
        now = proxy.totals()
        still = still + 1 if now == last else 0
        last = now
    return (last[0] - before[0]) + (last[1] - before[1])


class Http1:
    """One keep-alive HTTP/1.1 connection with exactly the headers we set."""

    def __init__(self, port):
        self.port = port
        self.conn = http.client.HTTPConnection("127.0.0.1", port)

    def call(self, method, path, body=b"", headers=()):
        self.conn.putrequest(method, path, skip_accept_encoding=True)
        for k, v in headers:
            self.conn.putheader(k, v)
        if method == "POST" or body:
            self.conn.putheader("Content-Length", str(len(body)))
        self.conn.endheaders(body or None)
        resp = self.conn.getresponse()
        return resp.status, resp.read()


def compact(body):
    return len(json.dumps(json.loads(body), separators=(",", ":")).encode())


def http1_calls(style):
    """The two calls in one style: (name, method, path, body, headers)."""
    rid = str(uuid.UUID("6f1c2a9e-3b4d-4e5f-8a7b-9c0d1e2f3a4b"))
    j = [("Content-Type", "application/json")]
    if style == "SOAP":
        return [("GetOrderStatus", "POST", "/harbor/soap", SOAP_ENVELOPE,
                 [("Content-Type", "text/xml; charset=utf-8"),
                  ("SOAPAction", SOAP_ACTION)])]
    if style == "REST":
        return [("GetOrderStatus", "GET", "/orders/ord-5002", b"", []),
                ("PlaceOrder", "POST", "/orders",
                 (EXAMPLES / "ch03-rest-apis" / "requests" /
                  "place-order.json").read_bytes(),
                 j + [("Idempotency-Key", f'"{rid}"')])]
    if style == "JSON-RPC":
        def rpc(method, params, id_):
            return json.dumps({"jsonrpc": "2.0", "method": method,
                               "params": params, "id": id_},
                              separators=(",", ":")).encode()
        return [("GetOrderStatus", "POST", "/rpc",
                 rpc("getOrderStatus", {"order_id": "ord-5002"}, 1), j),
                ("PlaceOrder", "POST", "/rpc",
                 rpc("placeOrder", {**ORDER, "request_id": rid}, 2), j)]
    if style == "tRPC":
        return [("GetOrderStatus", "GET",
                 "/getOrderStatus?input=%7B%22order_id%22%3A%22ord-5002"
                 "%22%7D", b"", []),
                ("PlaceOrder", "POST", "/placeOrder",
                 json.dumps({**ORDER, "request_id": rid},
                            separators=(",", ":")).encode(), j)]
    raise ValueError(style)


def measure_http1(style, proxy, runs):
    client = Http1(SERVERS[style][0] + PROXY_OFFSET)
    calls = http1_calls(style)
    client.call(*calls[0][1:])  # warm-up: opens the connection
    settle(proxy, proxy.totals())
    results = {}
    for name, method, path, body, headers in calls:
        wire, sizes = [], set()
        for _ in range(runs):
            before = proxy.totals()
            status, resp = client.call(method, path, body, headers)
            assert status in (200, 201), (style, name, status, resp[:200])
            wire.append(settle(proxy, before))
            sizes.add((len(body), len(resp)))
        (req, resp_len), = sizes  # the same bytes on every run
        row = {"request_message": req, "response_message": resp_len,
               "wire": wire}
        if "json" in dict(headers).get("Content-Type", "json") and \
                resp.startswith(b"{"):
            row["response_compact"] = compact(resp)
        results[name] = row
    return results


def h2_frames(data, decoder, client_side):
    """Parse a run of HTTP/2 frames. Yields (frame, headers, size)."""
    pos = len(PREFACE) if client_side and data.startswith(PREFACE) else 0
    while pos + 9 <= len(data):
        frame, length = Frame.parse_frame_header(memoryview(data[pos:pos + 9]))
        if pos + 9 + length > len(data):
            break
        frame.parse_body(memoryview(data[pos + 9:pos + 9 + length]))
        headers = None
        if frame.type in (0x01, 0x09):  # HEADERS, CONTINUATION
            headers = [(k.decode() if isinstance(k, bytes) else k,
                        v.decode() if isinstance(v, bytes) else v)
                       for k, v in decoder.decode(bytes(frame.data))]
        yield frame, headers, 9 + length
        pos += 9 + length


def stream_bytes(conn):
    """Bytes per HTTP/2 stream, both directions, for HEADERS and DATA
    frames only, plus a decoded log of every frame."""
    per_stream, log = {}, []
    for key, client_side in (("c2s", True), ("s2c", False)):
        decoder = Decoder()
        for frame, headers, size in h2_frames(bytes(conn[key]), decoder,
                                              client_side):
            log.append((key, frame, headers, size))
            if frame.type in (0x00, 0x01, 0x09) and frame.stream_id:
                per_stream[frame.stream_id] = \
                    per_stream.get(frame.stream_id, 0) + size
    return per_stream, log


def measure_grpc(proxy, runs):
    channel = grpc.insecure_channel(f"127.0.0.1:{8041 + PROXY_OFFSET}")
    stub = pb_grpc.OrderServiceStub(channel)
    status_req = pb.GetOrderStatusRequest(order_id="ord-5002")
    place_req = pb.PlaceOrderRequest(
        customer_id=ORDER["customer_id"],
        lines=[pb.LineItem(**l) for l in ORDER["lines"]],
        request_id="6f1c2a9e-3b4d-4e5f-8a7b-9c0d1e2f3a4b")
    stub.GetOrderStatus(status_req, timeout=5)  # warm-up
    calls = [("GetOrderStatus", stub.GetOrderStatus, status_req),
             ("PlaceOrder", stub.PlaceOrder, place_req)]
    order, sizes = [], {}
    for name, method, req in calls:
        for _ in range(runs):
            reply = method(req, timeout=5)
            order.append(name)
            sizes.setdefault(name, set()).add((req.ByteSize(),
                                               reply.ByteSize()))
    time.sleep(0.2)
    channel.close()
    time.sleep(0.2)
    conn, = proxy.conns
    per_stream, log = stream_bytes(conn)
    ids = sorted(per_stream)[1:]  # stream 1 was the warm-up
    assert len(ids) == len(order), (len(ids), len(order))
    results = {}
    for name, _, _ in calls:
        (req, resp), = sizes[name]
        results[name] = {"request_message": req, "response_message": resp,
                         "wire": [per_stream[i] for i, n in zip(ids, order)
                                  if n == name]}
    return results, log, ids[order.index("PlaceOrder")]


def frame_log(log, stream_id):
    """The decoded frames of one gRPC call, as printable lines."""
    names = {0x00: "DATA", 0x01: "HEADERS", 0x08: "WINDOW_UPDATE",
             0x09: "CONTINUATION"}
    lines = []
    for key, frame, headers, size in log:
        if frame.stream_id != stream_id:
            continue
        arrow = "client -> server" if key == "c2s" else "server -> client"
        flags = ",".join(sorted(frame.flags)) or "-"
        lines.append(f"{arrow}  {names.get(frame.type, frame.type)}  "
                     f"stream={frame.stream_id}  length={size - 9}  "
                     f"flags={flags}")
        for k, v in headers or []:
            lines.append(f"    {k}: {v}")
        if frame.type == 0x00 and frame.data:
            data = bytes(frame.data)
            lines.append(f"    prefix: compressed={data[0]} "
                         f"length={int.from_bytes(data[1:5], 'big')}")
            lines.append(f"    message: {data[5:].hex(' ')}")
    return lines


def summarize(results, runs):
    rows = []
    for name in ("GetOrderStatus", "PlaceOrder"):
        for style in SERVERS:
            r = results[style].get(name)
            if r is None:
                continue
            w = r["wire"]
            rows.append({
                "call": name, "style": style,
                "request_message": r["request_message"],
                "response_message": r["response_message"],
                "response_compact": r.get("response_compact"),
                "wire_median": statistics.median(w),
                "wire_min": min(w), "wire_max": max(w)})
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--out", default="captures")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    procs = []
    try:
        for style, (port, cmd, cwd) in SERVERS.items():
            procs.append(subprocess.Popen(
                cmd, cwd=cwd, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env={**os.environ,
                                                "PORT": str(port)}))
        proxies = {}
        for style, (port, _, _) in SERVERS.items():
            wait_for_port(port)
            proxies[style] = CountingProxy(port + PROXY_OFFSET, port)
        results = {}
        for style in SERVERS:
            if style == "gRPC":
                results[style], log, sid = measure_grpc(proxies[style],
                                                        args.runs)
            else:
                results[style] = measure_http1(style, proxies[style],
                                               args.runs)
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait(timeout=10)
    rows = summarize(results, args.runs)
    (out / "payload-sizes.json").write_text(json.dumps(
        {"runs": args.runs, "rows": rows}, indent=2) + "\n")
    lines = [f"{'call':<15}{'style':<10}{'req msg':>8}{'resp msg':>9}"
             f"{'compact':>8}{'wire':>7}{'range':>12}"]
    for r in rows:
        rng = (f"{r['wire_min']}-{r['wire_max']}"
               if r["wire_min"] != r["wire_max"] else "same")
        lines.append(f"{r['call']:<15}{r['style']:<10}"
                     f"{r['request_message']:>8}{r['response_message']:>9}"
                     f"{r['response_compact'] or '':>8}"
                     f"{r['wire_median']:>7.0f}{rng:>12}")
    lines.append(f"runs per call: {args.runs}, after one warm-up call")
    (out / "payload-sizes.txt").write_text("\n".join(lines) + "\n")
    (out / "grpc-frames.txt").write_text("\n".join(frame_log(log, sid))
                                         + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
