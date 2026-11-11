"""A logging reverse proxy for the Iceberg REST catalog.

Listens on 127.0.0.1:8180, forwards every request to Polaris on
127.0.0.1:8181, and appends one JSON line per exchange to the log file
given as the first argument. Tokens and storage secrets are replaced
with placeholders before anything is written.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

TARGET = "http://127.0.0.1:8181"
SECRET_KEYS = ("secret", "session-token", "access_token", "token",
               "access-key-id")
KEEP_KEYS = ("s3.session-token-expires-at-ms",)
KEEP_HEADERS = ("content-type", "x-iceberg-access-delegation")


def redact(value):
    if isinstance(value, dict):
        return {k: ("<redacted>" if any(s in k.lower() for s in SECRET_KEYS)
                    and k not in KEEP_KEYS and isinstance(v, str)
                    else redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def body_of(raw, content_type):
    if not raw:
        return None
    if "json" in (content_type or ""):
        return redact(json.loads(raw))
    return "<form data, redacted>"


class Proxy(BaseHTTPRequestHandler):
    client = httpx.Client(base_url=TARGET, timeout=30)

    def _forward(self):
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ("host", "content-length")}
        r = self.client.request(self.command, self.path, content=raw,
                                headers=headers)
        entry = {
            "request": f"{self.command} {self.path}",
            "headers": {k.lower(): v for k, v in self.headers.items()
                        if k.lower() in KEEP_HEADERS},
            "body": body_of(raw, self.headers.get("content-type")),
            "status": r.status_code,
            "response": body_of(r.content, r.headers.get("content-type")),
        }
        with open(sys.argv[1], "a") as log:
            log.write(json.dumps(entry) + "\n")
        self.send_response(r.status_code)
        for k, v in r.headers.items():
            if k.lower() not in ("content-length", "transfer-encoding",
                                 "content-encoding", "connection"):
                self.send_header(k, v)
        self.send_header("content-length", str(len(r.content)))
        self.end_headers()
        self.wfile.write(r.content)

    do_GET = do_POST = do_DELETE = do_HEAD = _forward

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8180), Proxy).serve_forever()
