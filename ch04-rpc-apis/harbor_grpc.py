"""Harbor's order service over gRPC, on 127.0.0.1:8041.

    uv run python harbor_grpc.py [--port 8041]

Generated code comes from proto/harbor/orders/v1/orders.proto; run.sh
writes it to gen/ with grpc_tools.protoc. Server reflection is on, so
grpcurl can list and describe the service without the .proto file.
Each call is logged to stderr as: method, status code, and detail.
"""

import argparse
import sys
import time
from concurrent import futures
from pathlib import Path

sys.path.insert(0, str(Path(__file__).with_name("gen")))

import grpc  # noqa: E402
from grpc_reflection.v1alpha import reflection  # noqa: E402
from google.protobuf.timestamp_pb2 import Timestamp  # noqa: E402

from harbor.orders.v1 import orders_pb2 as pb  # noqa: E402
from harbor.orders.v1 import orders_pb2_grpc as pb_grpc  # noqa: E402

import harbor_orders as h  # noqa: E402

CODES = {
    "invalid_argument": grpc.StatusCode.INVALID_ARGUMENT,
    "not_found": grpc.StatusCode.NOT_FOUND,
    "out_of_stock": grpc.StatusCode.FAILED_PRECONDITION,
    "request_id_reused": grpc.StatusCode.FAILED_PRECONDITION,
}

STATUS = {name.removeprefix("ORDER_STATUS_").lower(): value
          for name, value in pb.OrderStatus.items()}


def log(method, code, note=""):
    print(f"{method} {code}{' ' + note if note else ''}",
          file=sys.stderr, flush=True)


def timestamp(text):
    ts = Timestamp()
    ts.FromJsonString(text)
    return ts


def to_status_reply(order):
    v = h.status_view(order)
    return pb.OrderStatusReply(
        order_id=v["order_id"], status=STATUS[v["status"]],
        placed_at=timestamp(v["placed_at"]),
        total_cents=v["total_cents"], currency=v["currency"])


def to_order(order):
    return pb.Order(
        order_id=order["id"], customer_id=order["customer_id"],
        status=STATUS[order["status"]],
        placed_at=timestamp(order["placed_at"]),
        lines=[pb.OrderLine(**line) for line in order["lines"]],
        total_cents=order["total_cents"], currency=order["currency"])


def fail(context, method, err):
    message = err.message + (f" {err.hint}" if err.hint else "")
    log(method, CODES[err.kind].name)
    context.abort(CODES[err.kind], message)


class OrderService(pb_grpc.OrderServiceServicer):
    def __init__(self, store):
        self.store = store

    def PlaceOrder(self, request, context):
        lines = [{"book_id": l.book_id, "quantity": l.quantity}
                 for l in request.lines]
        try:
            fingerprint, lines = self.store.check_order(
                request.customer_id, lines, request.request_id)
            earlier = self.store.replay(request.request_id, fingerprint)
            if earlier is not None:
                log("PlaceOrder", "OK", f"replay {earlier['id']}")
                return to_order(earlier)
            time.sleep(h.PAYMENT_SECONDS)  # the payment stand-in
            if not context.is_active():
                log("PlaceOrder", "DEADLINE_EXCEEDED", "order not created")
                return pb.Order()
            order, new = self.store.commit(
                request.customer_id, lines, request.request_id,
                fingerprint)
        except h.OrderError as err:
            fail(context, "PlaceOrder", err)
        log("PlaceOrder", "OK", f"{'new' if new else 'replay'} {order['id']}")
        return to_order(order)

    def GetOrderStatus(self, request, context):
        try:
            order = self.store.get_order(request.order_id)
        except h.OrderError as err:
            fail(context, "GetOrderStatus", err)
        log("GetOrderStatus", "OK", order["id"])
        return to_status_reply(order)

    def WatchOrder(self, request, context):
        try:
            for order in self.store.watch(request.order_id):
                if not context.is_active():
                    return
                yield to_status_reply(order)
        except h.OrderError as err:
            fail(context, "WatchOrder", err)
        log("WatchOrder", "OK", request.order_id)


def serve(port):
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    pb_grpc.add_OrderServiceServicer_to_server(OrderService(h.Store()),
                                               server)
    reflection.enable_server_reflection(
        (pb.DESCRIPTOR.services_by_name["OrderService"].full_name,
         reflection.SERVICE_NAME), server)
    server.add_insecure_port(f"127.0.0.1:{port}")
    server.start()
    print(f"harbor gRPC on 127.0.0.1:{port}", file=sys.stderr, flush=True)
    server.wait_for_termination()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8041)
    serve(parser.parse_args().port)
