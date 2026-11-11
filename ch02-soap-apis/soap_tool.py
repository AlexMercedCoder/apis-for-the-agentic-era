"""Harbor's SOAP GetOrderStatus wrapped as one agent tool.

The tool definition is built from harbor.wsdl, so the model sees the
WSDL's own documentation, ID pattern, and status values. The host calls
the SOAP service through zeep and hands the model JSON, never XML.

    uv run python soap_tool.py    # print the tool and three tool results
"""

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from zeep import Client
from zeep.exceptions import Fault
from zeep.wsse.username import UsernameToken

HERE = Path(__file__).resolve().parent
WSDL_URL = "http://127.0.0.1:8002/harbor/soap?wsdl"
NS = {"w": "http://schemas.xmlsoap.org/wsdl/",
      "xs": "http://www.w3.org/2001/XMLSchema",
      "h": "http://harbor.example/orders"}


def build_tool(wsdl_path=HERE / "harbor.wsdl"):
    """Turn the WSDL's GetOrderStatus contract into a tool definition."""
    wsdl = ET.parse(wsdl_path).getroot()
    doc = wsdl.findtext(
        "w:portType/w:operation[@name='GetOrderStatus']/w:documentation",
        namespaces=NS)
    pattern = wsdl.find(
        ".//xs:simpleType[@name='OrderId']//xs:pattern",
        NS).get("value")
    statuses = [e.get("value") for e in wsdl.findall(
        ".//xs:simpleType[@name='OrderStatus']//xs:enumeration", NS)]
    description = " ".join(doc.split()).replace(
        " Requires a WS-Security UsernameToken for a Harbor partner"
        " account.", "")
    return {
        "name": "get_order_status",
        "description": (f"{description} Status is one of: "
                        f"{', '.join(statuses)}."),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "Harbor order ID, such as ord-5001",
                    "pattern": f"^{pattern}$",
                }
            },
            "required": ["order_id"],
        },
    }


TOOL = build_tool()
ORDER_ID = re.compile(TOOL["input_schema"]["properties"]["order_id"]
                      ["pattern"].strip("^$"))
_client = None


def soap_client():
    global _client
    if _client is None:
        _client = Client(WSDL_URL, wsse=UsernameToken("partner-demo",
                                                      "not-a-real-secret"))
    return _client


def run_tool(arguments):
    """Return (result, is_error) for one get_order_status call."""
    order_id = arguments.get("order_id", "")
    if not isinstance(order_id, str) or not ORDER_ID.fullmatch(order_id):
        return {"error": "invalid_argument",
                "message": "order_id must look like ord-5001."}, True
    try:
        o = soap_client().service.GetOrderStatus(orderId=order_id)
    except Fault as fault:
        result = {"error": "order_not_found"
                  if fault.detail is not None else "soap_fault",
                  "message": fault.message}
        if fault.detail is not None:
            result["hint"] = fault.detail.findtext(
                "h:NotFound/h:hint", namespaces=NS)
        return result, True
    return {"order_id": o.orderId, "status": o.status,
            "placed_at": o.placedAt.isoformat(),
            "total_cents": o.totalCents, "currency": o.currency}, False


if __name__ == "__main__":
    print("--- tool definition")
    print(json.dumps(TOOL, indent=2))
    for args in ({"order_id": "ord-5002"}, {"order_id": "ord-9999"},
                 {"order_id": "5002"}):
        result, is_error = run_tool(args)
        print(f"--- {json.dumps(args)} is_error={str(is_error).lower()}")
        print(json.dumps(result, indent=2))
