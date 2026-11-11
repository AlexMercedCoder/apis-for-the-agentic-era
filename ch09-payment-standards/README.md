# Chapter 9: Payment and Commerce Standards

Harbor Books takes payment three ways, all against local stand-ins built
from published specifications, so nothing needs an account:

1. **Checkout through a provider's test mode.** The browser's provider
   field tokenizes a test card (`pm_...`), Harbor creates a payment intent
   for `ord-5005` (3,797 cents) with an `Idempotency-Key`, loses the
   response to a timeout, and retries with the same key: one charge. The
   same failure without a key charges twice. The order turns `paid` only
   when the provider's signed webhook arrives. A partial refund sent twice
   with one key refunds once.
2. **An agent checkout under the Agentic Commerce Protocol (ACP),
   `2026-04-17`.** Rosa's agent host creates a checkout session at
   Harbor, approves the 2,299-cent total under her 3,000-cent limit, gets a
   one-time delegated token (`vt_...`) from the provider with an allowance
   for that amount, merchant, and session, and completes the session
   (`ord-5006`). The token is then refused for a second charge, one cent
   more, another session, and another seller.
3. **A paid request under x402 version 2.** `GET /books/bk-1001/excerpt`
   answers 402 with `PAYMENT-REQUIRED`; the host signs an `exact`-scheme
   EIP-3009 authorization and retries with `PAYMENT-SIGNATURE`; Harbor
   answers 200 with `PAYMENT-RESPONSE`. A replay and a payment edited after
   signing get 402.

## Requirements

- [uv](https://docs.astral.sh/uv/) (0.6.14 used); it installs Python 3.13
  (pinned in `.python-version`) and the packages in `pyproject.toml`:
  `starlette==1.10.0`, `uvicorn==0.54.0`, `httpx==0.28.1`,
  `pyyaml==6.0.3`, `jsonschema==4.26.0`, `eth-account==0.14.0` (locked in
  `uv.lock`)

No account and no API key. Every key in the code is a labeled
placeholder (`sk_test_not_a_real_key_harbor`, `pk_test_not_a_real_key_harbor`,
`sk_test_not_a_real_key_agent_platform`, `harbor_acp_not_a_real_key`), and
the two x402 wallet keys are derived from fixed placeholder strings, never
funded, and used only on `127.0.0.1`. Card numbers are a provider's
published test cards (`4242424242424242` pays, `4000000000000002`
declines). The webhook secret is Chapter 6's placeholder.

```bash
./run.sh          # every listing; writes captures/
```

`run.sh` stops if port 8009 or 8091 is already in use.

## Files

| File | What it is |
|---|---|
| `provider.py` | stand-in payment provider on `127.0.0.1:8091`: `POST /v1/payment_methods` (publishable key; tokenizes test cards), `POST /v1/payment_intents` and `POST /v1/refunds` (form-encoded, secret key, `Idempotency-Key` scoped to the account: replay with `Idempotent-Replayed: true`, 409 while the first request runs, 400 for the same key with other parameters), ACP `POST /agentic_commerce/delegate_payment` (vault token with an allowance; charges check merchant, amount, currency, session, expiry, one use). Sends Standard Webhooks-signed `payment_intent.succeeded` and `charge.refunded` to Harbor. `--slow-first N` holds the first N charge responses 1.5 s. Ledger lines go to stderr (`captures/provider.log`) |
| `harbor_shop.py` | Harbor on `127.0.0.1:8009`: `POST /hooks/payments` (verifies, deduplicates, marks an order paid if the amount matches), `GET /orders/{id}`, ACP seller endpoints `POST /checkout_sessions`, `GET` and `POST .../complete` (require `Authorization`, `API-Version: 2026-04-17`, `Idempotency-Key`; complete charges with key `acp-complete-<session>`), x402 `GET /books/{id}/excerpt` (10000 units of test-network USDC) |
| `x402_local.py` | x402 v2 `PaymentRequired`, `PaymentPayload`, and settlement objects; the `exact` EVM scheme (EIP-712 typed data for EIP-3009 `TransferWithAuthorization`, network `eip155:84532`); a stand-in facilitator whose verify checks version, requirements, recipient, amount, time window, signature, and nonce, and whose settle records the nonce and broadcasts nothing (`transaction` is empty) |
| `checkout_demo.py` | steps 1 to 5 of the provider checkout; `--dump` writes the first charge request |
| `acp_demo.py` | the ACP agent checkout and the token-misuse table; writes the ACP messages as JSON for the contract check |
| `x402_demo.py` | the x402 purchase, replay, and tampered payment; `--dump` writes the decoded exchange |
| `check_contract.py` | validates the captured ACP messages against the pinned OpenAPI schemas |
| `acp/` | ACP `2026-04-17` OpenAPI files (`openapi.agentic_checkout.yaml`, `openapi.delegate_payment.yaml`) and the Apache 2.0 license, pinned from agentic-commerce-protocol/agentic-commerce-protocol at commit `7fdd78d` |
| `webhooks.py` | Chapter 6's Standard Webhooks `sign()` and `verify()` |
| `run.sh` | starts both servers, runs the three demos and the contract check, writes `captures/` |

## What the run prints

`captures/checkout.out`:

```text
2. charge ord-5005 with one Idempotency-Key
  attempt 1: timed out after 1.0 s
  attempt 2: 409 idempotency_key_in_use replayed=false
  attempt 3: 200 pi_... replayed=true
  ord-5005 at Harbor: paid
3. the same lost response, no Idempotency-Key
  attempt 1: timed out after 1.0 s
  attempt 2: 200 pi_... replayed=false
4. the first key again, different amount
  attempt 1: 400 idempotency_key_reused replayed=false
```

`captures/acp.out` ends with:

```text
  same token, second charge         402 allowance already used (one_time)
  seller charges 1 cent more        402 2300 exceeds max_amount 2299
  token used on another session     402 allowance is for another checkout session
  another seller's API key          402 allowance is for harbor-books, not other-shop
  the right seller, amount, session 200 succeeded
```

`captures/x402.out`:

```text
first GET: 402, price 10000, budget 50000
paid GET: 200 Call me Ishmael....
same payment again: 402 invalid_transaction_state
expiry moved after signing: 402 invalid_exact_evm_payload_signature
```

IDs (`pm_`, `pi_`, `re_`, `vt_`, `cs_`) and timestamps change on every run.

## Notes

- In ACP `2026-04-17`, `CheckoutSessionWithOrder` combines
  `CheckoutSessionBase` (which sets `additionalProperties: false`) with an
  `order` member, so no document matches it; the specification's own
  example fails too. `check_contract.py` validates the completed session
  without `order` as a `CheckoutSession` and `order` against `Order`.
- The provider is not Stripe or any real provider; its shape follows the
  common developer-payment-API pattern so the chapter's points (tokens,
  idempotency keys, webhooks) run offline. A real integration uses the
  provider's own SDK, test keys from its dashboard, and its webhook
  signature header.
- Nothing here authenticates customers. Chapter 8's token gate would sit
  in front of Harbor's order and checkout routes.
