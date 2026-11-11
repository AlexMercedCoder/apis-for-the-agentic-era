"""Put Rosa's payment for ord-5001 on the provider stand-in's ledger.

ord-5001 (one Moby-Dick, 1,499 cents) is delivered in harbor-data, but
each run starts the provider with an empty ledger, so this charges it
once with Chapter 9's test card. The provider's payment_intent.succeeded
webhook tells the refunds API which payment to refund later.
"""

import uuid

import httpx

PROVIDER = "http://127.0.0.1:8091"
pm = httpx.post(f"{PROVIDER}/v1/payment_methods",
                headers={"Authorization":
                         "Bearer pk_test_not_a_real_key_harbor"},
                data={"card[number]": "4242424242424242"}).json()
pi = httpx.post(f"{PROVIDER}/v1/payment_intents",
                headers={"Authorization":
                         "Bearer sk_test_not_a_real_key_harbor",
                         "Idempotency-Key": str(uuid.uuid4())},
                data={"amount": "1499", "currency": "usd",
                      "payment_method": pm["id"], "confirm": "true",
                      "metadata[order_id]": "ord-5001"}).json()
print(f"seeded {pi['id']} {pi['amount']} for ord-5001: {pi['status']}")
