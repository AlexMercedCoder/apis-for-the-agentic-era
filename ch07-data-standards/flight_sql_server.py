"""Harbor's Arrow Flight SQL server on grpc://127.0.0.1:8071.

It answers the two Flight SQL calls a query needs: GetFlightInfo with a
CommandStatementQuery, and DoGet with the TicketStatementQuery it handed
out. Each query loads harbor.order_lines from the Iceberg REST catalog
as the read-only analytics-agent principal, reads the data files with
the credentials the catalog vends, and runs the SQL in DuckDB.
The Flight endpoint itself has no authentication in this demo.
"""
import sys
import uuid
from pathlib import Path

import duckdb
import pyarrow.flight as flight
from google.protobuf import any_pb2

sys.path.insert(0, str(Path(__file__).with_name("gen")))
import FlightSql_pb2 as sql  # noqa: E402

from harbor_catalog import agent_credential, open_catalog  # noqa: E402

LOCATION = "grpc://127.0.0.1:8071"
BATCH_ROWS = 2048


def pack(message):
    wrapper = any_pb2.Any()
    wrapper.Pack(message)
    return wrapper.SerializeToString()


class HarborFlightSql(flight.FlightServerBase):
    def __init__(self):
        super().__init__(LOCATION)
        self.catalog = open_catalog(agent_credential(), direct=True,
                                    name="flight-sql")
        self.results = {}

    def _run(self, query):
        table = self.catalog.load_table("harbor.order_lines")
        snapshot = table.current_snapshot().snapshot_id
        con = duckdb.connect(config={"TimeZone": "UTC"})
        con.register("order_lines", table.scan().to_arrow())
        # No file or network access from SQL, and no way to turn it back
        # on: the only data a query can reach is the table above.
        con.execute("SET enable_external_access = false")
        result = con.sql(query).to_arrow_table()
        print(f"query on snapshot {snapshot}: {result.num_rows} rows",
              file=sys.stderr, flush=True)
        return result

    def get_flight_info(self, context, descriptor):
        command = any_pb2.Any()
        command.ParseFromString(descriptor.command)
        if not command.Is(sql.CommandStatementQuery.DESCRIPTOR):
            raise flight.FlightServerError(
                f"unsupported command {command.type_url}")
        query = sql.CommandStatementQuery()
        command.Unpack(query)
        try:
            result = self._run(query.query)
        except duckdb.Error as err:
            raise flight.FlightServerError(str(err).splitlines()[0])
        handle = uuid.uuid4().bytes
        self.results[handle] = result
        ticket = pack(sql.TicketStatementQuery(statement_handle=handle))
        endpoint = flight.FlightEndpoint(ticket, [LOCATION])
        return flight.FlightInfo(result.schema, descriptor, [endpoint],
                                 result.num_rows, result.nbytes)

    def do_get(self, context, ticket):
        command = any_pb2.Any()
        command.ParseFromString(ticket.ticket)
        statement = sql.TicketStatementQuery()
        command.Unpack(statement)
        result = self.results.pop(statement.statement_handle, None)
        if result is None:
            raise flight.FlightServerError("unknown or used ticket")
        return flight.RecordBatchStream(
            result.combine_chunks().to_reader(max_chunksize=BATCH_ROWS))


if __name__ == "__main__":
    server = HarborFlightSql()
    print(f"Harbor Flight SQL listening on {LOCATION}", file=sys.stderr,
          flush=True)
    server.serve()
