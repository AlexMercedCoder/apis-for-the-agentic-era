# Chapter 12: Enabling Your AI Agent with the World of APIs

The Harbor support agent, end to end. Rosa asks: "Where's my order ord-5001? The copy of Moby-Dick that came was damaged. Can you refund it?" The host answers through two curated MCP task tools (one over Chapter 5's GraphQL trusted document, one over a new REST refunds API), an A2A hand-off to the fulfillment agent, a CIBA approval on Rosa's phone carrying RFC 9396 `authorization_details`, an idempotent refund retried through Apache APISIX, and a signed webhook that confirms it. Every hop is traced and audited, and an evaluation suite grades five scenarios.

## What you need

- Docker with Compose v2 (pulls `apache/apisix:3.19.0-debian` on first run).
- [uv](https://docs.astral.sh/uv/), which installs Python 3.13 (`.python-version`) and the pinned packages: `mcp` 2.3.0, `a2a-sdk[http-server]` 1.3.0, fastapi 0.162.0, uvicorn 0.54.0, httpx 0.28.1, jsonschema 4.26.0, PyJWT 2.15.1, opentelemetry-sdk 1.45.2.
- `../ch05-graphql-apis/` (its server and `trusted-documents.json`), `../ch09-payment-standards/` (the provider stand-in and `webhooks.py`), `../ch10-agent-protocols/harbor_mcp.py` (only for the tool comparison), and `../harbor-data/`.
- No model key. `host.py` uses Chapter 1's stand-in pattern: `stand_in()` picks the call a model would pick from the same tool list a model gets (`host.py --print-tools`), and `phone.py` plays Rosa's phone.

Every key, secret, and token is a placeholder; the issuer's signing key is generated into `keys/` (ignored by Git) on first run.

## Run everything

```bash
./run.sh
```

It starts, all on `127.0.0.1`:

| Port | What |
|---|---|
| 8120 | `issuer.py`: test issuer (token exchange per RFC 8693 with nested `act`; CIBA poll mode with `authorization_details` type `harbor_refund`; `/phone/*` for the stand-in phone) |
| 8005 | Chapter 5's GraphQL server, `--trusted-only` |
| 8091 | `payment_provider.py`: Chapter 9's provider stand-in, webhooks to 8123 |
| 8123 | `refunds_api.py`: `POST /orders/{order_id}/refunds` (needs `orders:refund`, matching `authorization_details`, a damage case, and an `Idempotency-Key`; problem details with `hint`); `--slow-first` holds the first created refund's answer 1.5 s |
| 8124 | `fulfillment_agent.py`: A2A 1.0, skill `report-damage`; requires a token for its own audience, checks the order's owner, answers `GetTask` only for the task's creator |
| 8122 | `support_mcp.py`: MCP `2026-07-28` server with `order_overview` and `refund_damaged_copy` |
| 8125 | the host's receiver for the signed `refund.succeeded` webhook (while `host.py` runs) |
| 9080 | Apache APISIX 3.19.0 (`apisix/apisix.yaml.tmpl`, rendered by `render.py`) |

It takes about three minutes, mostly waiting out the gateway's per-person limit.

## What each capture shows

- `tools.json`, `tools.out`: the three tools the model sees (1,912 bytes in the Messages API format) against Chapter 10's seven generated tools (3,301 bytes).
- `capstone.out`: Rosa's session, from the question to the answer.
- `support-mcp.log`, `refunds-api.log`: the refund's first attempt times out after 1.0 s, the retry 0.5 s later with the same key gets `201` replayed; the refunds API logs the actor chain `rosa->harbor-assistant->harbor-support-mcp`.
- `phone.log`: the binding message and `authorization_details` Rosa's phone showed.
- `provider.log`, `fulfillment.log`: one refund at the provider; the damage case `dmg-5001-1001`.
- `trace.out`, `trace.json`, `trace-*.jsonl`, `gateway-access.jsonl`, `audit.jsonl`: spans from the host, MCP server, fulfillment agent, and refunds API joined by trace ID (GenAI, MCP, and A2A semantic conventions), the gateway lines with the same trace ID (a 499 for the abandoned attempt), and one audit record per decision.
- `rate-limit.out`: Sam's assistant makes eleven reads; the eleventh gets 429 and `reliable.call()` waits out `X-RateLimit-Reset`.
- `eval-<scenario>.out`, `eval-<scenario>.json`, `eval.out`: five scenarios, each on a fresh stack (happy, guess-case, repeat, declined, not-theirs), graded by `eval_suite.py`: 7 passed, 0 failed.
- `replay.out`: `replay_test.py` replays the capstone's recorded tool results offline.

## Offline checks

```bash
uv run python test_offline.py   # retries, breaker, exchange rules, refusals
uv run python replay_test.py captures/fixtures-capstone.json
```

## Not production-ready

- The model is a stand-in, and so is Rosa's phone; a real host never answers an approval for the person.
- The issuer has no sign-in and keeps its key on disk; it copies `authorization_details` into exchanged tokens, a choice RFC 8693 and RFC 9396 leave open.
- The provider's and the host's webhooks go direct, not through the gateway, and the MCP server reads the GraphQL server on loopback without a token.
- Refunds, damage cases, and idempotency keys live in memory or a local file.
- The egress proxy and the Flight SQL token check from Chapter 11 are still not built.
