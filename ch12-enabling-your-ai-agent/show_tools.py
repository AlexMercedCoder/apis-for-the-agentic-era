"""Compare Chapter 10's generated tools with this chapter's curated ones.

Chapter 10's server turned every operation in Chapter 3's contract into a
tool. The support agent's host offers three task tools instead (two from
the support MCP server, one from the fulfillment agent's Agent Card,
saved by host.py --print-tools in captures/tools.json). This prints both
sets with what each tool does and the size of the definitions a model
reads on every turn, in the Messages API format.

    uv run python show_tools.py
"""

import json
import sys
from pathlib import Path

import common

sys.path.insert(0, str(common.HERE.parent / "ch10-agent-protocols"))
import harbor_mcp  # noqa: E402

generated = [{"name": t.name, "description": t.description,
              "input_schema": t.input_schema,
              "does": "reads" if t.annotations.read_only_hint
              else "writes"}
             for t in harbor_mcp.TOOLS]
curated = json.loads((common.OUT / "tools.json").read_text())
DOES = {"order_overview": "reads", "report_damage": "records a case",
        "refund_damaged_copy": "writes, after approval"}
for t in curated:
    t["does"] = DOES[t["name"]]


def size(tools):
    return len(json.dumps([{k: v for k, v in t.items() if k != "does"}
                           for t in tools]).encode())


for label, tools in (("Chapter 10, one tool per operation", generated),
                     ("Chapter 12, one tool per task", curated)):
    print(f"{label}: {len(tools)} tools, {size(tools)} bytes")
    for t in tools:
        print(f"  {t['name']:<20} {t['does']}")
