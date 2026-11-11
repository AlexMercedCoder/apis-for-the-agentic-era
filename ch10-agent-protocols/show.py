"""Print the chapter's listings from the captures, trimmed to 72 columns.

Every listing comes from captures/ written by run.sh: the MCP wire log
(mcp-wire.jsonl), the Agent Card, the A2A stream, and the server logs.
Trims are marked with "..." and long string values are shortened.

    uv run python show.py
"""

import json
import os
import textwrap
from pathlib import Path

CAP = Path(os.environ.get("OUT", "captures"))
WIDTH = 72


def wire():
    return [json.loads(line) for line in
            (CAP / "mcp-wire.jsonl").read_text().splitlines()]


def message(method, direction="send", nth=0, name=None):
    """The nth sent request for a method, or the response that answers it."""
    log = wire()
    hits = [i for i, m in enumerate(log) if m["dir"] == "send"
            and m["body"]["method"] == method
            and (name is None or m["body"]["params"].get("name") == name)]
    i = hits[nth]
    return log[i] if direction == "send" else log[i + 1]


def shorten(value, room):
    if isinstance(value, str) and len(value) > room:
        return value[:max(room - 3, 8)] + "..."
    return value


def fit(obj, indent=0):
    """json.dumps(indent=2), shortening strings so no line passes WIDTH."""
    pad = " " * indent
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        lines = ["{"]
        items = list(obj.items())
        for n, (k, v) in enumerate(items):
            comma = "," if n < len(items) - 1 else ""
            key = f'{pad}  "{k}": '
            if isinstance(v, (dict, list)) and v:
                inline = json.dumps(v)
                if len(key) + len(inline) + 1 <= WIDTH and len(inline) < 40:
                    lines.append(key + inline + comma)
                else:
                    lines.append(key + fit(v, indent + 2) + comma)
            else:
                room = WIDTH - len(key) - 3 - len(comma)
                lines.append(key + json.dumps(shorten(v, room)) + comma)
        lines.append(pad + "}")
        return "\n".join(lines)
    if isinstance(obj, list):
        lines = ["["]
        for n, v in enumerate(obj):
            comma = "," if n < len(obj) - 1 else ""
            if isinstance(v, (dict, list)):
                lines.append(pad + "  " + fit(v, indent + 2) + comma)
            else:
                lines.append(pad + "  " + json.dumps(
                    shorten(v, WIDTH - indent - 6)) + comma)
        lines.append(pad + "]")
        return "\n".join(lines)
    return json.dumps(obj)


def wrap(text):
    out = []
    for line in text.splitlines():
        indent = " " * (len(line) - len(line.lstrip()) + 4)
        out += textwrap.wrap(line, WIDTH, subsequent_indent=indent,
                             break_on_hyphens=False) or [""]
    return "\n".join(out)


def section(title, text):
    print(f"=== {title}")
    for line in text.splitlines():
        assert len(line) <= WIDTH, f"{title}: {len(line)} > {WIDTH}: {line}"
    print(text)
    print()


def main():
    discover = message("server/discover")
    answer = message("server/discover", "recv")["body"]
    section("server/discover request", fit(discover["body"]))
    result = dict(answer["result"])
    result.pop("instructions", None)
    section("server/discover response", fit(
        {"jsonrpc": "2.0", "id": answer["id"], "result": result}))

    tools = message("tools/list", "recv")["body"]["result"]["tools"]
    place = next(t for t in tools if t["name"] == "place_order")
    place["description"] = shorten(place["description"], 40)
    lines = place["inputSchema"]["properties"]["lines"]
    lines["items"] = {"...": "book_id and quantity, as in Chapter 3"}
    section("tools/list: place_order", fit(place))

    asked = message("tools/call", "recv", 0, "place_order")["body"]
    state = asked["result"]["requestState"]
    asked["result"]["requestState"] = f"<sealed, {len(state)} characters>"
    asked["result"].pop("_meta", None)
    section("tools/call place_order: input_required", fit(asked))
    retry = message("tools/call", "send", 1, "place_order")["body"]
    params = retry["params"]
    section("tools/call place_order: the retry's params", fit({
        "name": params["name"], "arguments": params["arguments"],
        "inputResponses": params["inputResponses"],
        "requestState": "<echoed unchanged>"}))

    section("MCP server log",
            wrap((CAP / "mcp-server.log").read_text().strip()))
    section("MCP client output",
            wrap((CAP / "mcp-client.out").read_text().strip()))

    card = json.loads((CAP / "agent-card.json").read_text())
    card["description"] = shorten(card["description"], 40)
    card["skills"][0]["description"] = shorten(
        card["skills"][0]["description"], 44)
    card["skills"][0].pop("examples", None)
    section("Agent Card", fit(card))

    out = (CAP / "a2a-ship.out").read_text().splitlines()
    request = json.loads(out[1].split("request: ", 1)[1])
    request["params"]["message"]["messageId"] = shorten(
        request["params"]["message"]["messageId"], 12)
    section("SendStreamingMessage request", fit(request))
    section("A2A hand-off output", wrap("\n".join(out[2:])))
    first = [line for line in
             (CAP / "a2a-stream-ship.txt").read_text().splitlines()
             if line.startswith("data:")][1]
    event = json.loads(first[5:])
    upd = event["result"]["statusUpdate"]
    upd["taskId"] = shorten(upd["taskId"], 12)
    upd["contextId"] = shorten(upd["contextId"], 12)
    upd["status"]["message"]["messageId"] = shorten(
        upd["status"]["message"]["messageId"], 12)
    section("one SSE event", "data: " + fit(event))
    for name in ("a2a-reject.out", "a2a-repeat.out"):
        section(name, wrap((CAP / name).read_text().strip()))
    section("push receiver log",
            (CAP / "push-receiver.log").read_text().strip())


if __name__ == "__main__":
    main()
