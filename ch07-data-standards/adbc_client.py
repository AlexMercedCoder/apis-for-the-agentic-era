"""The same kind of query through ADBC's Flight SQL driver, as a
DB-API cursor that returns an Arrow table."""
import sys

import adbc_driver_flightsql.dbapi as flight_sql

QUERY = """
SELECT book_id, sum(quantity)::BIGINT AS copies,
       sum(quantity * unit_price_cents)::BIGINT AS revenue_cents
FROM order_lines
WHERE status <> 'cancelled'
  AND placed_at >= TIMESTAMPTZ '2026-07-01'
  AND placed_at <  TIMESTAMPTZ '2026-10-01'
GROUP BY book_id ORDER BY revenue_cents DESC
"""

with flight_sql.connect("grpc://127.0.0.1:8071", autocommit=True) as conn:
    with conn.cursor() as cur:
        cur.execute(sys.argv[1] if len(sys.argv) > 1 else QUERY)
        result = cur.fetch_arrow_table()
print(type(result).__name__, result.num_rows, "rows")
print(result.schema)
for row in result.to_pylist():
    print(f"{row['book_id']}  {row['copies']:>4}  {row['revenue_cents']:>8}")
