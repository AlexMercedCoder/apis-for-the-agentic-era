"""An analytics tool for a model, backed by Flight SQL.

The model gets sales_by_book with two dates. The host validates them as
ISO dates and fills them into a fixed query, runs it over Flight SQL, and returns the
small aggregate as the tool result. The script also prints what the
same quarter's raw order lines would cost as a JSON tool result.
"""
import json
import sys
from datetime import date

import adbc_driver_flightsql.dbapi as flight_sql
import pyarrow as pa
import pyarrow.ipc as ipc

SALES_BY_BOOK = {
    "name": "sales_by_book",
    "description": (
        "Copies sold and revenue in cents per Harbor book between two "
        "dates (start inclusive, end exclusive), cancelled orders "
        "excluded. Read-only; returns at most one row per book."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "start": {"type": "string", "format": "date"},
            "end": {"type": "string", "format": "date"},
        },
        "required": ["start", "end"],
    },
}

AGGREGATE = """
SELECT book_id, sum(quantity)::BIGINT AS copies,
       sum(quantity * unit_price_cents)::BIGINT AS revenue_cents
FROM order_lines
WHERE status <> 'cancelled' AND placed_at >= TIMESTAMPTZ '{start}'
  AND placed_at < TIMESTAMPTZ '{end}'
GROUP BY book_id ORDER BY revenue_cents DESC
"""
RAW = """
SELECT * FROM order_lines
WHERE placed_at >= TIMESTAMPTZ '{start}' AND placed_at < TIMESTAMPTZ '{end}'
"""


def run(query):
    with flight_sql.connect("grpc://127.0.0.1:8071", autocommit=True) as c:
        with c.cursor() as cur:
            cur.execute(query)
            return cur.fetch_arrow_table()


def sales_by_book(start, end):
    # The host validates the model's arguments before building SQL.
    args = {"start": date.fromisoformat(start).isoformat(),
            "end": date.fromisoformat(end).isoformat()}
    return run(AGGREGATE.format(**args)).to_pylist()


def ipc_bytes(table):
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().size


if __name__ == "__main__":
    if "--definition" in sys.argv:
        print(json.dumps(SALES_BY_BOOK, indent=2))
        sys.exit()
    q3 = {"start": "2026-07-01", "end": "2026-10-01"}
    result = json.dumps(sales_by_book(**q3))
    raw = run(RAW.format(**q3))
    raw_json = json.dumps(raw.to_pylist(), default=str)
    print(f"tool result: {len(result):,} bytes of JSON, "
          f"{len(json.loads(result))} rows")
    print(f"raw lines:   {raw.num_rows:,} rows, {len(raw_json):,} bytes "
          f"of JSON, {ipc_bytes(raw):,} bytes as Arrow IPC")
