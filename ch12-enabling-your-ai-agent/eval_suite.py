"""Grade the support agent's recorded sessions against what should happen.

run.sh runs host.py once per scenario on a fresh stack and saves each
session (tool calls, results, approvals, answer) as
captures/eval-<scenario>.json. Each check below is about the APIs and the
host, not the wording of the answer: which tools ran in what order, how
many refunds Harbor made, and whether Rosa was asked. With a live model,
run every scenario several times and report the pass rate per check.

    uv run python eval_suite.py
"""

import json
import sys

import common


def refunds(s):
    return {c["result"]["structured"]["id"] for c in s["calls"]
            if c["tool"] == "refund_damaged_copy"
            and not c["result"]["isError"]}


def tools(s):
    return [c["tool"] for c in s["calls"]]


R, O, D = "refund_damaged_copy", "order_overview", "report_damage"
CHECKS = {
    "happy": [("asks Rosa once, refunds once",
               lambda s: s["approvals"] == ["approved"]
               and len(refunds(s)) == 1),
              ("overview, report, refund",
               lambda s: tools(s) == [O, D, R])],
    "guess-case": [("refused without a case, follows the hint",
                    lambda s: tools(s) == [O, R, D, R]
                    and "report-damage" in s["calls"][1]["result"]["text"]),
                   ("one approval, one refund",
                    lambda s: s["approvals"] == ["approved"]
                    and len(refunds(s)) == 1)],
    "repeat": [("a repeated call returns the same refund",
                lambda s: tools(s) == [O, D, R, R]
                and len(refunds(s)) == 1)],
    "declined": [("Rosa says no; nothing refunded",
                  lambda s: s["approvals"] == ["access_denied"]
                  and not refunds(s) and "declined" in s["answer"])],
    "not-theirs": [("someone else's order looks missing; no write",
                    lambda s: tools(s) == [O] and not s["approvals"]
                    and "can't find" in s["answer"])],
}

failed = 0
for scenario, checks in CHECKS.items():
    session = json.loads((common.OUT / f"eval-{scenario}.json").read_text())
    for name, check in checks:
        ok = check(session)
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {scenario:<11} {name}")
print(f"{sum(map(len, CHECKS.values())) - failed} passed, {failed} failed")
sys.exit(1 if failed else 0)
