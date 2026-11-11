"""Agent tools generated from Harbor's pinned OpenAPI document.

Each operation in openapi.json becomes one tool in the Anthropic Messages
API format (name, description, input_schema), the format Chapter 1's
agent.py uses. The generator reads only the pinned file, never the running
server, so a change to the API reaches the model only through a reviewed
change to openapi.json.

  uv run python openapi_tools.py              # print every tool's name
  uv run python openapi_tools.py place_order  # print one tool in full

Every tool whose method isn't safe (RFC 9110 section 9.2.1) is marked
needs_approval, so a host can run reads freely and ask a person first for
anything that changes data.

The host, not the model, supplies the Idempotency-Key header: it is left out
of input_schema, and call_tool() makes one key per tool call and reuses it
on every retry of that call.
"""

import copy
import json
import re
import sys
import uuid
from pathlib import Path

import httpx

SPEC = json.loads(Path(__file__).with_name("openapi.json").read_text())
SAFE = {"get", "head", "options"}
NOTES = {
    "get": "Read-only; safe to retry.",
    "put": "Changes data. Idempotent; safe to retry.",
    "post": "Changes data. Not idempotent; the host makes retries "
            "safe.",
    "delete": "Deletes data. Idempotent; ask the user first.",
}
HOST_HEADERS = {"Idempotency-Key"}


def resolve(node):
    """Inline every local $ref so the model sees one self-contained schema."""
    if isinstance(node, dict):
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            return resolve(copy.deepcopy(SPEC["components"]["schemas"][name]))
        out = {k: resolve(v) for k, v in node.items() if k != "title"}
        return dict(sorted(out.items(), key=lambda kv: kv[0] != "type"))
    if isinstance(node, list):
        return [resolve(v) for v in node]
    return node


def tool_for(path, method, op):
    props, required = {}, []
    for param in op.get("parameters", []):
        if param["name"] in HOST_HEADERS:
            continue
        schema = resolve(param["schema"])
        schema["description"] = param.get("description", "")
        props[param["name"]] = schema
        if param.get("required"):
            required.append(param["name"])
    body = op.get("requestBody", {}).get("content", {})
    if "application/json" in body:
        schema = resolve(body["application/json"]["schema"])
        props.update(schema["properties"])
        required += schema.get("required", [])
    note = NOTES[method]
    return {
        "name": op["operationId"],
        "description": f"{op['summary']}. {op['description']} {note}",
        "input_schema": {"type": "object", "properties": props,
                         "required": required},
        "x-harbor": {"method": method.upper(), "path": path,
                     "needs_approval": method not in SAFE},
    }


TOOLS = [tool_for(path, method, op)
         for path, item in SPEC["paths"].items()
         for method, op in item.items()]


def for_model(tool):
    """The tool as the model receives it, without the host's routing."""
    return {k: v for k, v in tool.items() if k != "x-harbor"}


def call_tool(name, args, base=SPEC["servers"][0]["url"], attempts=2):
    """Turn a tool call into one HTTP request, retrying on timeouts."""
    tool = next(t for t in TOOLS if t["name"] == name)
    method, path = tool["x-harbor"]["method"], tool["x-harbor"]["path"]
    args = dict(args)
    for key in list(args):
        if "{" + key + "}" in path:
            path = path.replace("{" + key + "}", str(args.pop(key)))
    headers = {}
    if method == "POST":
        headers["Idempotency-Key"] = f'"{uuid.uuid4()}"'
    query = args if method == "GET" else None
    body = None if method == "GET" else args
    for attempt in range(attempts):
        try:
            response = httpx.request(method, base + path, params=query,
                                     json=body, headers=headers,
                                     timeout=5)
            return response.status_code, response.json()
        except httpx.TimeoutException:
            if attempt == attempts - 1:
                raise


def compact(text):
    """Put short arrays of strings on one line so a listing fits a page."""
    return re.sub(r'\[\n\s+("[^"\n]*"(?:,\n\s+"[^"\n]*")*)\n\s+\]',
                  lambda m: "[" + re.sub(r",\n\s+", ", ", m[1]) + "]",
                  text)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        tool = next(t for t in TOOLS if t["name"] == sys.argv[1])
        print(compact(json.dumps(for_model(tool), indent=2)))
    else:
        for t in TOOLS:
            flag = "ask first" if t["x-harbor"]["needs_approval"] else ""
            print(f"{t['name']:<18} {t['x-harbor']['method']:<4} "
                  f"{t['x-harbor']['path']:<32} {flag}".rstrip())
