"""Run one Flight SQL query with PyArrow's plain Flight client, printing
each step: the command, the FlightInfo, the ticket, and the batches."""
import sys
from pathlib import Path

import pyarrow.flight as flight
from google.protobuf import any_pb2

sys.path.insert(0, str(Path(__file__).with_name("gen")))
import FlightSql_pb2 as sql  # noqa: E402

QUERY = ("SELECT order_id, placed_at, book_id, quantity, unit_price_cents "
         "FROM order_lines WHERE placed_at >= TIMESTAMPTZ '2026-01-01' "
         "ORDER BY placed_at")

command = any_pb2.Any()
command.Pack(sql.CommandStatementQuery(query=sys.argv[1] if len(sys.argv) > 1
                                       else QUERY))
client = flight.connect("grpc://127.0.0.1:8071")
descriptor = flight.FlightDescriptor.for_command(command.SerializeToString())
print("GetFlightInfo command:", command.type_url)
info = client.get_flight_info(descriptor)
print("schema:")
for field in info.schema:
    print(f"  {field.name}: {field.type}")
print("total_records:", info.total_records, " total_bytes:", info.total_bytes)
for endpoint in info.endpoints:
    print("endpoint:", [str(loc.uri, "utf-8") if isinstance(loc.uri, bytes)
                        else loc.uri for loc in endpoint.locations],
          "ticket:", len(endpoint.ticket.ticket), "bytes")
    reader = client.do_get(endpoint.ticket)
    for n, chunk in enumerate(reader, 1):
        print(f"DoGet batch {n}: {chunk.data.num_rows} rows")
