"""Harbor Books' storefront GraphQL API (Chapter 5).

Strawberry builds the schema from the Python types below. Run it with
`uv run python harbor_graphql.py` and it serves POST /graphql on
127.0.0.1:8005. Options:

  --no-loaders        resolve each book's reviews one call at a time (N+1)
  --trusted-only      execute only operations listed in trusted-documents.json
  --no-introspection  refuse __schema and __type queries
  --export            write the schema's SDL to schema.graphql and exit
"""

import argparse
import asyncio
import contextvars
import hashlib
import json
import sys
from collections import Counter
from collections.abc import AsyncGenerator
from enum import Enum
from pathlib import Path

import strawberry
from cross_web import HTTPException
from graphql import GraphQLError
from strawberry.asgi import GraphQL
from strawberry.dataloader import DataLoader
from strawberry.extensions import (
    AddValidationRules, DisableIntrospection, QueryDepthLimiter,
)
from strawberry.http import GraphQLRequestData
from strawberry.types import Info

from limits import MAX_FIRST, make_cost_rule
from store import Store

HERE = Path(__file__).resolve().parent
MAX_DEPTH = 6
STORE = Store()
CALLS: contextvars.ContextVar[list[str]] = contextvars.ContextVar("calls")
OPERATION: contextvars.ContextVar[str] = contextvars.ContextVar("operation")


def backend(call):
    """Run a store call and record it against the current request."""
    def wrapper(*args):
        result = call(*args)
        CALLS.get([]).append(call.__name__)
        return result
    return wrapper


def not_found(kind: str, value: str, hint: str) -> GraphQLError:
    return GraphQLError(f"No {kind} with id {value}.",
                        extensions={"code": "NOT_FOUND", "hint": hint})


def check_first(first: int) -> None:
    if not 1 <= first <= MAX_FIRST:
        raise GraphQLError(f"first must be between 1 and {MAX_FIRST}.",
                           extensions={"code": "BAD_USER_INPUT",
                                       "field": "first"})


@strawberry.enum(description="Where an order is in its life.")
class OrderStatus(Enum):
    PLACED = "placed"
    PAID = "paid"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


@strawberry.type(description="A customer's review of one book.")
class Review:
    id: strawberry.ID
    rating: int = strawberry.field(description="1 to 5 stars.")
    text: str
    posted_at: str
    book_id: strawberry.Private[str]

    @strawberry.field(description="The book this review is about.")
    async def book(self, info: Info) -> "Book":
        return await load_book(info, self.book_id)


@strawberry.type(description="A book in Harbor's catalog.")
class Book:
    id: strawberry.ID
    title: str
    author: str
    first_published: int
    format: str
    price_cents: int = strawberry.field(description="Price in cents.")
    currency: str
    in_stock: int = strawberry.field(description="Copies on hand.")

    @strawberry.field(description="Reviews of this book, newest last.")
    async def reviews(self, info: Info, first: int = 10) -> list[Review]:
        check_first(first)
        loaders = info.context["loaders"]
        if loaders is None:
            rows = backend(STORE.reviews_for)(self.id)
        else:
            rows = await loaders["reviews"].load(self.id)
        return [make_review(r) for r in rows[:first]]


@strawberry.type(description="One line of an order.")
class OrderLine:
    quantity: int
    unit_price_cents: int
    book_id: strawberry.Private[str]

    @strawberry.field
    async def book(self, info: Info) -> Book:
        return await load_book(info, self.book_id)


@strawberry.type(description="A customer's order.")
class Order:
    id: strawberry.ID
    status: OrderStatus
    placed_at: str
    total_cents: int
    currency: str
    lines: list[OrderLine]


@strawberry.type
class OrderStatusChange:
    order_id: strawberry.ID
    status: OrderStatus


@strawberry.input
class LineInput:
    book_id: strawberry.ID = strawberry.field(
        description="Harbor book ID, such as bk-1001.")
    quantity: int = strawberry.field(description="1 to 10.")


@strawberry.input
class PlaceOrderInput:
    customer_id: strawberry.ID
    lines: list[LineInput]
    request_id: strawberry.ID = strawberry.field(
        description="A new UUID per order; reuse it only to retry.")


def make_book(row: dict) -> Book:
    return Book(id=row["id"], title=row["title"], author=row["author"],
                first_published=row["first_published"],
                format=row["format"], price_cents=row["price_cents"],
                currency=row["currency"], in_stock=row["in_stock"])


def make_review(row: dict) -> Review:
    return Review(id=row["id"], rating=row["rating"], text=row["text"],
                  posted_at=row["posted_at"], book_id=row["book_id"])


def make_order(row: dict) -> Order:
    return Order(
        id=row["id"], status=OrderStatus(row["status"]),
        placed_at=row["placed_at"], total_cents=row["total_cents"],
        currency=row["currency"],
        lines=[OrderLine(quantity=line["quantity"],
                         unit_price_cents=line["unit_price_cents"],
                         book_id=line["book_id"])
               for line in row["lines"]])


async def load_book(info: Info, book_id: str) -> Book:
    loaders = info.context["loaders"]
    if loaders is None:
        row = backend(STORE.get_book)(book_id)
    else:
        row = await loaders["books"].load(book_id)
    return make_book(row)


@strawberry.type
class Query:
    @strawberry.field(description="One book by ID.")
    def book(self, id: strawberry.ID) -> Book | None:
        row = backend(STORE.get_book)(id)
        if row is None:
            raise not_found("book", id,
                            "This catalog has IDs bk-1001 to bk-1008.")
        return make_book(row)

    @strawberry.field(description="Books in catalog order.")
    def books(self, first: int = 10) -> list[Book]:
        check_first(first)
        return [make_book(r) for r in backend(STORE.list_books)(first)]

    @strawberry.field(description="One order by ID, such as ord-5002.")
    def order(self, id: strawberry.ID) -> Order | None:
        row = backend(STORE.get_order)(id)
        if row is None:
            raise not_found("order", id,
                            "Order IDs run from ord-5001 to ord-5004.")
        return make_order(row)


@strawberry.type
class Mutation:
    @strawberry.mutation(description="Buy books. A retry with the same "
                         "requestId returns the first order.")
    def place_order(self, input: PlaceOrderInput) -> Order:
        lines = [{"book_id": line.book_id, "quantity": line.quantity}
                 for line in input.lines]
        try:
            row, _ = backend(STORE.place_order)(
                input.customer_id, lines, input.request_id)
        except LookupError as err:
            raise not_found("book", str(err.args[0]),
                            "This catalog has IDs bk-1001 to bk-1008.")
        except ValueError as err:
            raise GraphQLError(f"{err.args[0]} is out of stock.",
                               extensions={"code": "OUT_OF_STOCK"})
        return make_order(row)


@strawberry.type
class Subscription:
    @strawberry.subscription(description="Each status change of one order "
                             "until it ships (a stand-in on a timer).")
    async def order_status(
            self, order_id: strawberry.ID,
    ) -> AsyncGenerator[OrderStatusChange, None]:
        row = STORE.orders.get(order_id)
        if row is None:
            raise not_found("order", order_id,
                            "Order IDs run from ord-5001 to ord-5004.")
        for status in ("placed", "paid", "shipped"):
            await asyncio.sleep(0.1)
            yield OrderStatusChange(order_id=order_id,
                                    status=OrderStatus(status))


class HarborSchema(strawberry.Schema):
    def process_errors(self, errors, execution_context=None) -> None:
        for error in errors:
            code = (error.extensions or {}).get("code", "GRAPHQL")
            path = ".".join(str(p) for p in error.path or []) or "-"
            print(f"error {code} path={path}: {error.message}",
                  file=sys.stderr, flush=True)


def build_schema(introspection: bool = True) -> strawberry.Schema:
    extensions = [lambda: QueryDepthLimiter(max_depth=MAX_DEPTH),
                  lambda: AddValidationRules([make_cost_rule()])]
    if not introspection:
        extensions.append(DisableIntrospection)
    return HarborSchema(query=Query, mutation=Mutation,
                        subscription=Subscription,
                        extensions=extensions)


def load_trusted() -> dict[str, str]:
    return json.loads((HERE / "trusted-documents.json").read_text())


def document_id(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


class HarborGraphQL(GraphQL):
    def __init__(self, schema, loaders: bool, trusted_only: bool) -> None:
        super().__init__(schema, graphql_ide=None)
        self.use_loaders = loaders
        self.trusted = load_trusted() if trusted_only else None

    async def parse_http_body(self, request) -> GraphQLRequestData:
        body = json.loads(await request.get_body() or b"{}")
        OPERATION.set(body.get("operationName") or "-")
        doc_id = body.get("documentId")
        if self.trusted is not None:
            if doc_id is None:
                raise HTTPException(
                    400, "This server runs only trusted documents; "
                         "send a documentId.")
            if doc_id not in self.trusted:
                raise HTTPException(400, f"Unknown documentId {doc_id}.")
            query = self.trusted[doc_id]
        elif doc_id is not None:
            query = load_trusted().get(doc_id)
        else:
            query = body.get("query")
        return GraphQLRequestData(
            query=query, variables=body.get("variables"),
            operation_name=body.get("operationName"),
            extensions=body.get("extensions"), protocol="http")

    async def get_context(self, request, response) -> dict:
        loaders = None
        if self.use_loaders:
            loaders = {
                "books": DataLoader(load_fn=self.load_books),
                "reviews": DataLoader(load_fn=self.load_reviews),
            }
        return {"request": request, "response": response,
                "loaders": loaders}

    @staticmethod
    async def load_books(ids: list[str]) -> list[dict]:
        return backend(STORE.get_books)(ids)

    @staticmethod
    async def load_reviews(ids: list[str]) -> list[list[dict]]:
        return backend(STORE.reviews_for_many)(ids)


def logged(app):
    """ASGI wrapper: one log line per request with its back-end calls."""
    async def wrapped(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        calls: list[str] = []
        CALLS.set(calls)
        OPERATION.set("-")
        status = {}

        async def capture(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        await app(scope, receive, capture)
        detail = " ".join(f"{name}={n}"
                          for name, n in Counter(calls).items())
        print(f"{scope['method']} {scope['path']} {status.get('code')} "
              f"op={OPERATION.get()} backend_calls={len(calls)} {detail}"
              .rstrip(), file=sys.stderr, flush=True)
    return wrapped


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8005)
    parser.add_argument("--no-loaders", action="store_true")
    parser.add_argument("--trusted-only", action="store_true")
    parser.add_argument("--no-introspection", action="store_true")
    parser.add_argument("--export", action="store_true")
    args = parser.parse_args()

    schema = build_schema(introspection=not args.no_introspection)
    if args.export:
        (HERE / "schema.graphql").write_text(schema.as_str() + "\n")
        return

    import uvicorn
    app = logged(HarborGraphQL(schema, loaders=not args.no_loaders,
                               trusted_only=args.trusted_only))
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
