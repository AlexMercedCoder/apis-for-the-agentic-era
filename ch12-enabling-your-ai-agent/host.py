"""The Harbor support agent's host: Rosa's damaged-order question, end to end.

No model runs here. `stand_in()` follows Chapter 1's stand-in pattern: a
deterministic program that picks the next tool call a model would pick,
from the same tool list a model would get (`--print-tools` prints it in
the Messages API format). Everything around it is the real host:

  * tokens: the assistant's grant (Chapter 8) is exchanged at the issuer
    for one token per API, MCP and the fulfillment agent, reads only;
  * tools: two from Harbor's support MCP server (MCP 2026-07-28 over
    Streamable HTTP, sent as raw JSON-RPC so every header is visible) and
    one the host builds from the fulfillment agent's Agent Card (A2A 1.0);
  * every call goes through the gateway and reliable.call(), which keeps
    a deadline, backs off with jitter, and waits out X-RateLimit-Reset;
  * every tool result enters the context as untrusted data, and any write
    proposed after one is held (Chapter 11) until Rosa approves it on her
    phone (CIBA, with RFC 9396 authorization_details naming the refund);
  * Harbor's signed refund.succeeded webhook wakes the agent (Chapter 6);
  * spans follow the OpenTelemetry GenAI and MCP conventions, and the
    audit log records each decision with both names (Chapter 11).

    uv run python host.py [--scenario happy] [--print-tools]
"""

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import re
import uuid

import httpx
import uvicorn
from opentelemetry import propagate
from opentelemetry.trace import SpanKind
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

import common
import issuer
import reliable
import webhooks_ch09 as webhooks

GW = issuer.GATEWAY
CARD_URL = GW + "/a2a/fulfillment/.well-known/agent-card.json"
SECRET = "not-a-real-secret-assistant"             # placeholder
VERSION = "2026-07-28"
WRITES = {"refund_damaged_copy"}
OUT = common.OUT
tracer = common.tracer("support-host")


class Host:
    def __init__(self, person="rosa", scenario="happy", say=print):
        self.person, self.scenario, self.say = person, scenario, say
        self.session = f"s-{uuid.uuid4().hex[:8]}"
        self.http = httpx.AsyncClient(timeout=10)
        self.tokens, self.approvals, self.calls = {}, {}, []
        self.decisions = []
        self.confirmed: dict[str, asyncio.Event] = {}
        self.step = 0

    def line(self, text):
        self.step += 1
        self.say(f"{self.step}. {text}")

    def audit(self, **record):
        record = {"session": self.session, "user": self.person,
                  "agent": "harbor-assistant",
                  "trace_id": common.current_trace_id(), **record}
        with (OUT / "audit.jsonl").open("a") as f:
            f.write(json.dumps(record) + "\n")

    # -- tokens ------------------------------------------------------------
    async def token_for(self, audience, scope="orders:read"):
        if audience not in self.tokens:
            resp = await self.http.post(f"{issuer.ISSUER}/token", data={
                "grant_type": issuer.EXCHANGE,
                "client_id": "harbor-assistant", "client_secret": SECRET,
                "subject_token": issuer.grant(self.person),
                "subject_token_type":
                    "urn:ietf:params:oauth:token-type:access_token",
                "audience": audience, "scope": scope})
            resp.raise_for_status()
            self.tokens[audience] = resp.json()["access_token"]
        return self.tokens[audience]

    # -- MCP, as raw JSON-RPC over Streamable HTTP ---------------------------
    async def mcp(self, method, params, name=None, token=None):
        token = token or await self.token_for(issuer.MCP_AUD)
        headers = {"Authorization": f"Bearer {token}",
                   "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": VERSION, "Mcp-Method": method}
        if name:
            headers["Mcp-Name"] = name
        meta = {"io.modelcontextprotocol/protocolVersion": VERSION,
                "io.modelcontextprotocol/clientCapabilities": {},
                "io.modelcontextprotocol/clientInfo": {
                    "name": "harbor-support-host", "version": "1.0.0"}}
        propagate.inject(headers)
        propagate.inject(meta)                     # SEP-414: _meta too
        body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method,
                "params": {**params, "_meta": meta}}

        async def send(timeout):
            return await self.http.post(f"{GW}/mcp", json=body,
                                        headers=headers, timeout=timeout)
        return await reliable.call(send, deadline=90.0, per_try=10.0,
                                   log=lambda m: self.say(f"   {m}"))

    async def tools(self):
        resp = await self.mcp("tools/list", {})
        listed = resp.json()["result"]["tools"]
        card = (await self.http.get(CARD_URL)).json()
        skill = next(s for s in card["skills"] if s["id"] == "report-damage")
        self.a2a_url = card["supportedInterfaces"][0]["url"]
        self.agent_name = card["name"]
        a2a_tool = {
            "name": "report_damage",
            "description": f"{skill['description']} (Asks {card['name']}"
                           f" over A2A.)",
            "inputSchema": {"type": "object", "properties": {
                "order_id": {"type": "string", "pattern": "^ord-[0-9]{4}$"},
                "book_id": {"type": "string", "pattern": "^bk-[0-9]{4}$"},
                "description": {"type": "string", "maxLength": 200}},
                "required": ["order_id", "book_id", "description"],
                "additionalProperties": False}}
        return listed + [a2a_tool]

    # -- A2A -----------------------------------------------------------------
    async def report_damage(self, args):
        token = await self.token_for(issuer.FULFILLMENT_AUD)
        headers = {"Authorization": f"Bearer {token}", "A2A-Version": "1.0"}
        with tracer.start_as_current_span(
                "SendMessage", kind=SpanKind.CLIENT, attributes={
                    "a2a.method.name": "SendMessage",
                    "a2a.protocol.version": "1.0",
                    "gen_ai.agent.name": self.agent_name}) as span:
            propagate.inject(headers)
            body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage",
                    "params": {"message": {
                        "messageId": str(uuid.uuid4()), "role": "ROLE_USER",
                        "parts": [{"data": args}]}}}

            async def send(timeout):
                return await self.http.post(self.a2a_url, json=body,
                                            headers=headers,
                                            timeout=timeout)
            resp = await reliable.call(send, deadline=30.0, per_try=10.0)
            task = resp.json()["result"]["task"]
            state = task["status"]["state"]
            span.set_attribute("a2a.task.id", task["id"])
            span.set_attribute("a2a.task.state", state)
        note = task["status"]["message"]["parts"][0]["text"]
        if state != "TASK_STATE_COMPLETED":
            return {"isError": True, "text": note}
        data = task["artifacts"][0]["parts"][0]["data"]
        return {"isError": False, "structured": data, "text": note,
                "task": task["id"]}

    # -- approval: CIBA on Rosa's phone ---------------------------------------
    async def approval(self, args, context):
        held = self.approvals.get((args["order_id"], args["book_id"]))
        if held:
            return held
        overview = next((i["structured"] for i in reversed(context)
                         if i.get("source") == "order_overview"
                         and not i["isError"] and i["structured"]
                         ["order_id"] == args["order_id"]), None)
        line = overview and next((ln for ln in overview["lines"]
                                  if ln["book_id"] == args["book_id"]),
                                 None)
        if line is None:
            self.line("host: no lookup of that order line; not asking")
            return None
        detail = {"type": "harbor_refund", "actions": ["refund"],
                  "order_id": args["order_id"], "book_id": args["book_id"],
                  "max_amount": line["price_cents"], "currency": "USD",
                  "request_id": str(uuid.uuid4())}
        message = f"Harbor {detail['request_id'][:4].upper()}"
        with tracer.start_as_current_span("approval harbor_refund") as span:
            resp = await self.http.post(f"{issuer.ISSUER}/bc-authorize",
                                        data={
                "client_id": "harbor-assistant", "client_secret": SECRET,
                "scope": "openid orders:refund", "login_hint": self.person,
                "binding_message": message,
                "authorization_details": json.dumps([detail])})
            req = resp.json()
            self.line(f"host: held for {self.person}'s approval; CIBA "
                      f"request sent to her phone, code {message}")
            while True:
                await asyncio.sleep(req["interval"])
                tok = await self.http.post(f"{issuer.ISSUER}/token", data={
                    "grant_type": issuer.CIBA, "client_id":
                        "harbor-assistant", "client_secret": SECRET,
                    "auth_req_id": req["auth_req_id"]})
                answer = tok.json()
                if answer.get("error") != "authorization_pending":
                    break
            decision = "approved" if tok.status_code == 200 else \
                answer["error"]
            span.set_attribute("harbor.approval.decision", decision)
        self.decisions.append(decision)
        self.audit(event="approval", tool="refund_damaged_copy",
                   args=digest("refund_damaged_copy", args),
                   decision=decision, max_amount=detail["max_amount"])
        if tok.status_code != 200:
            self.line(f"{self.person}'s phone: {decision}")
            self.declined = True
            return None
        self.line(f"{self.person}'s phone: approved; token scope "
                  f"\"{answer['scope']}\", authorization_details "
                  f"{detail['order_id']} {detail['book_id']} max "
                  f"{detail['max_amount']}")
        self.approvals[(args["order_id"], args["book_id"])] = \
            answer["access_token"]
        return answer["access_token"]

    # -- one tool call ------------------------------------------------------
    async def call(self, name, args, context):
        call_id = f"call-{uuid.uuid4().hex[:8]}"
        token = None
        if name in WRITES and must_hold(name, context):
            self.declined = False
            token = await self.approval(args, context)
            if token is None:
                text = ("The customer declined on her phone; nothing was "
                        "refunded." if self.declined else "Look up the "
                        "order first; nothing was refunded.")
                result = {"isError": True, "text": text}
                return self.record(name, args, result, context)
        if name == "report_damage":
            result = await self.report_damage(args)
        else:
            with tracer.start_as_current_span(
                    f"tools/call {name}", kind=SpanKind.CLIENT, attributes={
                        "mcp.method.name": "tools/call",
                        "gen_ai.tool.name": name,
                        "gen_ai.tool.call.id": call_id,
                        "gen_ai.operation.name": "execute_tool",
                        "mcp.protocol.version": VERSION}):
                resp = await self.mcp("tools/call", {
                    "name": name, "arguments": args}, name=name, token=token)
                if resp.status_code != 200:
                    result = {"isError": True,
                              "text": f"HTTP {resp.status_code} "
                                      f"{resp.headers.get('www-authenticate',
                                                          resp.text)}"}
                else:
                    r = resp.json()["result"]
                    result = {"isError": r.get("isError", False),
                              "structured": r.get("structuredContent"),
                              "text": r["content"][0]["text"]}
        self.audit(event="tool_call", tool=name, args=digest(name, args),
                   decision="allowed", error=result["isError"])
        return self.record(name, args, result, context)

    def record(self, name, args, result, context):
        context.append({"role": "tool", "source": name,
                        "trust": "untrusted", **result})
        self.calls.append({"tool": name, "args": args, "result": result})
        return result

    # -- the session ---------------------------------------------------------
    async def run(self, question):
        with tracer.start_as_current_span(
                "invoke_agent harbor-support", attributes={
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.agent.name": "harbor-support",
                    "gen_ai.conversation.id": self.session}):
            tools = await self.tools()
            self.say(f"session {self.session}: {self.person} via "
                     f"harbor-assistant, {len(tools)} tools")
            context = [{"role": "user", "text": question}]
            self.context = context
            self.line(f"{self.person}: \"{question}\"")
            for _ in range(8):
                step = stand_in(context, self.scenario)
                if "answer" in step:
                    break
                name, args = step["call"]
                if not schema_ok(tools, name, args):
                    self.line(f"host refused {name}: arguments don't match")
                    continue
                result = await self.call(name, args, context)
                self.line(f"{name} {json.dumps(args)}\n   " +
                          summary(name, result))
                if name == "refund_damaged_copy" and not result["isError"]:
                    await self.wait_for_webhook(result["structured"]["id"])
            self.line(f"answer: {step['answer']}")
            self.answer = step["answer"]
            return step["answer"]

    async def wait_for_webhook(self, refund_id):
        event = self.confirmed.setdefault(refund_id, asyncio.Event())
        with tracer.start_as_current_span("await refund.succeeded"):
            try:
                await asyncio.wait_for(event.wait(), 10)
                self.line(f"webhook refund.succeeded {refund_id}: "
                          f"agent woken")
                self.context.append({"role": "event",
                                     "source": "refund.succeeded",
                                     "refund_id": refund_id})
            except TimeoutError:
                self.line(f"no webhook for {refund_id} yet")

    async def hook(self, request):
        body = await request.body()
        try:
            webhooks.verify(common.HOST_SECRET, request.headers, body)
        except webhooks.InvalidWebhook:
            return Response(status_code=401)
        refund = json.loads(body)["data"]
        self.confirmed.setdefault(refund["id"], asyncio.Event()).set()
        return Response(status_code=204)


# -- the stand-in for a model ---------------------------------------------------

def last(context, tool, ok=True):
    for item in reversed(context):
        if item.get("source") == tool and (not ok or not item["isError"]):
            return item.get("structured")
    return None


def stand_in(context, scenario="happy"):
    """Pick the next call by fixed rules, as a model would. Not a model.

    It reads the question, the tool results, and each error's hint, and
    it repeats nothing it wasn't told about; `scenario` scripts the one
    mistake each evaluation scenario needs.
    """
    question = context[0]["text"]
    order_id = re.search(r"ord-[0-9]{4}", question).group(0)
    seen = [(n, i) for n, i in enumerate(context) if i.get("source")]
    if not seen:
        return {"call": ("order_overview", {"order_id": order_id})}
    overview = last(context, "order_overview")
    if overview is None:
        return {"answer": f"I can't find {order_id} on your account."}
    line = next(ln for ln in overview["lines"]
                if ln["title"].lower() in question.lower())
    said = next(s.strip() for s in re.split(r"(?<=[.?!])\s+", question)
                if line["title"].lower() in s.lower())
    refund = {"order_id": order_id, "book_id": line["book_id"]}
    refunds = [(n, i) for n, i in seen
               if i["source"] == "refund_damaged_copy"]
    cases = [(n, i) for n, i in seen
             if i["source"] == "report_damage" and not i["isError"]]
    if scenario == "guess-case" and not refunds:
        return {"call": ("refund_damaged_copy",
                         {**refund, "damage_case": "dmg-00000001"})}
    hint = ((refunds[-1][1].get("structured") or {}).get("problem", {})
            .get("hint", "")) if refunds else ""
    if not cases:
        failed = [i for _, i in seen if i["source"] == "report_damage"]
        if failed:
            return {"answer": failed[-1]["text"]}
        if refunds and "report-damage" not in hint:
            return {"answer": refunds[-1][1]["text"]}
        return {"call": ("report_damage", {**refund, "description": said})}
    case_at, case = cases[-1][0], cases[-1][1]["structured"]
    after = [i for n, i in refunds if n > case_at]
    done = [i for i in after if not i["isError"]]
    if done and scenario == "repeat" and len(after) == 1:
        return {"call": ("refund_damaged_copy",
                         {**refund, "damage_case": case["case_id"]})}
    if done and done[-1]["structured"].get("status") == "unknown":
        return {"answer": f"{order_id} was {overview['status']}. I've "
                          f"sent the refund for the damaged "
                          f"{line['title']}, and Harbor is confirming it "
                          f"with the payment provider."}
    if done:
        amount = done[-1]["structured"]["amount_cents"] / 100
        confirmed = any(i.get("source") == "refund.succeeded"
                        for i in context)
        status = ("Harbor has confirmed it" if confirmed else
                  "it's pending until Harbor confirms it")
        return {"answer": f"{order_id} was {overview['status']}. "
                          f"I've refunded {amount:.2f} USD for the "
                          f"damaged {line['title']}, and {status}."}
    if after and "declined" in after[-1]["text"]:
        return {"answer": "You declined the refund on your phone, so "
                          "nothing was changed."}
    if after:
        return {"answer": after[-1]["text"]}
    return {"call": ("refund_damaged_copy",
                     {**refund, "damage_case": case["case_id"]})}


# -- host checks -------------------------------------------------------------

def must_hold(name, context):
    """Chapter 11: hold any write once any tool result is in the context."""
    return name in WRITES and any(i.get("role") == "tool" for i in context)


def schema_ok(tools, name, args):
    tool = next((t for t in tools if t["name"] == name), None)
    if tool is None:
        return False
    return common.Strict(tool["inputSchema"]).is_valid(args)


def digest(name, args):
    text = json.dumps([name, args], sort_keys=True).encode()
    key = os.environ["HARBOR_AUDIT_KEY"].encode()
    return "hmac:" + hmac.new(key, text, hashlib.sha256).hexdigest()[:16]


def summary(name, result):
    if result["isError"]:
        return "error: " + result["text"]
    s = result.get("structured") or {}
    if name == "order_overview":
        books = ", ".join(f"{ln['book_id']} {ln['title']} "
                          f"{ln['price_cents']}" for ln in s["lines"])
        return f"{s['status']}: {books}"
    if name == "report_damage":
        return (f"A2A task {result['task'][:8]} completed: "
                f"{s['case_id']}, return_required={s['return_required']}")
    return f"{s['id']} {s['amount_cents']} cents, {s['status']}"


def messages_api_tools(tools):
    return [{"name": t["name"], "description": t["description"],
             "input_schema": t["inputSchema"]} for t in tools]


async def main(args):
    if len(os.environ.get("HARBOR_AUDIT_KEY", "")) < 32:
        raise SystemExit("set HARBOR_AUDIT_KEY to a secret of 32+ characters")
    host = Host(args.person, args.scenario)
    if args.print_tools:
        print(json.dumps(messages_api_tools(await host.tools()), indent=2))
        return
    hooks = uvicorn.Server(uvicorn.Config(
        Starlette(routes=[Route("/hooks/harbor", host.hook,
                                methods=["POST"])]),
        host="127.0.0.1", port=8125, log_level="warning"))
    serving = asyncio.create_task(hooks.serve())
    while not hooks.started:
        await asyncio.sleep(0.05)
    await host.run(args.question)
    hooks.should_exit = True
    await serving
    if args.fixtures:
        (OUT / args.fixtures).write_text(json.dumps({
            "scenario": args.scenario, "question": args.question,
            "calls": host.calls, "approvals": host.decisions,
            "confirmed": [i["refund_id"] for i in host.context
                          if i.get("source") == "refund.succeeded"],
            "answer": host.answer}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--person", default="rosa")
    parser.add_argument("--scenario", default="happy")
    parser.add_argument("--question", default=(
        "Where's my order ord-5001? The copy of Moby-Dick that came has "
        "a torn cover. Can you refund it?"))
    parser.add_argument("--print-tools", action="store_true")
    parser.add_argument("--fixtures")
    asyncio.run(main(parser.parse_args()))
