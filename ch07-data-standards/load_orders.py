"""Create harbor.order_lines and load the order history in two commits:
the generated history first, then the four orders from harbor-data."""
from pyiceberg.io.pyarrow import pyarrow_to_schema
from pyiceberg.table.name_mapping import MappedField, NameMapping

import order_history
from harbor_catalog import open_catalog

catalog = open_catalog()
catalog.create_namespace_if_not_exists("harbor")
mapping = NameMapping([MappedField(field_id=i, names=[name]) for i, name
                       in enumerate(order_history.SCHEMA.names, 1)])
table = catalog.create_table(
    "harbor.order_lines",
    schema=pyarrow_to_schema(order_history.SCHEMA, name_mapping=mapping))
history = order_history.table()
seed_rows = 6  # the four orders in harbor-data have six lines
table.append(history.slice(0, history.num_rows - seed_rows))
table.append(history.slice(history.num_rows - seed_rows))
for s in catalog.load_table("harbor.order_lines").snapshots():
    print(s.snapshot_id, s.summary.operation.value,
          s.summary["added-records"], "rows")
