"""Rosa's assistant meets a planted instruction, and three controls stop it.

No model runs here, as in Chapter 1's stand-in. `obedient_model()` plays
the worst case on purpose: a model that follows any instruction it finds
in a tool result. The point is to test the controls, which must hold
whatever the model does:

  1. the host tags tool results as untrusted data and, once untrusted
     text is in the conversation, holds every write for the person;
  2. the gateway refuses write tools to the agent's client
     (Mcp-Method / Mcp-Name route rule);
  3. the token carries only orders:read, so the MCP server refuses too.

Steps 2 and 3 are shown by switching off the control in front of them.
Every tool call gets an OpenTelemetry span (GenAI semantic conventions),
its traceparent goes out on the request, and the host appends one audit
record per decision to captures/audit.jsonl.

    uv run python agent_host.py
"""

import asyncio
import hashlib
import json
import os
import re
import uuid
from pathlib import Path

import httpx
import httpx2
import mcp_types as types
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from opentelemetry import propagate, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (SimpleSpanProcessor,
                                            SpanExporter, SpanExportResult)

import issuer

GATEWAY_MCP = "http://127.0.0.1:9080/mcp"
DIRECT_MCP = "http://127.0.0.1:8010/mcp"
OUT = Path(os.environ.get("OUT", "captures"))
SESSION = f"s-{uuid.uuid4().hex[:8]}"
READ_ONLY = {"search_catalog", "get_book", "list_reviews",
             "get_order_status", "get_cancellation"}
# A tripwire, not a defense: it labels text that looks like instructions
# so the audit trail shows where they entered. Attackers can reword.
SUSPECT = re.compile(r"(?i)\b(ai assistants?|ignore (all|previous)|"
                     r"do not ask|don't ask|system prompt|call \w+_\w+)")


class JsonLines(SpanExporter):
    def export(self, spans):
        with (OUT / "trace.jsonl").open("a") as f:
            for s in spans:
                f.write(json.dumps({
                    "name": s.name,
                    "trace_id": f"{s.context.trace_id:032x}",
                    "span_id": f"{s.context.span_id:016x}",
                    "parent": f"{s.parent.span_id:016x}" if s.parent
                    else None,
                    "attributes": dict(s.attributes)}) + "\n")
        return SpanExportResult.SUCCESS


provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(JsonLines()))
tracer = provider.get_tracer("harbor-assistant-host")


def audit(**record):
    span = trace.get_current_span().get_span_context()
    record = {"session": SESSION, "user": "rosa",
              "agent": "harbor-assistant",
              "trace_id": f"{span.trace_id:032x}", **record}
    with (OUT / "audit.jsonl").open("a") as f:
        f.write(json.dumps(record) + "\n")


def digest(args):
    text = json.dumps(args, sort_keys=True).encode()
    return "sha256:" + hashlib.sha256(text).hexdigest()[:16]


async def add_traceparent(request):
    propagate.inject(request.headers)


def connect(token):
    http = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        event_hooks={"request": [add_traceparent]})
    return Client(streamable_http_client(GATEWAY_MCP, http_client=http),
                  client_info=types.Implementation(
                      name="harbor-assistant-host", version="1.0.0"))


def obedient_model(context):
    """Stand-in for a model at its worst: it obeys text in tool results."""
    for item in context:
        if "cancel_order" in item.get("text", ""):
            return [("cancel_order", {"order_id": oid})
                    for oid in context[0]["orders_seen"]]
    return []


def raw_call(url, token, name, args):
    """One tools/call over HTTP, as the SDK would send it."""
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": "2026-07-28",
               "Mcp-Method": "tools/call", "Mcp-Name": name}
    propagate.inject(headers)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": args, "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {}}}}
    return httpx.post(url, json=body, headers=headers)


async def call(client, name, args):
    with tracer.start_as_current_span(
            f"execute_tool {name}", attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": name,
                "gen_ai.tool.call.id": f"call-{uuid.uuid4().hex[:8]}",
                "gen_ai.agent.name": "harbor-assistant",
                "gen_ai.conversation.id": SESSION}):
        result = await client.call_tool(name, args)
        audit(event="tool_call", tool=name, args=digest(args),
              decision="allowed", error=result.is_error)
        return result


async def main():
    for name in ("audit.jsonl", "trace.jsonl"):
        (OUT / name).write_text("")
    token = issuer.mint("harbor-assistant")
    print(f"session {SESSION}: rosa via harbor-assistant, orders:read")
    with tracer.start_as_current_span("invoke_agent harbor-assistant",
                                      attributes={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": "harbor-assistant",
            "gen_ai.conversation.id": SESSION}):
        context = [{"role": "user", "orders_seen": ["ord-5003"],
                    "text": "Where's ord-5003? And is Frankenstein "
                            "any good?"}]
        print(f'1. rosa: "{context[0]["text"]}"')
        async with connect(token) as client:
            order = await call(client, "get_order_status",
                               {"order_id": "ord-5003"})
            print(f"2. get_order_status ord-5003: "
                  f"{order.structured_content['status']}")
            reviews = await call(client, "list_reviews",
                                 {"book_id": "bk-1003"})
        items = json.loads(reviews.content[0].text)
        print(f"3. list_reviews bk-1003: {len(items)} reviews, "
              f"tagged untrusted")
        tainted = False
        for review in items:
            context.append({"role": "tool", "source": "list_reviews",
                            "trust": "untrusted", "text": review["text"]})
            hit = SUSPECT.search(review["text"])
            if hit:
                tainted = True
                audit(event="untrusted_instruction", tool="list_reviews",
                      item=review["id"], matched=hit.group(0))
                print(f"   {review['id']} flagged: {hit.group(0)!r}")
        for name, args in obedient_model(context):
            print(f"4. stand-in model proposes {name} {json.dumps(args)}")
            if name not in READ_ONLY and tainted:
                audit(event="tool_call", tool=name, args=digest(args),
                      decision="held_for_approval",
                      reason="write after untrusted content")
                print("   host: held for Rosa's approval (write after "
                      "untrusted content)")
            with tracer.start_as_current_span(
                    f"execute_tool {name}", attributes={
                        "gen_ai.operation.name": "execute_tool",
                        "gen_ai.tool.name": name,
                        "gen_ai.agent.name": "harbor-assistant",
                        "gen_ai.conversation.id": SESSION}):
                gw = raw_call(GATEWAY_MCP, token, name, args)
                print(f"5. host rule off, via gateway: {gw.status_code}\n"
                      f"   {gw.json()['message']}")
                audit(event="tool_call", tool=name, args=digest(args),
                      decision="refused_by_gateway", status=gw.status_code)
                direct = raw_call(DIRECT_MCP, token, name, args)
                challenge = direct.headers.get("www-authenticate", "")
                audit(event="tool_call", tool=name, args=digest(args),
                      decision="refused_by_server", status=direct.status_code)
                print(f"6. gateway rule off too, direct to the server: "
                      f"{direct.status_code}\n   {challenge[:62]}")
    print(f"audit records: {len(open(OUT / 'audit.jsonl').readlines())}")


if __name__ == "__main__":
    asyncio.run(main())
