# Chapter 8: Identity Standards with OAuth 2.0, OpenID Connect, and Tokens

Chapter 3's Harbor REST API, unchanged, behind a local OpenID provider
(Keycloak). A customer signs in with the authorization code flow and PKCE,
a fulfillment service uses client credentials, Harbor validates JWT access
tokens and DPoP proofs and serves its OAuth 2.0 Protected Resource Metadata,
and Harbor's assistant acts for a customer through token exchange.

## Requirements

- Docker with Compose v2. Image: `quay.io/keycloak/keycloak:26.8.2`
  (pulled on first run), started with the preview features
  `token-exchange-delegation,parameterized-scopes`
- [uv](https://docs.astral.sh/uv/); it installs Python 3.13 (pinned in
  `.python-version`) and `fastapi==0.162.0`, `uvicorn==0.54.0`,
  `httpx==0.28.1`, `pyjwt[crypto]==2.15.1` (locked in `uv.lock`)
- `../ch03-rest-apis/harbor_api.py`, imported as is
- Every client's access tokens carry the RFC 9068 header type `at+jwt`
  (Keycloak's per-client `access.token.header.type.rfc9068`)

Every password and client secret here is a placeholder for this local stack:
the Keycloak admin `admin` / `not-a-real-password`, the test customers `rosa`
(`cus-001`) and `sam` (`cus-002`) with `not-a-real-password`, and the clients
`harbor-fulfillment` and `harbor-assistant` with `not-a-real-secret`.
Captures print tokens, codes, and verifiers as `<JWT n chars>` or
`<n chars>`, and client credentials as `<client_id:client_secret>`.
Tokens live only in the git-ignored `tokens.json` during a run, and `run.sh`
deletes it. Everything uses plain HTTP on `127.0.0.1`, which is safe only
on the loopback address.

```bash
./run.sh          # every listing; writes captures/ (about a minute)
KEEP=1 ./run.sh   # the same, and leave Keycloak running
```

State values, keys, IDs, and timestamps change on every run; the status
codes, errors, scopes, and claims don't.

## Ports

| Port | Service |
|---|---|
| `127.0.0.1:8080` | Keycloak, realm `harbor` |
| `127.0.0.1:8008` | Harbor's protected API (`harbor_secured.py`) |
| `127.0.0.1:8090` | the storefront's callback URL (nothing listens; the scripted login stops at the redirect) |

## Files

| File | What it does | Capture |
|---|---|---|
| `docker-compose.yml` | Keycloak 26.8.2 in `start-dev` mode | |
| `setup_realm.py` | Realm `harbor` through the admin API: scopes `orders:read`, `orders:write`, `orders:read:all` (each adds audience `http://127.0.0.1:8008`; the first two add `customer_id`), clients `harbor-storefront` (public, code + PKCE S256, consent), `harbor-fulfillment` (client credentials), `harbor-assistant` (confidential, standard token exchange, DPoP required on every token request), users, refresh token rotation, and the permission that lets the assistant act for a user | `setup.out` |
| `oauth.py` | Discovery, PKCE, a scripted browser login, redaction | |
| `harbor_secured.py` | The resource server: JWT checks (`typ` `at+jwt`, RS256 only, `iss`, `aud`, `exp`), fixed error descriptions (nothing from the token or the request path is echoed into `WWW-Authenticate`; log lines use the still-encoded path), delegated tokens refused unless DPoP-bound, one `Authorization` and one `DPoP` header only, DPoP proof checks with a replay cache kept for each proof's whole window, unknown `cnf` bindings refused, scopes, ownership with one identical 404 for missing and foreign orders, `/.well-known/oauth-protected-resource` | `api.log`, `protected-resource.json` |
| `code_flow.py` | Authorization request (fresh `state`, `nonce`, verifier each time), callback with `state` and `iss` checks, the token response, ID token validation, a second login's code with a wrong verifier | `code-flow.out` |
| `refresh_demo.py` | Refresh token rotation, reuse ending the storefront's session, local validation against introspection | `refresh.out` |
| `call_api.py` | A decoded token, no token, an ID token, `alg: none` and HS256 key-confusion forgeries with the real `kid`, an edited payload, two `Authorization` headers, someone else's order against a missing one, someone else's `customer_id`, a missing scope | `call-api.out` |
| `fulfillment.py` | Client credentials; reads any order, can't cancel | `fulfillment.out` |
| `agent_exchange.py` | The assistant-only grant (audience `harbor-assistant`, refused by Harbor), actor token, delegated DPoP-bound token exchange, a stolen token, a replayed proof, a write refused | `agent-exchange.out` |

`discovery.json` is Keycloak's OpenID configuration for the realm.

## Not for production

`start-dev` keeps state in memory and serves HTTP. Use Keycloak 26.8.2 or later for delegation: 26.8.2 fixed exchanged tokens that could outlive their subject token. Token exchange
delegation is a preview feature in Keycloak 26.8. Harbor's DPoP replay cache
lives in memory, so a production API with several instances needs a shared
store for proof IDs.
