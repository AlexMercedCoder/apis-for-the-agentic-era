"""Two writers load the same snapshot of harbor.order_lines and each
append one new order. The second commit's requirement no longer holds,
so the catalog answers 409 Conflict. PyIceberg reloads the table,
reapplies the append on the new snapshot, and commits again."""
from datetime import datetime, timezone

import pyarrow as pa
import logging

import order_history
from harbor_catalog import open_catalog


def order(order_id, lines):
    placed = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)
    return pa.Table.from_pylist(
        [{"order_id": order_id, "customer_id": "cus-001",
          "placed_at": placed, "status": "placed", "book_id": book,
          "quantity": qty, "unit_price_cents": price, "currency": "USD"}
         for book, qty, price in lines], schema=order_history.SCHEMA)


logging.basicConfig(format="%(name)s: %(message)s")
a = open_catalog(name="writer-a").load_table("harbor.order_lines")
b = open_catalog(name="writer-b").load_table("harbor.order_lines")
start = a.current_snapshot().snapshot_id
print("both writers loaded snapshot", start)

a.append(order("ord-5005", [("bk-1002", 1, 1199), ("bk-1008", 2, 1299)]))
print("writer A committed snapshot", a.current_snapshot().snapshot_id)

b.append(order("ord-5006", [("bk-1001", 1, 1499)]))
snap = b.current_snapshot()
print("writer B committed snapshot", snap.snapshot_id,
      "with parent", snap.parent_snapshot_id)
