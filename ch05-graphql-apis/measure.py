"""The order screen for ord-5002 over REST (Chapter 3) and GraphQL.

    uv run python measure.py [--runs 20] [--out captures]

The screen shows an order, its two books, and their reviews. Over REST
that is five GETs to Chapter 3's API: the order, then each book and each
book's reviews. Over GraphQL it is one POST of the OrderScreen operation,
sent once as query text and once as a trusted document ID.

Starts both servers from seed data, puts a byte-counting TCP proxy in
front of each, warms each keep-alive connection with one screen load,
then loads the screen --runs times. Reports per screen load: requests,
response body bytes (as served, and as compact JSON), leaf values
delivered, and wire bytes (both directions, headers included; median and
range). Writes order-screen-sizes.json to the --out folder.
"""

import argparse
import hashlib
import http.client
import json
import socket
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXAMPLES = HERE.parent
SERVERS = {
    "REST": (8003, ["uv", "run", "-q", "python", "harbor_api.py",
                    "--port", "8003"], EXAMPLES / "ch03-rest-apis"),
    "GraphQL": (8005, ["uv", "run", "-q", "python", "harbor_graphql.py",
                       "--port", "8005"], HERE),
}
PROXY_OFFSET = 100
SCREEN = (HERE / "operations" / "OrderScreen.graphql").read_text()
DOC_ID = "sha256:" + hashlib.sha256(SCREEN.encode()).hexdigest()


class CountingProxy:
    """Forwards 127.0.0.1:listen to 127.0.0.1:target, counting bytes."""

    def __init__(self, listen: int, target: int) -> None:
        self.target = target
        self.c2s = 0
        self.s2c = 0
        self.lock = threading.Lock()
        self.sock = socket.create_server(("127.0.0.1", listen))
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self) -> None:
        while True:
            client, _ = self.sock.accept()
            upstream = socket.create_connection(("127.0.0.1", self.target))
            for src, dst, key in ((client, upstream, "c2s"),
                                  (upstream, client, "s2c")):
                threading.Thread(target=self.pump, args=(src, dst, key),
                                 daemon=True).start()

    def pump(self, src, dst, key) -> None:
        try:
            while data := src.recv(65536):
                with self.lock:
                    setattr(self, key, getattr(self, key) + len(data))
                dst.sendall(data)
        except OSError:
            pass

    def total(self) -> int:
        with self.lock:
            return self.c2s + self.s2c


def wait_for_port(port: int, seconds: float = 30) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"nothing listening on {port}")


class Http1:
    """One keep-alive HTTP/1.1 connection with only the headers we set."""

    def __init__(self, port: int) -> None:
        self.conn = http.client.HTTPConnection("127.0.0.1", port)

    def call(self, method: str, path: str, body: bytes = b"") -> bytes:
        self.conn.putrequest(method, path, skip_accept_encoding=True)
        if body:
            self.conn.putheader("Content-Type", "application/json")
            self.conn.putheader("Content-Length", str(len(body)))
        self.conn.endheaders(body or None)
        resp = self.conn.getresponse()
        data = resp.read()
        assert resp.status == 200, (path, resp.status, data)
        return data


def leaves(value) -> int:
    if isinstance(value, dict):
        return sum(leaves(v) for v in value.values())
    if isinstance(value, list):
        return sum(leaves(v) for v in value)
    return 1


def compact(body: bytes) -> int:
    return len(json.dumps(json.loads(body), separators=(",", ":")).encode())


def rest_screen(http: Http1) -> list[bytes]:
    order = http.call("GET", "/orders/ord-5002")
    bodies = [order]
    for line in json.loads(order)["lines"]:
        book_id = line["book_id"]
        bodies.append(http.call("GET", f"/books/{book_id}"))
        bodies.append(http.call("GET", f"/books/{book_id}/reviews"))
    return bodies


def graphql_screen(http: Http1, by_id: bool) -> list[bytes]:
    body = {"operationName": "OrderScreen",
            "variables": {"orderId": "ord-5002"}}
    if by_id:
        body["documentId"] = DOC_ID
    else:
        body["query"] = SCREEN
    reply = http.call("POST", "/graphql", json.dumps(body).encode())
    assert "errors" not in json.loads(reply), reply
    return [reply]


def settle(proxy: CountingProxy) -> int:
    last, still = proxy.total(), 0
    while still < 3:
        time.sleep(0.01)
        now = proxy.total()
        still = still + 1 if now == last else 0
        last = now
    return last


def measure(name: str, proxy: CountingProxy, load, runs: int) -> dict:
    http = Http1(proxy.sock.getsockname()[1])
    load(http)
    wire = []
    for _ in range(runs):
        before = settle(proxy)
        bodies = load(http)
        wire.append(settle(proxy) - before)
    return {
        "name": name,
        "requests": len(bodies),
        "body_bytes": sum(len(b) for b in bodies),
        "compact_bytes": sum(compact(b) for b in bodies),
        "leaf_values": sum(leaves(json.loads(b)) for b in bodies),
        "wire_median": int(statistics.median(wire)),
        "wire_min": min(wire),
        "wire_max": max(wire),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--out", default=str(HERE / "captures"))
    args = parser.parse_args()
    procs = []
    try:
        for port, cmd, cwd in SERVERS.values():
            procs.append(subprocess.Popen(cmd, cwd=cwd,
                                          stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL))
            wait_for_port(port)
        rest = CountingProxy(8003 + PROXY_OFFSET, 8003)
        gql = CountingProxy(8005 + PROXY_OFFSET, 8005)
        rows = [
            measure("REST, five GETs", rest, rest_screen, args.runs),
            measure("GraphQL, query text", gql,
                    lambda h: graphql_screen(h, by_id=False), args.runs),
            measure("GraphQL, document ID", gql,
                    lambda h: graphql_screen(h, by_id=True), args.runs),
        ]
    finally:
        for p in procs:
            p.terminate()
            p.wait()
    out = Path(args.out) / "order-screen-sizes.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"runs": args.runs, "rows": rows},
                              indent=2) + "\n")
    print(f"order screen for ord-5002, {args.runs} runs after one warm-up")
    print(f"{'':22}{'requests':>9}{'body':>7}{'compact':>9}"
          f"{'values':>8}{'wire median':>13}")
    for r in rows:
        print(f"{r['name']:22}{r['requests']:>9}{r['body_bytes']:>7}"
              f"{r['compact_bytes']:>9}{r['leaf_values']:>8}"
              f"{r['wire_median']:>13,}")
    for r in rows:
        print(f"{r['name']}: wire {r['wire_min']:,} to {r['wire_max']:,}")


if __name__ == "__main__":
    sys.exit(main())
