# Chapter 10: Agent Protocols with MCP, A2A, and Their Neighbors

Harbor's MCP server, generated from Chapter 3's pinned OpenAPI document and protected by Chapter 8's Keycloak, called by an MCP client built on the official SDK; and an A2A hand-off from Rosa's shopping agent to Harbor's fulfillment agent, with streaming and push notifications.

## What you need

- Docker with Compose v2 (pulls `quay.io/keycloak/keycloak:26.8.2` on first run).
- [uv](https://docs.astral.sh/uv/), which installs Python 3.13 (`.python-version`) and the pinned packages: `mcp` 2.3.0, `a2a-sdk[http-server]` 1.3.0, httpx 0.28.1, PyJWT 2.15.1, uvicorn 0.54.0.
- `../ch03-rest-apis/` (its server and its pinned `openapi.json`).
- No model key: programs make the calls a model would choose, and a callback answers Rosa's confirmation with yes.

Every password, client secret, push token, and state key here is a placeholder for this local stack.

## Run everything

```bash
./run.sh
```

It starts, all on `127.0.0.1`:

| Port | What |
|---|---|
| 8080 | Keycloak 26.8.2, realm `harbor`, `--features=resource-indicators` (RFC 8707, experimental) |
| 8003 | Chapter 3's REST API, fresh seed data |
| 8010 | `harbor_mcp.py`: MCP `2026-07-28` over Streamable HTTP at `/mcp`, Protected Resource Metadata at `/.well-known/oauth-protected-resource/mcp` |
| 8011 | `fulfillment_agent.py`: A2A 1.0 JSON-RPC binding at `/`, Agent Card at `/.well-known/agent-card.json` |
| 8012 | `push_receiver.py`: the shopping agent's push receiver at `/a2a/push` |

Then it runs `mcp_client.py`, a request with a bad token, `shopping_agent.py` three times (ships `ord-5003`, rejects shipped `ord-5002`, rejects a repeat for `ord-5003`), and two forged pushes (an unknown token gets 401, another task's token 403), and writes `captures/`. It takes about two minutes, mostly Keycloak's start. `KEEP=1 ./run.sh` leaves Keycloak running.

## Files

- `setup_realm.py`: realm `harbor`; scopes `orders:read`, `orders:write`; client `harbor-mcp` (the MCP server, `resource_url` `http://127.0.0.1:8010/mcp`); public client `harbor-mcp-client` (code + PKCE S256, consent, audience mapper to `harbor-mcp`, `customer_id` claim); users `rosa` (`cus-001`) and `sam` (`cus-002`), password `not-a-real-password`.
- `login.py`: scripted sign-in with PKCE, `state`, `iss` check (RFC 9207), and `resource` (RFC 8707) on both requests.
- `harbor_mcp.py`: Protected Resource Metadata lists only `orders:read`; a write tool called with a read-only token gets HTTP 403 `insufficient_scope` with `scope="orders:read orders:write"` (step-up; the tool name comes from `Mcp-Name`, which the SDK rejects with 400 and code -32020 when it disagrees with the body). Validates every tool's arguments against its published input schema (jsonschema 4.26.0) and percent-encodes path segments before building a URL, so `get_book` with `../orders/ord-5002` is refused. Builds seven tools from `../ch03-rest-apis/openapi.json` (operationId as name; `customer_id` filled from the token; `Idempotency-Key` filled by the server; `GET` is `readOnlyHint`, `PUT` destructive and idempotent). Validates Keycloak tokens (RS256, `at+jwt`, issuer, audience `/mcp`), never forwards them, answers others' orders as missing, and confirms every write with a multi round-trip elicitation whose `requestState` is HMAC-sealed and bound to the person, the arguments, five minutes, and one idempotency key. One resource template (`harbor://books/{book_id}`) and one prompt (`where_is_my_order`).
- `mcp_client.py`: the official SDK client; discovery from the 401, a check that Keycloak's metadata names the issuer it was fetched from (RFC 8414), sign-in for `orders:read`, then `server/discover`, `tools/list`, `tools/call`, `resources/read`, `prompts/get`; a path-traversal `get_book` that the schema check refuses; a write that gets the 403, a step-up sign-in for `orders:write`, and a confirmed `place_order` (one *Treasure Island*). The callback that answers Rosa's confirmation is a demo stand-in; a real host shows the form to the person and never lets the model or code answer. Logs every JSON-RPC message to `captures/mcp-wire.jsonl`.
- `fulfillment_agent.py`: A2A SDK server, skill `arrange-delivery`; tasks go submitted, working, working, artifact `shipment`, completed (stand-in timer), or rejected. Pushes only to an allow-listed URL (a stand-in for the SDK's `validate_push_notification_url`, which refuses loopback and private addresses but runs only if passed in; by default the SDK checks nothing). Not production-ready: no `securitySchemes`, no per-order authorization, `GetTask` not limited to the creator; see the docstring.
- `shopping_agent.py`: plain httpx JSON-RPC client: Agent Card, `SendStreamingMessage` with a data part and a push config carrying a fresh token for this task only (registered with the receiver through `captures/push-tokens.txt`), `GetTask`.
- `push_receiver.py`: accepts only tokens the shopping agent minted (constant-time compare, else 401), and only for the task the shopping agent recorded for that token in `captures/push-tasks.txt` (another task gets 403); an update that arrives before the shopping agent has read the task ID is held (202) and released once it's recorded. Processes each update once. Production adds HTTPS and `authentication` credentials.
- `show.py`: prints the chapter's listings from the captures at 72 columns (`captures/listings.txt`).

## Expected output (from `captures/`)

```text
5. a token for another resource: 400 invalid_target
6. tools/list: 7 tools
...
8. get_order_status ord-5002 (Sam's): is_error=True No order with id
    ord-5002.
...
11. place_order with that token: 403
...
   form shown to Rosa: Order 1 x Treasure Island for 1899 cents?
   ord-5005 for cus-001: placed, 1899 cents
```

```text
response: 200 text/event-stream; charset=utf-8
  task 4b13cf9b TASK_STATE_SUBMITTED
  status TASK_STATE_WORKING   Picking ord-5003
  status TASK_STATE_WORKING   Packed and gift-wrapped ord-5003
  artifact shipment {"tracking": "DP50030042", "order_id": "ord-5003",
      "carrier": "demo-post", "gift_wrap": true}
  status TASK_STATE_COMPLETED ord-5003 handed to the carrier
GetTask: TASK_STATE_COMPLETED, 1 artifact
```

Task IDs, and the order of keys in the artifact, differ between runs.
