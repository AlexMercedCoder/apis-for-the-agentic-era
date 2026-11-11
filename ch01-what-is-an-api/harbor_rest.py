"""Harbor Books REST API, Chapter 1 version: one read-only endpoint.

GET /books/{id} returns one book from ../harbor-data/books.json.
Standard library only, so it runs on any Python 3.13 or later.
Chapter 3 builds the full REST API; this file stays small on purpose.
"""

import argparse
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "harbor-data" / "books.json"
BOOKS = {book["id"]: book for book in json.loads(DATA.read_text())}
BOOK_PATH = re.compile(r"^/books/(?P<book_id>[^/]+)$")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def version_string(self):
        return "harbor-ch01/0.1"

    def do_GET(self):
        match = BOOK_PATH.match(self.path)
        if not match:
            self.send_json(404, {"error": "not_found",
                                 "message": f"No route for {self.path}."})
            return
        book_id = match["book_id"]
        book = BOOKS.get(book_id)
        if book is None:
            self.send_json(404, {
                "error": "book_not_found",
                "message": f"No book with id {book_id}.",
                "hint": "Book IDs look like bk-1001.",
            })
            return
        self.send_json(200, book)

    def send_json(self, status, payload):
        body = json.dumps(payload, indent=2).encode() + b"\n"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        agent = self.headers.get("User-Agent", "-")
        line = f'"{self.requestline}" {args[1]} ua="{agent}"'
        print(line, file=sys.stderr, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Harbor REST on http://127.0.0.1:{args.port}", file=sys.stderr,
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
