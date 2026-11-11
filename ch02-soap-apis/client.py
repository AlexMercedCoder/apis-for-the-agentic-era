"""A Harbor SOAP client generated at runtime from the WSDL by zeep.

    uv run python client.py ord-5002            # one order's status
    uv run python client.py ord-9999            # a SOAP fault
    uv run python client.py ord-5002 --history  # print both envelopes
"""

import sys

from zeep import Client
from zeep.exceptions import Fault
from zeep.plugins import HistoryPlugin
from zeep.wsse.username import UsernameToken

WSDL = "http://127.0.0.1:8002/harbor/soap?wsdl"
NS = "http://harbor.example/orders"

history = HistoryPlugin()
client = Client(
    WSDL,
    wsse=UsernameToken("partner-demo", "not-a-real-secret"),
    plugins=[history],
)

order_id = sys.argv[1] if len(sys.argv) > 1 else "ord-5002"
try:
    order = client.service.GetOrderStatus(orderId=order_id)
    print(f"{order.orderId}: {order.status} since {order.placedAt}, "
          f"{order.totalCents / 100:.2f} {order.currency}")
except Fault as fault:
    print(f"Fault {fault.code}: {fault.message}")
    if fault.detail is not None:
        hint = fault.detail.findtext(f"{{{NS}}}NotFound/{{{NS}}}hint")
        print("Hint:", hint)

if "--history" in sys.argv:
    from lxml import etree

    for label, entry in (("sent", history.last_sent),
                         ("received", history.last_received)):
        print(f"--- {label}")
        print(etree.tostring(entry["envelope"], pretty_print=True,
                             encoding="unicode"), end="")
