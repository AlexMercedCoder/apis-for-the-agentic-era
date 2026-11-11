"""What the analytics agent's principal can and can't do.

The principal holds only order_history_reader: NAMESPACE_LIST and
TABLE_LIST on the catalog, TABLE_READ_DATA on harbor.order_lines. It
loads that table and reads it with the credentials the catalog vends.
The catalog refuses another table and a commit (here a schema change);
the object store refuses a write with the vended credentials and a read
outside the table's location.
"""
import re
import textwrap

from pyiceberg.exceptions import ForbiddenError
from pyiceberg.types import StringType

from harbor_catalog import agent_credential, open_catalog
from pyarrow import fs

catalog = open_catalog(agent_credential(), name="analytics-agent")
table = catalog.load_table("harbor.order_lines")
print("load table: ok,", table.scan().to_arrow().num_rows, "rows read")

try:
    catalog.load_table("harbor.customers")
    print("load harbor.customers: allowed")
except ForbiddenError as err:
    print("load harbor.customers refused by the catalog:",
          str(err).split(":")[0])

try:
    with table.update_schema() as update:
        update.add_column("gift_note", StringType())
except ForbiddenError as err:
    print("commit refused by the catalog:")
    print(textwrap.fill(str(err).splitlines()[0], 68,
                        initial_indent="  ", subsequent_indent="  "))

io = table.io.properties
s3 = fs.S3FileSystem(access_key=io["s3.access-key-id"],
                     secret_key=io["s3.secret-access-key"],
                     session_token=io["s3.session-token"],
                     endpoint_override=io["s3.endpoint"], scheme="http",
                     region=io["client.region"])
path = "harbor-lake/harbor/order_lines/data/agent-was-here.txt"
try:
    with s3.open_output_stream(path) as f:
        f.write(b"x")
    print("write with the vended credentials: allowed")
except OSError as err:
    print("write with the vended credentials refused by the store:")
    found = re.search(r"AWS Error \w+ during \w+", str(err))
    print("  " + (found.group(0) if found else str(err)[:66]))

try:
    s3.get_file_info(fs.FileSelector("harbor-lake/harbor/customers",
                                      recursive=True))
    print("list harbor/customers with the vended credentials: allowed")
except OSError as err:
    print("list harbor/customers with the vended credentials refused "
          "by the store:")
    found = re.search(r"AWS Error \w+ during \w+", str(err))
    print("  " + (found.group(0) if found else str(err)[:66]))
