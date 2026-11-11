"""Harbor's order history as an Arrow table, one row per order line.

The four orders in harbor-data/orders.json come last. Before them, a
seeded generator writes 4,000 made-up historical orders (ord-1000 to
ord-4999, January 2025 to October 2026) for the same three customers
and eight books, so every run produces the same rows.
"""
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa

DATA = Path(__file__).resolve().parent.parent / "harbor-data"

SCHEMA = pa.schema([
    pa.field("order_id", pa.string(), nullable=False),
    pa.field("customer_id", pa.string(), nullable=False),
    pa.field("placed_at", pa.timestamp("us", tz="UTC"), nullable=False),
    pa.field("status", pa.string(), nullable=False),
    pa.field("book_id", pa.string(), nullable=False),
    pa.field("quantity", pa.int32(), nullable=False),
    pa.field("unit_price_cents", pa.int64(), nullable=False),
    pa.field("currency", pa.string(), nullable=False),
])


def ts(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def rows():
    books = json.loads((DATA / "books.json").read_text())
    customers = [c["id"] for c in
                 json.loads((DATA / "customers.json").read_text())]
    rng = random.Random(7)
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    span = (datetime(2026, 10, 27, tzinfo=timezone.utc) - start)
    span_s = int(span.total_seconds())
    times = sorted(rng.randrange(span_s) for _ in range(4000))
    for n, offset in enumerate(times):
        status = "cancelled" if rng.random() < 0.04 else "delivered"
        placed = start + timedelta(seconds=offset)
        customer = rng.choice(customers)
        for book in rng.sample(books, rng.choice((1, 1, 1, 2, 2, 3))):
            yield (f"ord-{1000 + n}", customer, placed, status, book["id"],
                   rng.choice((1, 1, 1, 1, 2, 2, 3)), book["price_cents"],
                   "USD")
    for o in json.loads((DATA / "orders.json").read_text()):
        for line in o["lines"]:
            yield (o["id"], o["customer_id"], ts(o["placed_at"]),
                   o["status"], line["book_id"], line["quantity"],
                   line["unit_price_cents"], o["currency"])


def table(start=0, stop=None):
    data = list(rows())[start:stop]
    return pa.Table.from_pylist(
        [dict(zip(SCHEMA.names, r)) for r in data], schema=SCHEMA)


if __name__ == "__main__":
    t = table()
    print(t.num_rows, "order lines,",
          len(set(t.column("order_id").to_pylist())), "orders")
