"""The third caller: a model that picks a tool at runtime.

The script builds a request in the Anthropic Messages API format (tool
definitions with name, description, and input_schema), gets back a response
whose content holds a tool_use block, runs that tool call against the Harbor
REST API, and prints the tool_result block for the model. With --live it
also sends that block back in a second request and prints the model's
answer. The stand-in stops after the tool_result, because anything it
printed as an answer would be invented.

Two modes:

  --live     send the request to the real Messages API. Needs
             ANTHROPIC_API_KEY in the environment. Costs tokens.
  (default)  use LocalStandIn, a deterministic stand-in that is not a
             model. It reads the prompt with a regular expression and emits
             a response in the documented tool_use shape. It exists so the
             example runs offline and without an account. Its output is
             labeled "model": "local-standin" so nobody mistakes it for a
             model's output.
"""

import argparse
import json
import os
import re
import sys

import httpx

HARBOR = "http://127.0.0.1:8000"
MESSAGES_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2026-10-21"
MODEL = os.environ.get("HARBOR_MODEL", "claude-opus-5-5")

GET_BOOK_TOOL = {
    "name": "get_book",
    "description": (
        "Look up one book in the Harbor Books catalog by its ID. "
        "Returns title, author, format, price in cents, and copies "
        "in stock. Read-only."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "book_id": {
                "type": "string",
                "description": "Harbor book ID, such as bk-1001",
                "pattern": "^bk-[0-9]{4}$",
            }
        },
        "required": ["book_id"],
    },
}


def build_request(prompt):
    return {
        "model": MODEL,
        "max_tokens": 1024,
        "tools": [GET_BOOK_TOOL],
        "messages": [{"role": "user", "content": prompt}],
    }


class LocalStandIn:
    """Deterministic stand-in for a model. Not a model."""

    BOOK_ID = re.compile(r"\bbk-[0-9]{4}\b")

    def create(self, request):
        prompt = request["messages"][-1]["content"]
        match = self.BOOK_ID.search(prompt)
        if not match:
            return {
                "id": "msg_standin_0001",
                "type": "message",
                "role": "assistant",
                "model": "local-standin",
                "content": [{"type": "text",
                             "text": "local-standin: no book ID found"}],
                "stop_reason": "end_turn",
            }
        return {
            "id": "msg_standin_0001",
            "type": "message",
            "role": "assistant",
            "model": "local-standin",
            "content": [{
                "type": "tool_use",
                "id": "toolu_standin_0001",
                "name": "get_book",
                "input": {"book_id": match.group(0)},
            }],
            "stop_reason": "tool_use",
        }


class LiveModel:
    def create(self, request):
        response = httpx.post(
            MESSAGES_URL,
            headers={
                "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
            json=request,
            timeout=60,
        )
        response.raise_for_status()
        return response.json()


def run_tool(name, tool_input):
    if name != "get_book":
        return {"error": f"unknown tool {name}"}, True
    response = httpx.get(
        f"{HARBOR}/books/{tool_input['book_id']}",
        headers={"User-Agent": "harbor-agent/0.1"},
    )
    return response.json(), response.is_error


def show(label, payload):
    print(f"--- {label}")
    print(json.dumps(payload, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt", nargs="?",
                        default="Is bk-1001 in stock, and what does it cost?")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()

    if args.live and "ANTHROPIC_API_KEY" not in os.environ:
        sys.exit("--live needs ANTHROPIC_API_KEY")
    model = LiveModel() if args.live else LocalStandIn()

    request = build_request(args.prompt)
    show(f"request to POST {MESSAGES_URL}", request)
    response = model.create(request)
    show("response", response)

    results = []
    for block in response["content"]:
        if block["type"] != "tool_use":
            continue
        result, is_error = run_tool(block["name"], block["input"])
        results.append({
            "type": "tool_result",
            "tool_use_id": block["id"],
            "content": json.dumps(result),
            "is_error": is_error,
        })
    for result in results:
        show("tool_result to send back to the model", result)

    if args.live and results:
        request["messages"] += [
            {"role": "assistant", "content": response["content"]},
            {"role": "user", "content": results},
        ]
        followup = model.create(request)
        show("model's answer", followup)


if __name__ == "__main__":
    main()
