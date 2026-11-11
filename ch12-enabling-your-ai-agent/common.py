"""Pieces every Chapter 12 service shares: seed data, token checks, tracing.

Each service writes its OpenTelemetry spans (GenAI and MCP semantic
conventions, Development status as of November 2026) to its own JSON
Lines file under captures/, and show_trace.py joins them by trace ID.
"""

import base64
import json
import os
from pathlib import Path

import jwt
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (SimpleSpanProcessor,
                                            SpanExporter, SpanExportResult)

import issuer

HERE = Path(__file__).parent
DATA = HERE.parent / "harbor-data"
OUT = Path(os.environ.get("OUT", HERE / "captures"))
STATE = HERE / "state"
ORDERS = {o["id"]: o for o in json.loads((DATA / "orders.json").read_text())}
BOOKS = {b["id"]: b for b in json.loads((DATA / "books.json").read_text())}
CASES = STATE / "damage-cases.json"      # written by the fulfillment agent
REQUIRED = ["exp", "iat", "iss", "aud", "sub"]
HOST_HOOK = "http://127.0.0.1:8125/hooks/harbor"   # the one subscriber
HOST_SECRET = "whsec_" + base64.b64encode(          # placeholder secret
    b"not-a-real-secret-for-the-support-host").decode()


def check_token(token, audience):
    """Chapter 8's checks: RS256 pinned, at+jwt, issuer, audience, claims."""
    if jwt.get_unverified_header(token).get("typ") != "at+jwt":
        raise jwt.InvalidTokenError("not an access token")
    return jwt.decode(token, issuer.private_key().public_key(),
                      algorithms=["RS256"], audience=audience,
                      issuer=issuer.ISSUER, options={"require": REQUIRED})


def actors(claims):
    """The delegation chain, outermost actor first (RFC 8693 4.1)."""
    chain, act = [], claims.get("act")
    while act:
        chain.append(act["client_id"])
        act = act.get("act")
    return chain


def cases():
    return json.loads(CASES.read_text()) if CASES.exists() else {}


class JsonLines(SpanExporter):
    def __init__(self, name):
        self.path = OUT / f"trace-{name}.jsonl"

    def export(self, spans):
        with self.path.open("a") as f:
            for s in spans:
                f.write(json.dumps({
                    "service": s.resource.attributes["service.name"],
                    "name": s.name,
                    "trace_id": f"{s.context.trace_id:032x}",
                    "span_id": f"{s.context.span_id:016x}",
                    "parent": f"{s.parent.span_id:016x}" if s.parent
                    else None,
                    "start": s.start_time, "end": s.end_time,
                    "status": s.status.status_code.name,
                    "attributes": dict(s.attributes)}) + "\n")
        return SpanExportResult.SUCCESS


def tracer(name):
    provider = TracerProvider(resource=Resource.create(
        {"service.name": name}))
    provider.add_span_processor(SimpleSpanProcessor(JsonLines(name)))
    return provider.get_tracer(name)


def current_trace_id():
    ctx = trace.get_current_span().get_span_context()
    return f"{ctx.trace_id:032x}" if ctx.trace_id else None
