"""Replay a recorded session offline: same tool calls, same holds.

host.py --fixtures saves every tool call and result of a live session.
This test feeds those results back to the stand-in planner, with no
server running, and checks that it asks for the same calls in the same
order and that the host's rule holds the refund for Rosa's approval. Run
it in CI after any change to the planner, the prompts, or the tool list;
with a live model, compare tool names and arguments the same way.

    uv run python replay_test.py [captures/fixtures-capstone.json]
"""

import json
import sys

import host

path = sys.argv[1] if len(sys.argv) > 1 else "captures/fixtures-capstone.json"
session = json.load(open(path))
context = [{"role": "user", "text": session["question"]}]
for n, recorded in enumerate(session["calls"], 1):
    step = host.stand_in(context, session["scenario"])
    assert "call" in step, f"step {n}: answered early: {step}"
    name, args = step["call"]
    assert (name, args) == (recorded["tool"], recorded["args"]), \
        f"step {n}: asked for {name} {args}, recorded {recorded['tool']}"
    held = host.must_hold(name, context)
    assert held == (name in host.WRITES), f"step {n}: hold rule changed"
    print(f"{n}. {name} {'held for approval' if held else 'ran'}: ok")
    context.append({"role": "tool", "source": name, **recorded["result"]})
    rid = (recorded["result"].get("structured") or {}).get("id")
    if rid in session.get("confirmed", []):
        context.append({"role": "event", "source": "refund.succeeded",
                        "refund_id": rid})
final = host.stand_in(context, session["scenario"])
assert final.get("answer") == session["answer"], "the answer changed"
print(f"answer matches; {len(session['calls'])} calls replayed")
