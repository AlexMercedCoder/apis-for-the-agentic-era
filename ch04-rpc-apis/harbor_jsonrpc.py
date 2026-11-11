"""Harbor's order service over JSON-RPC 2.0, on two transports.

    uv run python harbor_jsonrpc.py [--port 8042]   # HTTP: POST /rpc
    uv run python harbor_jsonrpc.py --stdio         # one JSON per line

Both transports call the same handle() function, which takes the text of
one JSON-RPC message (a request, a notification, or a batch) and returns
the text of the reply, or None when the spec says to send nothing. The
transport only moves those strings. Standard library only; the order
rules live in harbor_orders.py, shared with harbor_grpc.py.

Methods: placeOrder, getOrderStatus, and, on stdio only, watchOrder,
which answers at once and then sends orderStatusChanged notifications.
"""

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import harbor_orders as h

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND = -32700, -32600, -32601
INVALID_PARAMS, INTERNAL_ERROR = -32602, -32603

# Application errors use codes outside the range the spec reserves
# (-32768 to -32000).
APP_CODES = {"not_found": 1001, "out_of_stock": 1002,
             "request_id_reused": 1003}

store = h.Store()


def log(text):
    print(text, file=sys.stderr, flush=True)


class RpcError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def from_order_error(err):
    data = {k: v for k, v in (("field", err.field), ("hint", err.hint))
            if v is not None}
    code = APP_CODES.get(err.kind, INVALID_PARAMS)
    message = "Invalid params" if code == INVALID_PARAMS else err.message
    if code == INVALID_PARAMS:
        data = {"detail": err.message, **data}
    return RpcError(code, message, data or None)


def place_order(params, transport):
    if not isinstance(params, dict):
        raise RpcError(INVALID_PARAMS, "Invalid params",
                       {"detail": "params must be an object."})
    fingerprint, lines = store.check_order(
        params.get("customer_id"), params.get("lines"),
        params.get("request_id"))
    earlier = store.replay(params["request_id"], fingerprint)
    if earlier is not None:
        return h.order_view(earlier), "replay"
    time.sleep(h.PAYMENT_SECONDS)  # the payment stand-in
    order, new = store.commit(params["customer_id"], lines,
                              params["request_id"], fingerprint)
    return h.order_view(order), "new" if new else "replay"


def get_order_status(params, transport):
    if not isinstance(params, dict):
        raise RpcError(INVALID_PARAMS, "Invalid params",
                       {"detail": "params must be an object."})
    return h.status_view(store.get_order(params.get("order_id"))), None


def watch_order(params, transport):
    if transport.notify is None:
        raise RpcError(METHOD_NOT_FOUND, "Method not found",
                       {"detail": "watchOrder needs a transport where the "
                                  "server can send first, such as stdio."})
    order_id = (params or {}).get("order_id")
    steps = store.watch(order_id)
    first = next(steps)  # raises for an unknown order before we answer

    def push():
        for order in steps:
            transport.notify("orderStatusChanged",
                             {"order_id": order["id"],
                              "status": order["status"]})
    threading.Thread(target=push, daemon=True).start()
    return {"order_id": first["id"], "status": first["status"]}, None


METHODS = {"placeOrder": place_order, "getOrderStatus": get_order_status,
           "watchOrder": watch_order}


def error(id_, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "error": err, "id": id_}


def handle_one(msg, transport):
    """Process one request object. Returns a response dict, or None for a
    notification."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or \
            not isinstance(msg.get("method"), str) or \
            ("id" in msg and not isinstance(msg["id"], (str, int,
                                                         type(None)))):
        log(f"{transport.name} - invalid request")
        return error(msg.get("id") if isinstance(msg, dict) else None,
                     INVALID_REQUEST, "Invalid Request")
    is_notification = "id" not in msg
    method, id_ = msg["method"], msg.get("id")
    kind = "notification" if is_notification else f"id={id_}"
    try:
        if method not in METHODS:
            raise RpcError(METHOD_NOT_FOUND, "Method not found")
        result, note = METHODS[method](msg.get("params"), transport)
        log(f"{transport.name} {method} {kind} ok"
            f"{' ' + note + ' ' + result['order_id'] if note else ''}")
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "result": result, "id": id_}
    except h.OrderError as err:
        rpc = from_order_error(err)
    except RpcError as err:
        rpc = err
    except Exception as exc:  # report, never crash the connection
        rpc = RpcError(INTERNAL_ERROR, "Internal error",
                       {"detail": type(exc).__name__})
    log(f"{transport.name} {method} {kind} error {rpc.code}")
    if is_notification:
        return None
    return error(id_, rpc.code, rpc.message, rpc.data)


def handle(text, transport):
    """One JSON-RPC message in, the reply text out (None means send
    nothing, which the spec requires for notifications)."""
    try:
        msg = json.loads(text)
    except ValueError:
        log(f"{transport.name} - parse error")
        return dumps(error(None, PARSE_ERROR, "Parse error"))
    if isinstance(msg, list):
        if not msg:
            return dumps(error(None, INVALID_REQUEST, "Invalid Request"))
        replies = [r for r in (handle_one(m, transport) for m in msg)
                   if r is not None]
        return dumps(replies) if replies else None
    reply = handle_one(msg, transport)
    return dumps(reply) if reply is not None else None


def dumps(obj):
    return json.dumps(obj, separators=(",", ":"))


class HttpTransport:
    """Request and response only: the server can't speak first."""
    name, notify = "http", None


class Handler(BaseHTTPRequestHandler):
    server_version = "harbor-ch04-jsonrpc/1.0"
    sys_version = ""

    def log_message(self, *args):
        pass

    def do_POST(self):
        if self.path != "/rpc":
            self.send_error(404)
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        reply = handle(body.decode("utf-8", "replace"), HttpTransport)
        if reply is None:
            self.send_response(204)
            self.end_headers()
            return
        data = reply.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class StdioTransport:
    """Newline-delimited JSON on stdin and stdout. Either side can send a
    message at any time, so the server can push notifications."""
    name = "stdio"
    lock = threading.Lock()

    @classmethod
    def send(cls, text):
        with cls.lock:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()

    @classmethod
    def notify(cls, method, params):
        cls.send(dumps({"jsonrpc": "2.0", "method": method,
                        "params": params}))


def serve_stdio():
    for line in sys.stdin:
        if line.strip():
            reply = handle(line, StdioTransport)
            if reply is not None:
                StdioTransport.send(reply)
    time.sleep(3 * h.WATCH_SECONDS)  # let pending notifications go out


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8042)
    parser.add_argument("--stdio", action="store_true")
    args = parser.parse_args()
    if args.stdio:
        serve_stdio()
    else:
        log(f"harbor JSON-RPC on http://127.0.0.1:{args.port}/rpc")
        ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
