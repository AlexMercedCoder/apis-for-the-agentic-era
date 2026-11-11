"""Harbor Books SOAP service, Chapter 2.

Implements the contract in harbor.wsdl (WSDL 1.1, SOAP 1.1 over HTTP,
document/literal wrapped):

    GET  /harbor/soap?wsdl   returns harbor.wsdl
    POST /harbor/soap        GetBook (public) and GetOrderStatus (needs a
                             WS-Security UsernameToken)

Standard library only. The credential below is a placeholder for the
book's examples; it protects nothing.
"""

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "harbor-data"
WSDL = (HERE / "harbor.wsdl").read_bytes()
BOOKS = {b["id"]: b for b in json.loads((DATA / "books.json").read_text())}
ORDERS = {o["id"]: o for o in json.loads((DATA / "orders.json").read_text())}

SOAP11 = "http://schemas.xmlsoap.org/soap/envelope/"
SOAP12 = "http://www.w3.org/2003/05/soap-envelope"
H = "http://harbor.example/orders"
WSSE = ("http://docs.oasis-open.org/wss/2004/01/"
        "oasis-200401-wss-wssecurity-secext-1.0.xsd")
PATH = "/harbor/soap"

PARTNER_USER = os.environ.get("HARBOR_PARTNER_USER", "partner-demo")
PARTNER_PASSWORD = os.environ.get("HARBOR_PARTNER_PASSWORD",
                                  "not-a-real-secret")

# The same patterns harbor.wsdl declares; XML Schema patterns are
# anchored at both ends, so fullmatch is the faithful check.
ORDER_ID = re.compile(r"ord-[0-9]{4}")
BOOK_ID = re.compile(r"bk-[0-9]{4}")

ET.register_namespace("soap", SOAP11)
ET.register_namespace("h", H)
ET.register_namespace("wsse", WSSE)


class Fault(Exception):
    def __init__(self, code, string, detail=None):
        super().__init__(string)
        self.code, self.string, self.detail = code, string, detail


def q(ns, name):
    return f"{{{ns}}}{name}"


def child(parent, name):
    return ET.SubElement(parent, q(H, name))


def leaf(parent, name, value):
    child(parent, name).text = str(value)


def not_found(kind, wanted, hint):
    detail = ET.Element(q(H, "NotFound"))
    leaf(detail, "id", wanted)
    leaf(detail, "hint", hint)
    return Fault("soap:Client", f"No {kind} with id {wanted}.", detail)


def invalid(field, value, pattern):
    """SOAP 1.1 section 4.4: a fault about the Body carries a detail."""
    detail = ET.Element(q(H, "InvalidArgument"))
    leaf(detail, "field", field)
    leaf(detail, "pattern", pattern)
    return Fault("soap:Client",
                 f"{field} {value!r} does not match {pattern}.", detail)


def read_username_token(header):
    """Return (username, password) from a wsse:Security header, if any."""
    if header is None:
        return None
    sec = header.find(q(WSSE, "Security"))
    if sec is None:
        return None
    user = sec.findtext(f"{q(WSSE, 'UsernameToken')}/{q(WSSE, 'Username')}")
    pw = sec.find(f"{q(WSSE, 'UsernameToken')}/{q(WSSE, 'Password')}")
    if pw is not None and not pw.get("Type", "").endswith("#PasswordText"):
        raise Fault("wsse:UnsupportedSecurityToken",
                    "Only PasswordText UsernameTokens are supported.")
    return user, (pw.text if pw is not None else None)


def check_headers(header):
    """SOAP 1.1 section 4.2.3: a header block marked mustUnderstand="1"
    that this service doesn't process must fail the whole message."""
    if header is None:
        return
    for block in header:
        if block.tag == q(WSSE, "Security"):
            continue
        if block.get(q(SOAP11, "mustUnderstand")) in ("1", "true"):
            raise Fault("soap:MustUnderstand",
                        f"Header {block.tag} was not understood.")


def get_book(op, _token):
    book_id = op.findtext(q(H, "bookId"), "")
    if not BOOK_ID.fullmatch(book_id):
        raise invalid("bookId", book_id, BOOK_ID.pattern)
    book = BOOKS.get(book_id)
    if book is None:
        raise not_found("book", book_id,
                        f"Book IDs run from {min(BOOKS)} to {max(BOOKS)}.")
    resp = ET.Element(q(H, "GetBookResponse"))
    leaf(resp, "bookId", book["id"])
    leaf(resp, "title", book["title"])
    leaf(resp, "author", book["author"])
    leaf(resp, "priceCents", book["price_cents"])
    leaf(resp, "currency", book["currency"])
    leaf(resp, "inStock", book["in_stock"])
    return resp


def get_order_status(op, token):
    if token != (PARTNER_USER, PARTNER_PASSWORD):
        raise Fault("wsse:FailedAuthentication",
                    "GetOrderStatus needs a valid partner UsernameToken.")
    order_id = op.findtext(q(H, "orderId"), "")
    if not ORDER_ID.fullmatch(order_id):
        raise invalid("orderId", order_id, ORDER_ID.pattern)
    order = ORDERS.get(order_id)
    if order is None:
        raise not_found("order", order_id,
                        f"Order IDs run from {min(ORDERS)} to "
                        f"{max(ORDERS)}.")
    resp = ET.Element(q(H, "GetOrderStatusResponse"))
    leaf(resp, "orderId", order["id"])
    leaf(resp, "status", order["status"])
    leaf(resp, "placedAt", order["placed_at"])
    leaf(resp, "totalCents", order["total_cents"])
    leaf(resp, "currency", order["currency"])
    return resp


OPERATIONS = {
    q(H, "GetBook"): get_book,
    q(H, "GetOrderStatus"): get_order_status,
}


def envelope(payload):
    env = ET.Element(q(SOAP11, "Envelope"))
    ET.SubElement(env, q(SOAP11, "Body")).append(payload)
    ET.indent(env)
    # One namespace declaration per line keeps the root tag readable.
    text = ET.tostring(env).replace(b" xmlns:", b"\n    xmlns:", 2)
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + text + b"\n"


def fault_envelope(fault):
    f = ET.Element(q(SOAP11, "Fault"))
    if fault.code.startswith("wsse:"):
        f.set("xmlns:wsse", WSSE)  # the faultcode's prefix must resolve
    ET.SubElement(f, "faultcode").text = fault.code
    ET.SubElement(f, "faultstring").text = fault.string
    if fault.detail is not None:
        ET.SubElement(f, "detail").append(fault.detail)
    return envelope(f)


def handle(body):
    try:
        env = ET.fromstring(body)
    except ET.ParseError as exc:
        raise Fault("soap:Client", f"Request is not well-formed XML: {exc}")
    if env.tag == q(SOAP12, "Envelope"):
        raise Fault("soap:VersionMismatch",
                    "This service speaks SOAP 1.1 only.")
    if env.tag != q(SOAP11, "Envelope"):
        raise Fault("soap:Client", "The root element must be soap:Envelope.")
    header = env.find(q(SOAP11, "Header"))
    check_headers(header)
    token = read_username_token(header)
    body_el = env.find(q(SOAP11, "Body"))
    if body_el is None or len(body_el) == 0:
        raise Fault("soap:Client", "The envelope has no Body content.")
    op = body_el[0]
    handler = OPERATIONS.get(op.tag)
    if handler is None:
        raise Fault("soap:Client", f"Unknown operation {op.tag}.")
    return envelope(handler(op, token))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def version_string(self):
        return "harbor-ch02/0.1"

    def do_GET(self):
        if self.path == PATH + "?wsdl":
            self.send(200, WSDL)
        else:
            self.send(404, b"Not found. The WSDL is at " +
                      PATH.encode() + b"?wsdl\n", "text/plain")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        if self.path != PATH:
            self.send(404, b"Not found.\n", "text/plain")
            return
        try:
            self.send(200, handle(body))
        except Fault as fault:
            # SOAP 1.1 section 6.2: a fault goes back as 500.
            self.send(500, fault_envelope(fault))

    def send(self, status, body, ctype='text/xml; charset="utf-8"'):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        action = self.headers.get("SOAPAction", "-").strip('"')
        line = (f'"{self.requestline}" {args[1]} '
                f'action={action.rsplit("/", 1)[-1]}')
        print(line, file=sys.stderr, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8002)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Harbor SOAP service on http://127.0.0.1:{args.port}{PATH}",
          file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
