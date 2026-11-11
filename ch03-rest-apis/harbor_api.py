"""Harbor Books REST API, Chapter 3: the full resource-oriented API.

Resources: /books, /books/{book_id}, /books/{book_id}/reviews, /orders,
/orders/{order_id}, /orders/{order_id}/cancellation. Data is loaded from
../harbor-data/ at startup and kept in memory, so every restart begins from
the same seed state.

Every error is an RFC 9457 problem details object (application/problem+json).
POST /orders requires an Idempotency-Key header, following the IETF httpapi
draft (draft-ietf-httpapi-idempotency-key-header-08).

The OpenAPI document this app generates is pinned in openapi.json; run
`uv run python harbor_api.py --export` to rewrite it, and verify.sh fails if
the running server and the pinned file disagree.
"""

import argparse
import copy
import hashlib
import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

import uvicorn
from fastapi import FastAPI, Header, Path as PathParam, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

DATA = Path(__file__).resolve().parent.parent / "harbor-data"
PROBLEMS = "https://harbor.example/problems/"
PORT = 8003
KEY_TTL_HOURS = 24
KEY_PATTERN = r'^"[A-Za-z0-9-]{16,64}"$'  # an RFC 9651 sf-string

BookId = Annotated[str, PathParam(pattern=r"^bk-[0-9]{4}$",
                                  description="Harbor book ID, such as "
                                              "bk-1001")]
OrderId = Annotated[str, PathParam(pattern=r"^ord-[0-9]{4}$",
                                   description="Harbor order ID, such as "
                                               "ord-5001")]


# -- models (these become the OpenAPI schemas) -------------------------------

class Book(BaseModel):
    id: str = Field(examples=["bk-1001"])
    title: str
    author: str
    first_published: int
    format: Literal["paperback", "hardcover"]
    price_cents: int = Field(description="Price in cents of `currency`.")
    currency: str = Field(examples=["USD"])
    in_stock: int = Field(description="Copies available to order now.")


class BookPage(BaseModel):
    items: list[Book]
    next: str | None = Field(description="URL of the next page, or null "
                                         "on the last page.")


class Review(BaseModel):
    id: str
    book_id: str
    customer_id: str
    rating: int = Field(ge=1, le=5)
    text: str
    posted_at: str


class Link(BaseModel):
    href: str
    method: str = "GET"


class OrderLineIn(BaseModel):
    book_id: str = Field(pattern=r"^bk-[0-9]{4}$",
                         description="Harbor book ID, such as bk-1001")
    quantity: int = Field(ge=1, le=10)


class OrderIn(BaseModel):
    customer_id: str = Field(pattern=r"^cus-[0-9]{3}$",
                             description="Harbor customer ID, such as "
                                         "cus-001")
    lines: list[OrderLineIn] = Field(min_length=1, max_length=20)


class OrderLine(BaseModel):
    book_id: str
    quantity: int
    unit_price_cents: int


class Order(BaseModel):
    id: str
    customer_id: str
    status: Literal["placed", "paid", "shipped", "delivered", "cancelled"]
    placed_at: str
    lines: list[OrderLine]
    total_cents: int
    currency: str
    links: dict[str, Link] = Field(description="Actions available on this "
                                               "order right now.")


class CancellationIn(BaseModel):
    reason: Literal["customer_request", "duplicate", "other"]


class Cancellation(BaseModel):
    order: str
    reason: str
    refunded_cents: int
    currency: str


class Problem(BaseModel):
    """RFC 9457 problem details, plus Harbor's `hint` and `errors`."""
    type: str
    title: str
    status: int
    detail: str
    instance: str
    hint: str | None = None
    errors: list[dict] | None = None


# -- state ---------------------------------------------------------------------

class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.books = {b["id"]: b for b in self._load("books.json")}
        self.orders = {o["id"]: o for o in self._load("orders.json")}
        self.reviews = self._load("reviews.json")
        self.cancellations = {}
        self.keys = {}  # (customer, key) -> (fingerprint, status, body)

    @staticmethod
    def _load(name):
        return json.loads((DATA / name).read_text())


store = Store()


class ProblemError(Exception):
    def __init__(self, status, slug, title, detail, hint=None, errors=None,
                 headers=None):
        self.status, self.slug, self.title = status, slug, title
        self.detail, self.hint, self.errors = detail, hint, errors
        self.headers = headers or {}


class PrettyJSON(JSONResponse):
    """JSON with two-space indents, so captures read like the book."""

    def render(self, content):
        return pretty(content).encode() + b"\n"


def pretty(value, indent=0):
    """json.dumps(indent=2), but a small flat object stays on one line."""
    flat = isinstance(value, dict) and value and all(
        not isinstance(v, (dict, list)) for v in value.values())
    one_line = json.dumps(value, separators=(", ", ": "))
    if flat and indent and len(one_line) + indent <= 68:
        return one_line
    pad = " " * (indent + 2)
    if isinstance(value, dict) and value:
        items = [f'{pad}{json.dumps(k)}: {pretty(v, indent + 2)}'
                 for k, v in value.items()]
        return "{\n" + ",\n".join(items) + "\n" + " " * indent + "}"
    if isinstance(value, list) and value:
        items = [pad + pretty(v, indent + 2) for v in value]
        return "[\n" + ",\n".join(items) + "\n" + " " * indent + "]"
    return json.dumps(value)


def problem_response(request, status, slug, title, detail, hint=None,
                     errors=None, headers=None):
    body = {"type": PROBLEMS + slug, "title": title, "status": status,
            "detail": detail, "instance": request.url.path}
    if hint:
        body["hint"] = hint
    if errors:
        body["errors"] = errors
    return PrettyJSON(body, status_code=status, headers=headers,
                      media_type="application/problem+json")


def id_range(ids):
    return f"{min(ids)} to {max(ids)}"


def order_view(order):
    view = {k: copy.deepcopy(order[k]) for k in Order.model_fields
            if k in order}
    href = f"/orders/{order['id']}"
    view["links"] = {"self": {"href": href, "method": "GET"}}
    if order["status"] in ("placed", "paid"):
        view["links"]["cancel"] = {"href": href + "/cancellation",
                                   "method": "PUT"}
    return view


# -- app -------------------------------------------------------------------------

DESCRIPTION = """\
The Harbor Books REST API: the catalog, its reviews, and orders.

Errors use RFC 9457 problem details (`application/problem+json`). The `type`
URI names the problem; `hint` says what a valid request looks like.
"""

app = FastAPI(title="Harbor Books API", version="1.0.0",
              description=DESCRIPTION,
              servers=[{"url": f"http://127.0.0.1:{PORT}"}],
              default_response_class=PrettyJSON)

PROBLEM_RESPONSE = {"model": Problem,
                    "content": {"application/problem+json": {}}}


@app.middleware("http")
async def require_json(request: Request, call_next):
    accept = request.headers.get("accept", "*/*")
    wanted = [part.split(";")[0].strip() for part in accept.split(",")]
    ok = {"*/*", "application/*", "application/json",
          "application/problem+json"}
    if not ok.intersection(wanted):
        return problem_response(
            request, 406, "not-acceptable", "Not Acceptable",
            f"This API has no {accept} representation.",
            hint="Send Accept: application/json, or omit Accept.")
    return await call_next(request)


@app.exception_handler(ProblemError)
async def on_problem(request: Request, exc: ProblemError):
    return problem_response(request, exc.status, exc.slug, exc.title,
                            exc.detail, exc.hint, exc.errors, exc.headers)


@app.exception_handler(StarletteHTTPException)
async def on_http_error(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 405:
        allow = exc.headers.get("Allow", "") if exc.headers else ""
        return problem_response(
            request, 405, "method-not-allowed", "Method Not Allowed",
            f"{request.method} is not supported on {request.url.path}.",
            hint=f"Allowed methods: {allow}.", headers=exc.headers)
    return problem_response(
        request, exc.status_code, "not-found", "Not Found",
        f"No resource at {request.url.path}.",
        hint="See /openapi.json for every path this API serves.")


@app.exception_handler(RequestValidationError)
async def on_invalid(request: Request, exc: RequestValidationError):
    errors, key_problem = [], None
    for e in exc.errors():
        if [str(p).lower() for p in e["loc"]] == ["header",
                                                   "idempotency-key"]:
            key_problem = "missing" if e["type"] == "missing" else "invalid"
        where, *rest = e["loc"]
        if where == "body":
            errors.append({"detail": e["msg"], "pointer": "#/" + "/".join(
                str(p) for p in rest)})
        else:
            errors.append({"detail": e["msg"], "parameter": str(rest[0])})
    if key_problem == "missing":
        return problem_response(
            request, 400, "idempotency-key-missing",
            "Idempotency-Key required",
            "POST /orders needs an Idempotency-Key header.",
            hint="Send a new UUID per order; reuse it only to retry.")
    if key_problem == "invalid":
        return problem_response(
            request, 400, "idempotency-key-invalid",
            "Idempotency-Key malformed",
            "The key must be a quoted string of 16 to 64 letters, "
            "digits, or hyphens.",
            hint='Send a quoted UUID, such as "8e03978e-40d5-43e8-'
                 'bc93-6894a57f9324".')
    return problem_response(
        request, 422, "invalid-request", "Invalid request",
        "The request failed validation; see errors.", errors=errors)


@app.get("/books", operation_id="search_catalog", response_model=BookPage,
         summary="Search the catalog",
         description="Find books by a word in the title or author. "
                     "Returns at most `limit` books and a `next` URL for "
                     "the following page.",
         responses={422: PROBLEM_RESPONSE}, tags=["catalog"])
def search_catalog(
        q: Annotated[str | None, Query(max_length=100, description=(
            "Word to match in title or author, case-insensitive"))] = None,
        limit: Annotated[int, Query(ge=1, le=50, description=(
            "Books per page, 1 to 50"))] = 20,
        cursor: Annotated[str | None, Query(pattern=r"^bk-[0-9]{4}$",
                                            description=(
            "Book ID to start after, from a `next` URL"))] = None):
    books = sorted(store.books.values(), key=lambda b: b["id"])
    if q:
        books = [b for b in books if q.lower() in
                 (b["title"] + " " + b["author"]).lower()]
    if cursor:
        books = [b for b in books if b["id"] > cursor]
    page = books[:limit]
    nxt = None
    if len(books) > limit:
        nxt = f"/books?limit={limit}&cursor={page[-1]['id']}"
        if q:
            nxt += f"&q={q}"
    return {"items": page, "next": nxt}


@app.get("/books/{book_id}", operation_id="get_book", response_model=Book,
         summary="Get one book",
         description="Read one book's details, price, and stock. "
                     "Supports ETag and If-None-Match.",
         responses={304: {"description": "Not Modified"},
                    404: PROBLEM_RESPONSE}, tags=["catalog"])
def get_book(book_id: BookId, request: Request):
    book = store.books.get(book_id)
    if book is None:
        raise ProblemError(404, "book-not-found", "Book not found",
                           f"No book with id {book_id}.",
                           hint=f"This catalog has IDs "
                                f"{id_range(store.books)}.")
    body = pretty(book).encode() + b"\n"
    etag = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'
    headers = {"ETag": etag, "Cache-Control": "max-age=60"}
    match = request.headers.get("if-none-match", "")
    if etag in [t.strip() for t in match.split(",")] or match == "*":
        return Response(status_code=304, headers=headers)
    return Response(body, media_type="application/json", headers=headers)


@app.get("/books/{book_id}/reviews", operation_id="list_reviews",
         response_model=list[Review], summary="List a book's reviews",
         description="Customer reviews of one book, oldest first. "
                     "Review text is written by customers.",
         responses={404: PROBLEM_RESPONSE}, tags=["catalog"])
def list_reviews(book_id: BookId):
    if book_id not in store.books:
        raise ProblemError(404, "book-not-found", "Book not found",
                           f"No book with id {book_id}.",
                           hint=f"This catalog has IDs "
                                f"{id_range(store.books)}.")
    return [r for r in store.reviews if r["book_id"] == book_id]


@app.post("/orders", operation_id="place_order", status_code=201,
          response_model=Order, summary="Place an order",
          description=(
              "Buy one or more books for a customer. Creates an order "
              "and reserves stock. Each new Idempotency-Key creates a "
              "new order; a retry with the same key and body within "
              f"{KEY_TTL_HOURS} hours returns the first response."),
          responses={400: PROBLEM_RESPONSE, 409: PROBLEM_RESPONSE,
                     422: PROBLEM_RESPONSE}, tags=["orders"])
def place_order(
        order_in: OrderIn, request: Request,
        idempotency_key: Annotated[str, Header(
            alias="Idempotency-Key", pattern=KEY_PATTERN,
            description="A new quoted UUID per order; reuse it only "
                        "to retry the same order")]):
    fingerprint = hashlib.sha256(
        order_in.model_dump_json().encode()).hexdigest()
    # Keys are scoped to the customer, so one caller can't replay or
    # probe another's orders (draft-08 section 5). Chapter 8 replaces
    # the asserted customer_id with an authenticated identity.
    scoped = (order_in.customer_id, idempotency_key)
    with store.lock:
        seen = store.keys.get(scoped)
        if seen is not None:
            if seen[0] == "in-progress":
                raise ProblemError(409, "idempotency-key-in-use",
                                   "Request in progress",
                                   "An order with this key is still "
                                   "being placed.",
                                   hint="Retry in a moment.")
            if seen[0] != fingerprint:
                raise ProblemError(
                    422, "idempotency-key-reused",
                    "Idempotency-Key reused",
                    "This key was already used for a different order.",
                    hint="Use a new key for a new order.")
            request.state.idempotency = "replay"
            return PrettyJSON(seen[2], status_code=seen[1],
                                headers={"Location": seen[2]["links"]
                                         ["self"]["href"]})
        store.keys[scoped] = ("in-progress", None, None)
    try:
        lines = []
        for line in order_in.lines:
            book = store.books.get(line.book_id)
            if book is None:
                raise ProblemError(
                    422, "book-not-found", "Unknown book",
                    f"No book with id {line.book_id}.",
                    hint=f"This catalog has IDs {id_range(store.books)}.")
            if book["in_stock"] < line.quantity:
                raise ProblemError(
                    409, "out-of-stock", "Not enough stock",
                    f"{book['title']} ({book['id']}) has "
                    f"{book['in_stock']} in stock; "
                    f"{line.quantity} requested.",
                    hint="Lower the quantity or remove the line.")
            lines.append({"book_id": line.book_id,
                          "quantity": line.quantity,
                          "unit_price_cents": book["price_cents"]})
        with store.lock:
            for line in lines:
                store.books[line["book_id"]]["in_stock"] -= line["quantity"]
            number = max(int(i.split("-")[1]) for i in store.orders) + 1
            order = {
                "id": f"ord-{number}",
                "customer_id": order_in.customer_id,
                "placed_at": datetime.now(timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "status": "placed",
                "lines": lines,
                "total_cents": sum(l["quantity"] * l["unit_price_cents"]
                                   for l in lines),
                "currency": "USD",
            }
            store.orders[order["id"]] = order
            view = order_view(order)
            store.keys[scoped] = (fingerprint, 201, view)
    except Exception:
        with store.lock:
            store.keys.pop(scoped, None)
        raise
    request.state.idempotency = "new"
    return PrettyJSON(view, status_code=201,
                        headers={"Location": view["links"]["self"]["href"]})


def find_order(order_id):
    order = store.orders.get(order_id)
    if order is None:
        raise ProblemError(404, "order-not-found", "Order not found",
                           f"No order with id {order_id}.",
                           hint=f"Order IDs run from "
                                f"{id_range(store.orders)}.")
    return order


@app.get("/orders/{order_id}", operation_id="get_order_status",
         response_model=Order, summary="Get an order and its status",
         description="Report where one order is. `links` lists what "
                     "can be done with the order now.",
         responses={404: PROBLEM_RESPONSE}, tags=["orders"])
def get_order_status(order_id: OrderId):
    return order_view(find_order(order_id))


@app.get("/orders/{order_id}/cancellation", operation_id="get_cancellation",
         response_model=Cancellation, summary="Get an order's cancellation",
         description="Read the cancellation of a cancelled order.",
         responses={404: PROBLEM_RESPONSE}, tags=["orders"])
def get_cancellation(order_id: OrderId):
    find_order(order_id)
    if order_id not in store.cancellations:
        raise ProblemError(404, "cancellation-not-found",
                           "No cancellation",
                           f"Order {order_id} has not been cancelled here.")
    return store.cancellations[order_id]


@app.put("/orders/{order_id}/cancellation", operation_id="cancel_order",
         response_model=Cancellation, summary="Cancel an order",
         description="Cancel a placed or paid order and refund it in "
                     "full. Repeating the call returns the same "
                     "cancellation and refunds nothing more.",
         responses={404: PROBLEM_RESPONSE, 409: PROBLEM_RESPONSE},
         tags=["orders"])
def cancel_order(order_id: OrderId, body: CancellationIn,
                 request: Request):
    with store.lock:
        order = find_order(order_id)
        if order_id in store.cancellations:
            request.state.cancellation = "existing"
            return store.cancellations[order_id]
        if order["status"] not in ("placed", "paid"):
            raise ProblemError(
                409, "order-not-cancellable", "Order can't be cancelled",
                f"Order {order_id} is {order['status']}.",
                hint="Only placed or paid orders can be cancelled.")
        refunded = order["total_cents"] if order["status"] == "paid" else 0
        for line in order["lines"]:
            store.books[line["book_id"]]["in_stock"] += line["quantity"]
        order["status"] = "cancelled"
        record = {"order": f"/orders/{order_id}", "reason": body.reason,
                  "refunded_cents": refunded, "currency": order["currency"]}
        store.cancellations[order_id] = record
        request.state.cancellation = "new"
        return record


# -- the OpenAPI document ------------------------------------------------------

def harbor_openapi():
    """FastAPI's generated document, with its default 422 schema replaced
    by the Problem schema every error here actually uses."""
    if app.openapi_schema:
        return app.openapi_schema
    doc = get_openapi(title=app.title, version=app.version,
                      description=app.description, routes=app.routes,
                      servers=app.servers)
    schemas = doc["components"]["schemas"]
    schemas.pop("HTTPValidationError", None)
    schemas.pop("ValidationError", None)
    for path in doc["paths"].values():
        for op in path.values():
            bad = op["responses"].get("422")
            if bad and "HTTPValidationError" in json.dumps(bad):
                op["responses"]["422"] = {
                    "description": "Invalid request",
                    "content": {"application/problem+json": {"schema": {
                        "$ref": "#/components/schemas/Problem"}}}}
            for param in op.get("parameters", []):
                param["schema"].pop("title", None)
                if param["schema"].get("description") == param.get(
                        "description"):
                    param["schema"].pop("description")
            for code, resp in op["responses"].items():
                content = resp.get("content", {})
                if code >= "400" and "application/json" in content:
                    content["application/problem+json"] = content.pop(
                        "application/json")
                if code >= "400" and "application/problem+json" in content:
                    content["application/problem+json"].setdefault(
                        "schema", {"$ref": "#/components/schemas/Problem"})
    app.openapi_schema = json.loads(json.dumps(doc), parse_float=_number)
    return app.openapi_schema


def _number(text):
    """Pydantic writes le=10 as 10.0; keep whole numbers whole."""
    value = float(text)
    return int(value) if value.is_integer() else value


app.openapi = harbor_openapi


# -- logging and main -----------------------------------------------------------

class LogMiddleware:
    """One line per request: request line, status, idempotency outcome."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        status = {}

        async def capture(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        await self.inner(scope, receive, capture)
        path = scope["path"]
        if scope.get("query_string"):
            path += "?" + scope["query_string"].decode()
        state = scope.get("state", {})
        extra = "".join(f" {k}={state[k]}" for k in
                        ("idempotency", "cancellation") if k in state)
        print(f'"{scope["method"]} {path} HTTP/{scope["http_version"]}" '
              f'{status.get("code")}{extra}', file=sys.stderr, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--export", action="store_true",
                        help="write openapi.json and exit")
    args = parser.parse_args()
    if args.export:
        out = Path(__file__).with_name("openapi.json")
        out.write_text(json.dumps(app.openapi(), indent=2) + "\n")
        print(f"wrote {out.name}")
        return
    uvicorn.run(LogMiddleware(app), host="127.0.0.1", port=args.port,
                access_log=False, log_level="warning",
                server_header=False, date_header=True,
                headers=[("server", "harbor-ch03/1.0")])


if __name__ == "__main__":
    main()
