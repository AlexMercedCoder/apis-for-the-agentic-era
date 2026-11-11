"""Harbor's data layer for Chapter 5: the seed data behind a call counter.

Every function here stands for one query to a back end (a database or
another service). The GraphQL server counts the calls per request, so the
N+1 problem and its fix show up as numbers in the server log.
"""

import json
import threading
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "harbor-data"


class Store:
    def __init__(self) -> None:
        self.books = {b["id"]: b for b in self._load("books.json")}
        self.orders = {o["id"]: o for o in self._load("orders.json")}
        self.reviews = self._load("reviews.json")
        self.calls: list[str] = []
        self._lock = threading.Lock()
        self._next_order = 5005
        self._by_request_id: dict[str, str] = {}

    @staticmethod
    def _load(name: str) -> list[dict]:
        return json.loads((DATA / name).read_text())

    def _count(self, call: str) -> None:
        self.calls.append(call)

    # Reads ------------------------------------------------------------
    def get_order(self, order_id: str) -> dict | None:
        self._count(f"orders.get({order_id})")
        return self.orders.get(order_id)

    def list_books(self, first: int) -> list[dict]:
        self._count(f"books.list(first={first})")
        return list(self.books.values())[:first]

    def get_book(self, book_id: str) -> dict | None:
        self._count(f"books.get({book_id})")
        return self.books.get(book_id)

    def get_books(self, book_ids: list[str]) -> list[dict | None]:
        self._count(f"books.get_many({','.join(book_ids)})")
        return [self.books.get(i) for i in book_ids]

    def reviews_for(self, book_id: str) -> list[dict]:
        self._count(f"reviews.for_book({book_id})")
        return [r for r in self.reviews if r["book_id"] == book_id]

    def reviews_for_many(self, book_ids: list[str]) -> list[list[dict]]:
        self._count(f"reviews.for_books({','.join(book_ids)})")
        return [[r for r in self.reviews if r["book_id"] == i]
                for i in book_ids]

    # Writes -----------------------------------------------------------
    def place_order(self, customer_id: str, lines: list[dict],
                    request_id: str) -> tuple[dict, bool]:
        """Create an order, or return the first one for a retried ID."""
        self._count("orders.create")
        with self._lock:
            if request_id in self._by_request_id:
                return self.orders[self._by_request_id[request_id]], False
            priced = []
            for line in lines:
                book = self.books.get(line["book_id"])
                if book is None:
                    raise LookupError(line["book_id"])
                if book["in_stock"] < line["quantity"]:
                    raise ValueError(line["book_id"])
                priced.append({**line,
                               "unit_price_cents": book["price_cents"]})
            order_id = f"ord-{self._next_order}"
            self._next_order += 1
            order = {
                "id": order_id,
                "customer_id": customer_id,
                "placed_at": "2026-11-11T12:00:00Z",
                "status": "placed",
                "lines": priced,
                "total_cents": sum(line["quantity"] * line["unit_price_cents"]
                                   for line in priced),
                "currency": "USD",
            }
            self.orders[order_id] = order
            self._by_request_id[request_id] = order_id
            return order, True
