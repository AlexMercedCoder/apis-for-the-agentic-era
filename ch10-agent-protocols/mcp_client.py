"""An MCP client for Harbor's server, using the official Python SDK.

It starts with no token, follows the 401 to the Protected Resource Metadata
and then to Keycloak, signs Rosa in with the authorization code flow, PKCE,
and a resource indicator for the MCP endpoint, and then talks MCP
2026-07-28 over Streamable HTTP: server/discover, tools/list, tools/call,
resources/read, prompts/get, and a place_order that asks Rosa first. No
model is involved: the program makes the calls a host would make for one.
Every JSON-RPC message on the wire is appended to captures/mcp-wire.jsonl.

    uv run python mcp_client.py
"""

import asyncio
import json
import os
import re
from pathlib import Path

import httpx
import httpx2
import mcp_types as types
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from login import claims as token_claims, sign_in

MCP = "http://127.0.0.1:8010/mcp"
WIRE = Path(os.environ.get("OUT", "captures")) / "mcp-wire.jsonl"


def short(token):
    return f"<JWT {len(token)} chars>"


def discover_and_sign_in():
    first = httpx.post(MCP, json={"jsonrpc": "2.0", "id": 1,
                                  "method": "server/discover"},
                       headers={"Accept": "application/json, "
                                          "text/event-stream"})
    challenge = first.headers["www-authenticate"]
    print(f"1. no token: {first.status_code}\n   www-authenticate: "
          f"{challenge}")
    prm_url = re.search(r'resource_metadata="([^"]+)"', challenge)[1]
    prm = httpx.get(prm_url).json()
    print(f"2. {prm_url}\n   {json.dumps(prm)}")
    issuer = prm["authorization_servers"][0]
    meta = httpx.get(f"{issuer}/.well-known/oauth-authorization-server"
                     ).json()
    print(f"3. authorization server {meta['issuer']}, PKCE "
          f"{meta['code_challenge_methods_supported']}")
    scope = " ".join(prm["scopes_supported"])
    resp = sign_in(meta, prm["resource"], scope, "rosa")
    token = resp.json()["access_token"]
    claims = token_claims(token)
    print(f"4. rosa signed in: token {short(token)}, aud {claims['aud']}, "
          f"scope {claims['scope']!r}, customer_id {claims['customer_id']}")
    other = sign_in(meta, "http://127.0.0.1:9999/mcp", scope, "rosa")
    print(f"5. a token for another resource: {other.status_code} "
          f"{other.json()['error']}")
    return token


async def log_request(request):
    if request.method == "POST" and request.url.path == "/mcp":
        with WIRE.open("a") as f:
            f.write(json.dumps({"dir": "send", "headers": {
                k: v for k, v in request.headers.items()
                if k.lower().startswith("mcp-")},
                "body": json.loads(request.content)}) + "\n")


async def log_response(response):
    if response.request.url.path == "/mcp":
        await response.aread()
        if response.content:
            with WIRE.open("a") as f:
                f.write(json.dumps({"dir": "recv",
                                    "status": response.status_code,
                                    "body": json.loads(response.content)})
                        + "\n")


async def rosa_says_yes(context, params):
    """Stands in for Rosa clicking Yes on the host's confirmation form."""
    print(f"   form shown to Rosa: {params.message}")
    return types.ElicitResult(action="accept", content={"confirm": True})


async def session(token):
    WIRE.write_text("")
    http = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        event_hooks={"request": [log_request], "response": [log_response]})
    async with Client(streamable_http_client(MCP, http_client=http),
                      elicitation_callback=rosa_says_yes,
                      client_info=types.Implementation(
                          name="harbor-demo-host", version="1.0.0")
                      ) as client:
        tools = await client.list_tools()
        print(f"6. tools/list: {len(tools.tools)} tools")
        for tool in tools.tools:
            a = tool.annotations
            mark = "read-only" if a.read_only_hint else (
                "destructive" if a.destructive_hint else "writes")
            print(f"   {tool.name:<17} {mark}")
        mine = await client.call_tool("get_order_status",
                                      {"order_id": "ord-5003"})
        print(f"7. get_order_status ord-5003: "
              f"{mine.structured_content['status']}")
        theirs = await client.call_tool("get_order_status",
                                        {"order_id": "ord-5002"})
        print(f"8. get_order_status ord-5002 (Sam's): is_error="
              f"{theirs.is_error} {theirs.content[0].text}")
        book = await client.read_resource("harbor://books/bk-1001")
        print(f"9. resources/read harbor://books/bk-1001: "
              f"{json.loads(book.contents[0].text)['title']}")
        prompt = await client.get_prompt("where_is_my_order",
                                         {"order_id": "ord-5003"})
        print(f"10. prompts/get where_is_my_order: "
              f"{prompt.messages[0].content.text[:60]}...")
        print("11. place_order, which asks Rosa first:")
        order = await client.call_tool("place_order", {"lines": [
            {"book_id": "bk-1003", "quantity": 1}]})
        data = order.structured_content
        print(f"   {data['id']} for {data['customer_id']}: "
              f"{data['status']}, {data['total_cents']} cents")


if __name__ == "__main__":
    asyncio.run(session(discover_and_sign_in()))
