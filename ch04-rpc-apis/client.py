"""Place Harbor's order through the client generated from orders.proto.

    uv run python client.py

The first attempt gives the call 100 ms, less than the payment step
takes, so it fails with DEADLINE_EXCEEDED. The retry sends the same
request_id with a 2-second deadline, so the server can tell a retry
from a new order.
"""

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).with_name("gen")))

import grpc  # noqa: E402

from harbor.orders.v1 import orders_pb2 as pb  # noqa: E402
from harbor.orders.v1 import orders_pb2_grpc as pb_grpc  # noqa: E402

channel = grpc.insecure_channel("127.0.0.1:8041")
orders = pb_grpc.OrderServiceStub(channel)

request = pb.PlaceOrderRequest(
    customer_id="cus-001",
    lines=[pb.LineItem(book_id="bk-1002", quantity=1),
           pb.LineItem(book_id="bk-1008", quantity=2)],
    request_id=str(uuid.uuid4()),
)

for timeout in (0.1, 2.0):
    try:
        order = orders.PlaceOrder(request, timeout=timeout)
        break
    except grpc.RpcError as err:
        print(f"{err.code().name} after {timeout}s: {err.details()}")
else:
    sys.exit(1)

print(f"{order.order_id}: {pb.OrderStatus.Name(order.status)}, "
      f"{order.total_cents} cents")
