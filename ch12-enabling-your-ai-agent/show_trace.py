"""Print the last support session's trace as a tree, joined across services.

Reads the span files each service wrote (captures/trace-*.jsonl), picks the
trace of the most recent invoke_agent span, and prints each span under its
parent with its service, duration, and the attributes that matter. The
gateway's access log lines with the same trace ID follow, so the trace and
the gateway's own record can be read side by side.

    uv run python show_trace.py [--json]
"""

import json
import sys

import common

KEEP = ("gen_ai.tool.name", "a2a.task.state", "harbor.approval.decision",
        "enduser.id", "error.type")
LABEL = {"a2a.task.state": "state", "harbor.approval.decision": "decision",
         "enduser.id": "user", "error.type": "error"}
spans = [json.loads(line) for path in sorted(common.OUT.glob("trace-*.jsonl"))
         for line in path.read_text().splitlines()]
root = max((s for s in spans if s["name"].startswith("invoke_agent")),
           key=lambda s: s["start"])
mine = [s for s in spans if s["trace_id"] == root["trace_id"]]
children = {}
for s in mine:
    children.setdefault(s["parent"], []).append(s)
ids = {s["span_id"] for s in mine}
t0 = root["start"]
rows = []


def walk(span, depth):
    attrs = {k: v for k, v in span["attributes"].items() if k in KEEP
             and k != "gen_ai.tool.name"}
    rows.append({"depth": depth, "service": span["service"],
                 "name": span["name"],
                 "start_ms": round((span["start"] - t0) / 1e6),
                 "ms": round((span["end"] - span["start"]) / 1e6),
                 "attrs": attrs})
    for child in sorted(children.get(span["span_id"], []),
                        key=lambda s: s["start"]):
        walk(child, depth + 1)


walk(root, 0)
orphans = [s for s in mine if s["parent"] not in ids and s is not root]
for s in sorted(orphans, key=lambda s: s["start"]):
    walk(s, 1)
if "--json" in sys.argv:
    print(json.dumps(rows, indent=1))
    sys.exit()
print(f"trace {root['trace_id']}")
print(f"{'start':>5} {'ms':>5}  {'service':<12} span")
for r in rows:
    extra = " ".join(f"{LABEL.get(k, k)}={v}" for k, v in r["attrs"].items())
    label = " " * r["depth"] + r["name"]
    print(f"{r['start_ms']:>5} {r['ms']:>5}  {r['service']:<12} "
          f"{label} {extra}".rstrip())
log = common.OUT / "gateway" / "access.jsonl"
if log.exists():
    print("gateway lines with this trace ID:")
    for line in log.read_text().splitlines():
        rec = json.loads(line)
        if root["trace_id"] in rec.get("traceparent", ""):
            print(f"  {rec['consumer']:<11} sub={rec['sub']:<5} "
                  f"{rec['method']} {rec['uri']:<26} {rec['status']}")
