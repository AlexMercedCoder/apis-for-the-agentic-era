# Chapter 12: Enabling Your AI Agent with the World of APIs

The Harbor support agent, end to end. Rosa asks: "Where's my order ord-5001? The copy of Moby-Dick that came was damaged. Can you refund it?" The host answers through two curated MCP task tools (one over Chapter 5's GraphQL trusted document, one over a new REST refunds API), an A2A hand-off to the fulfillment agent, a CIBA approval on Rosa's phone carrying RFC 9396 `authorization_details`, an idempotent refund retried through Apache APISIX, and a signed webhook that confirms it. Every hop is traced and audited, and an evaluation suite grades five scenarios.

## What you need

- Docker with Compose v2 (pulls `apache/apisix:3.19.0-debian` on first run).
- [uv](https://docs.astral.sh/uv/), which installs Python 3.13 (`.python-version`) and the pinned packages: `mcp` 2.3.0, `a2a-sdk[http-server]` 1.3.0, fastapi 0.162.0, uvicorn 0.54.0, httpx 0.28.1, jsonschema 4.26.0, PyJWT 2.15.1, opentelemetry-sdk 1.45.2.
- `../ch05-graphql-apis/` (its server and `trusted-documents.json`), `../ch09-payment-standards/` (the provider stand-in and `webhooks.py`), `../ch10-agent-protocols/harbor_mcp.py` (only for the tool comparison), and `../harbor-data/`.
- No model key. `host.py` uses Chapter 1's stand-in pattern: `stand_in()` picks the call a model would pick from the same tool list a model gets (`host.py --print-tools`), and `phone.py` plays Rosa's phone.

Every key, secret, and token is a placeholder (each client has its own placeholder secret, and the host holds only the assistant's); the issuer's signing key is generated into `keys/` (ignored by Git) on first run.

## Run everything

```bash
./run.sh
```

It starts, all on `127.0.0.1`:

| Port | What |
|---|---|
| 8120 | `issuer.py`: test issuer (token exchange per RFC 8693 with nested `act`; CIBA poll mode, `openid` required, with `authorization_details` type `harbor_refund` checked field by field (known currency, integer amount, no free text), rendered by the issuer for the phone; `/phone/*` for the stand-in phone, behind a device secret `run.sh` gives only to the issuer and `phone.py`) |
| 8005 | Chapter 5's GraphQL server, `--trusted-only` |
| 8091 | `payment_provider.py`: Chapter 9's provider stand-in, webhooks to 8123 |
| 8123 | `refunds_api.py`: `POST /orders/{order_id}/refunds` (needs `orders:refund`, the support MCP server as current actor, matching `authorization_details`, an `Idempotency-Key` equal to the approval's `request_id`, the order's currency, a recorded payment, and a damage case; one refund per order line; state in `state/refunds.json`; provider key per order line, so a lost record can't refund twice while the provider keeps keys (24 hours in the stand-in); a provider refusal or an unreachable provider frees the line, while a lost reply (timeout, reset, 5xx) answers 202 with status `unknown`, keeps the line reserved, and reconciles with the provider under the same key, freeing the line only if the provider refuses that key (an unreachable provider keeps it reserved); the host's agent then says the refund is being confirmed; problem details with `hint`); `--slow-first` holds the first created refund's answer 1.5 s |
| 8124 | `fulfillment_agent.py`: A2A 1.0, skill `report-damage` (random case IDs; the description stored as untrusted text); requires a token for its own audience, checks the order's owner, answers `GetTask` only for the task's creator |
| 8122 | `support_mcp.py`: MCP `2026-07-28` server with `order_overview` and `refund_damaged_copy` |
| 8125 | the host's receiver for the signed `refund.succeeded` webhook (while `host.py` runs) |
| 9080 | Apache APISIX 3.19.0 (`apisix/apisix.yaml.tmpl`, rendered by `render.py`) |

It takes about three minutes, mostly waiting out the gateway's per-person limit.

## What each capture shows

- `tools.json`, `tools.out`: the three tools the model sees (1,906 bytes in the Messages API format) against Chapter 10's seven generated tools (3,301 bytes).
- `capstone.out`: Rosa's session, from the question to the answer.
- `support-mcp.log`, `refunds-api.log`: the refund's first attempt times out after 1.0 s, the retry 0.5 s later with the same key gets `201` replayed; the refunds API logs the actor chain `rosa->harbor-assistant->harbor-support-mcp`.
- `phone.log`: what Rosa's phone showed: the issuer's rendering of the `authorization_details`, the host's short binding code, and the details.
- `forge-approval.out`: the host's own attempts to read Rosa's phone queue and approve its request get 401 `invalid_device`, no token is issued, and a CIBA request without `openid` gets 400.
- `provider.log`, `fulfillment.log`: one refund at the provider; a random damage case ID.
- `trace.out`, `trace.json`, `trace-*.jsonl`, `gateway-access.jsonl`, `audit.jsonl`: spans from the host, MCP server, fulfillment agent, and refunds API joined by trace ID (GenAI, MCP, and A2A semantic conventions), the gateway lines with the same trace ID (a 499 for the abandoned attempt), and one audit record per decision.
- `rate-limit.out`: Sam's assistant makes eleven reads; the eleventh gets 429 and `reliable.call()` waits out `X-RateLimit-Reset`.
- `eval-<scenario>.out`, `eval-<scenario>.json`, `eval.out`: five scenarios, each on a fresh stack (happy, guess-case, repeat, declined, not-theirs), graded by `eval_suite.py`: 7 passed, 0 failed.
- `replay.out`: `replay_test.py` replays the capstone's recorded tool results offline.

## Offline checks

```bash
uv run python test_offline.py   # retries, breaker, phone, exchange, refunds API
uv run python replay_test.py captures/fixtures-capstone.json
```

## Not production-ready

- The model is a stand-in (fixed rules that read results and hints), and so is Rosa's phone; its device secret stands in for a real device that signs her in.
- The issuer has no sign-in, keeps its key on disk, and omits the ID Token from CIBA token responses; it copies `authorization_details` into exchanged tokens, a choice RFC 8693 and RFC 9396 leave open.
- Refund state is a JSON file, written after each change; production keeps it in the same database transaction as the refund.
- The provider's and the host's webhooks go direct, not through the gateway, and the MCP server reads the GraphQL server on loopback without a token.
- The egress proxy and the Flight SQL token check from Chapter 11 are still not built.
