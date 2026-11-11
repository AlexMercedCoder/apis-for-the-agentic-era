# Chapter 11: Governing and Securing Your APIs

Harbor's contracts linted and diffed in review, Apache APISIX in front of Harbor's REST API and MCP server with one policy per kind of caller, the OWASP API Security Top 10 checked against Harbor, a prompt injection planted in a book review, and the audit trail of one agent session.

## What you need

- Docker with Compose v2 (pulls `apache/apisix:3.19.0-debian` and `tufin/oasdiff:v1.37.0` on first run).
- Node 24 (`governance/package.json` pins `@stoplight/spectral-cli` 6.17.1; `run.sh` runs `npm ci`).
- [uv](https://docs.astral.sh/uv/), which installs Python 3.13 (`.python-version`) and the pinned packages: `mcp` 2.3.0, fastapi 0.162.0, uvicorn 0.54.0, httpx 0.28.1, jsonschema 4.26.0, PyJWT 2.15.1, PyYAML 6.0.3, opentelemetry-sdk 1.45.2.
- `../ch03-rest-apis/` (its app and pinned `openapi.json`), `../ch06-streaming-and-event-apis/asyncapi.yaml`, and `../ch10-agent-protocols/harbor_mcp.py`.
- No model key: `agent_host.py` uses a stand-in that obeys any instruction it reads, on purpose.

The signing key is generated on first run into `keys/` (ignored by Git). Every token is minted at run time by a local test issuer; nothing here is a real credential.

## Run everything

```bash
./run.sh
```

It starts, all on `127.0.0.1`:

| Port | What |
|---|---|
| 8110 | `issuer.py`: test issuer, discovery document and JWKS (stands in for Chapter 8's Keycloak) |
| 8003 | Chapter 3's API with Chapter 3's logger, unchanged; the MCP server's upstream, as in Chapter 10 |
| 8111 | the same app behind `harbor_backend.py`'s gate: RS256, `at+jwt`, issuer, audience `http://127.0.0.1:9080`, scopes, ownership (one 404), body `customer_id`, JSON log with the raw path |
| 8010 | `mcp_server.py`: Chapter 10's MCP server with the issuer and resource (`http://127.0.0.1:9080/mcp`) changed |
| 9080 | Apache APISIX 3.19.0, standalone (`apisix/config.yaml`, routes in `apisix/apisix.yaml.tmpl`, rendered by `render.py`) |

Then it lints and diffs the contracts, sends the gateway and BOLA requests, sends eleven MCP calls as the assistant (the eleventh gets 429), waits out the rate-limit window, runs `agent_host.py`, sends a log-injection path, and runs `inventory.py`. It takes about 90 seconds.

## What each capture shows

- `lint-pinned.out`, `lint-proposed.out`, `lint-asyncapi.out`: Spectral with `governance/harbor-ruleset.yaml` (no version in paths, problem details on every 4xx and 5xx, `Idempotency-Key` on every `POST`, descriptions) on Chapter 3's contract (passes) and on `governance/proposed-openapi.json` from `propose_change.py` (fails with two errors, exit 1); `spectral:asyncapi` on Chapter 6's AsyncAPI 3.1 document (five warnings).
- `breaking.out`: `oasdiff breaking` finds the `price_cents` rename (2 errors, exit 1).
- `gateway-callers.out`: no token 401; the assistant's token on the REST route 403; the storefront's 200 with `X-RateLimit-*`.
- `deprecated.out`: `/v0/books/*` answers with `Deprecation` (RFC 9745), `Sunset` (RFC 8594), and `Link rel="deprecation"`.
- `bola.out`: Chapter 3's API returns Sam's `ord-5002` to anyone; through the gateway Rosa's token gets 404.
- `rate-limit.out`: ten 200s, then 429 with `X-RateLimit-Remaining: 0`. APISIX's default is 503; the consumer sets 429.
- `agent-host.out`, `audit.jsonl`, `trace.jsonl`, `gateway-access.jsonl`, `mcp-server.log`: the planted review `rev-004` (`planted-review.json`) arrives through `list_reviews`; the host flags it, holds the stand-in's `cancel_order`, and the run then shows the gateway's 403 (route `mcp-write-tools`) and the MCP server's `insufficient_scope`. Audit records, spans (GenAI semantic conventions), and gateway log lines share one trace ID.
- `backend.log`: Chapter 3's logger writes a forged `DELETE` line from a path with `%0A`; the gate's JSON line keeps the encoded path.
- `inventory.out`: FastAPI's `/docs`, `/docs/oauth2-redirect`, `/openapi.json`, `/redoc` are served but not in the contract; `/v0/books/*` is deprecated and still called.

## Not production-ready

- APISIX's `jwt-auth` checks signature and expiry, not `iss` or `aud`; the gate checks again.
- The MCP server still calls Chapter 3's API on loopback without a token; production exchanges Rosa's token (Chapter 8) and gives each service an identity.
- The host's pattern match is a tripwire for the audit log, not a defense.
- The issuer has no login and its key sits on disk.
