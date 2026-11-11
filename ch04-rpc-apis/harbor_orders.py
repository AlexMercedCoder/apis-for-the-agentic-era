"""The order logic every Chapter 4 Python server shares.

harbor_grpc.py and harbor_jsonrpc.py are two front doors onto this one
store, so the gRPC and JSON-RPC versions of PlaceOrder behave the same:
same validation, same order IDs, same request_id replay rule. The tRPC
server in trpc/ is TypeScript and repeats these rules in its own code.

State is in memory and reloads from ../harbor-data/ on every start, so
the first new order is always ord-5005.
"""

import copy
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "harbor-data"

CUSTOMER_ID = re.compile(r"cus-[0-9]{3}")
BOOK_ID = re.compile(r"bk-[0-9]{4}")
ORDER_ID = re.compile(r"ord-[0-9]{4}")
REQUEST_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# The payment step is a stand-in: it waits this long, as a call to a
# payment provider would, so a short client deadline can expire inside it.
PAYMENT_SECONDS = 0.15

# A watched order moves on this timer, standing in for Harbor's payment
# and shipping services.
WATCH_STEPS = ("paid", "shipped")
WATCH_SECONDS = 0.1


class OrderError(Exception):
    """A rule the request broke. Each front door maps `kind` to its own
    error format (a gRPC status code, a JSON-RPC error code)."""

    def __init__(self, kind, message, hint=None, field=None):
        super().__init__(message)
        self.kind, self.message, self.hint, self.field = (
            kind, message, hint, field)


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.books = {b["id"]: b for b in
                      json.loads((DATA / "books.json").read_text())}
        self.customers = {c["id"] for c in
                          json.loads((DATA / "customers.json").read_text())}
        self.orders = {o["id"]: o for o in
                       json.loads((DATA / "orders.json").read_text())}
        self.next_id = 1 + max(int(i.split("-")[1]) for i in self.orders)
        self.requests = {}  # request_id -> (fingerprint, order_id)

    def order_id_hint(self):
        ids = sorted(self.orders)
        return f"Order IDs run from {ids[0]} to {ids[-1]}."

    def get_order(self, order_id):
        if not isinstance(order_id, str) or not ORDER_ID.fullmatch(order_id):
            raise OrderError("invalid_argument",
                             f"order_id {order_id!r} does not match "
                             "ord-[0-9]{4}.", field="order_id",
                             hint="Order IDs look like ord-5001.")
        with self.lock:
            order = self.orders.get(order_id)
            if order is None:
                raise OrderError("not_found",
                                 f"No order with id {order_id}.",
                                 hint=self.order_id_hint())
            return copy.deepcopy(order)

    def check_order(self, customer_id, lines, request_id):
        """Validate a PlaceOrder request. Returns (fingerprint, lines)."""
        if not isinstance(customer_id, str) or \
                not CUSTOMER_ID.fullmatch(customer_id):
            raise OrderError("invalid_argument",
                             f"customer_id {customer_id!r} does not match "
                             "cus-[0-9]{3}.", field="customer_id",
                             hint="Customer IDs look like cus-001.")
        if customer_id not in self.customers:
            raise OrderError("not_found", f"No customer {customer_id}.",
                             field="customer_id")
        if not isinstance(request_id, str) or \
                not REQUEST_ID.fullmatch(request_id):
            raise OrderError("invalid_argument",
                             "request_id must be a lowercase UUID.",
                             field="request_id",
                             hint="Send a new UUID per order and the same "
                                  "one on a retry.")
        if not isinstance(lines, list) or not 1 <= len(lines) <= 20:
            raise OrderError("invalid_argument",
                             "lines must hold 1 to 20 items.",
                             field="lines")
        clean = []
        for i, line in enumerate(lines):
            book_id = line.get("book_id") if isinstance(line, dict) else None
            qty = line.get("quantity") if isinstance(line, dict) else None
            if not isinstance(book_id, str) or not BOOK_ID.fullmatch(book_id):
                raise OrderError("invalid_argument",
                                 f"lines[{i}].book_id {book_id!r} does not "
                                 "match bk-[0-9]{4}.",
                                 field=f"lines[{i}].book_id",
                                 hint="Book IDs look like bk-1001.")
            if isinstance(qty, bool) or not isinstance(qty, int) or \
                    not 1 <= qty <= 10:
                raise OrderError("invalid_argument",
                                 f"lines[{i}].quantity must be an integer "
                                 "from 1 to 10.",
                                 field=f"lines[{i}].quantity")
            clean.append({"book_id": book_id, "quantity": qty})
        fingerprint = json.dumps([customer_id, clean], sort_keys=True)
        return fingerprint, clean

    def replay(self, request_id, fingerprint):
        """The order an earlier request with this request_id placed, or
        None. A request_id reused for a different order is an error."""
        with self.lock:
            seen = self.requests.get(request_id)
        if seen is None:
            return None
        if seen[0] != fingerprint:
            raise OrderError("request_id_reused",
                             "This request_id was already used for a "
                             "different order.", field="request_id",
                             hint="Use a new request_id for a new order.")
        return self.get_order(seen[1])

    def commit(self, customer_id, lines, request_id, fingerprint):
        """Create the order. Call only after payment, and only if the
        caller is still waiting for the answer."""
        with self.lock:
            seen = self.requests.get(request_id)
            if seen is not None:
                return copy.deepcopy(self.orders[seen[1]]), False
            priced = []
            for line in lines:
                book = self.books.get(line["book_id"])
                if book is None:
                    raise OrderError("not_found",
                                     f"No book {line['book_id']}.",
                                     hint="This catalog has IDs bk-1001 "
                                          "to bk-1008.")
                if book["in_stock"] < line["quantity"]:
                    raise OrderError("out_of_stock",
                                     f"{book['title']} has "
                                     f"{book['in_stock']} in stock.",
                                     hint="Order fewer copies or another "
                                          "book.")
                priced.append({**line,
                               "unit_price_cents": book["price_cents"]})
            for line in priced:
                self.books[line["book_id"]]["in_stock"] -= line["quantity"]
            order_id = f"ord-{self.next_id}"
            self.next_id += 1
            order = {
                "id": order_id, "customer_id": customer_id,
                "placed_at": now_utc(), "status": "placed",
                "lines": priced,
                "total_cents": sum(l["quantity"] * l["unit_price_cents"]
                                   for l in priced),
                "currency": "USD",
            }
            self.orders[order_id] = order
            self.requests[request_id] = (fingerprint, order_id)
            return copy.deepcopy(order), True

    def watch(self, order_id):
        """Yield the order's status now, then each step of the stand-in
        fulfillment timer."""
        order = self.get_order(order_id)
        yield order
        if order["status"] == "placed":
            steps = WATCH_STEPS
        elif order["status"] == "paid":
            steps = WATCH_STEPS[1:]
        else:
            return
        for step in steps:
            time.sleep(WATCH_SECONDS)
            with self.lock:
                self.orders[order_id]["status"] = step
                order = copy.deepcopy(self.orders[order_id])
            yield order


def status_view(order):
    """The five fields GetOrderStatus returns, as Chapter 2's SOAP
    service does. The ID is order_id, as in the .proto, so it can't be
    mistaken for a JSON-RPC message id."""
    return {"order_id": order["id"],
            **{k: order[k] for k in
               ("status", "placed_at", "total_cents", "currency")}}


def order_view(order):
    """A whole order, keyed the way the .proto names its fields."""
    return {"order_id": order["id"],
            **{k: order[k] for k in
               ("customer_id", "status", "placed_at", "lines",
                "total_cents", "currency")}}
