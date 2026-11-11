"""Harbor's checkout against the stand-in provider (provider.py).

1. The browser's provider field tokenizes the test card; Harbor sees pm_...
2. Harbor charges order ord-5005 with one Idempotency-Key, times out
   (the provider holds the first response), and retries with the same key.
3. The same lost response without a key: two charges.
4. The key reused with a different amount: refused.
5. A partial refund sent twice with one key: refunded once.
The order turns paid only when the signed webhook reaches Harbor.

Usage: checkout_demo.py --dump captures/charge.http
"""

import argparse
import time
import uuid

import httpx

from provider import MERCHANT_PUBLISHABLE_KEY, MERCHANT_SECRET_KEY

PROVIDER = "http://127.0.0.1:8091"
HARBOR = "http://127.0.0.1:8009"
AUTH = {"Authorization": f"Bearer {MERCHANT_SECRET_KEY}"}


def dump_request(path: str, request: httpx.Request) -> None:
    lines = [f"{request.method} {request.url.raw_path.decode()} HTTP/1.1",
             f"Host: {request.url.netloc.decode()}",
             "Authorization: Bearer sk_test_not_a_real_key_harbor",
             f"Content-Type: {request.headers['content-type']}",
             f"Idempotency-Key: {request.headers['idempotency-key']}",
             "", request.content.decode().replace("&", "&\n")]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def charge(form: dict, key: str | None, dump: str | None = None) -> dict:
    """POST a payment intent, retrying with the same key until answered."""
    headers = dict(AUTH)
    if key:
        headers["Idempotency-Key"] = key
    for attempt in range(1, 5):
        try:
            with httpx.Client(timeout=1.0) as client:
                request = client.build_request(
                    "POST", f"{PROVIDER}/v1/payment_intents",
                    headers=headers, data=form)
                if dump and attempt == 1:
                    dump_request(dump, request)
                r = client.send(request)
        except httpx.TimeoutException:
            print(f"  attempt {attempt}: timed out after 1.0 s")
            continue
        replayed = r.headers.get("idempotent-replayed", "false")
        body = r.json()
        print(f"  attempt {attempt}: {r.status_code} "
              f"{body.get('id') or body['error']['code']} "
              f"replayed={replayed}")
        if r.status_code == 409:
            time.sleep(1.0)
            continue
        return body
    raise SystemExit("gave up")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dump")
    args = parser.parse_args()

    print("1. tokenize in the browser (publishable key)")
    r = httpx.post(f"{PROVIDER}/v1/payment_methods",
                   headers={"Authorization":
                            f"Bearer {MERCHANT_PUBLISHABLE_KEY}"},
                   data={"type": "card", "card[number]": "4242424242424242",
                         "card[exp_month]": "12", "card[exp_year]": "2030",
                         "card[cvc]": "123"})
    pm = r.json()
    print(f"  {pm['id']} card ****{pm['card']['last4']}; "
          "Harbor receives only the pm_ ID")

    form = {"amount": "3797", "currency": "usd", "payment_method": pm["id"],
            "confirm": "true", "metadata[order_id]": "ord-5005"}
    key = str(uuid.uuid4())
    print("2. charge ord-5005 with one Idempotency-Key")
    first = charge(form, key, dump=args.dump)
    time.sleep(0.5)
    status = httpx.get(f"{HARBOR}/orders/ord-5005").json()["status"]
    print(f"  ord-5005 at Harbor: {status}")

    print("3. the same lost response, no Idempotency-Key")
    charge({**form, "metadata[order_id]": "demo-no-key"}, None)

    print("4. the first key again, different amount")
    charge({**form, "amount": "3798"}, key)

    print("5. refund one damaged Little Women copy, sent twice")
    refund_key = str(uuid.uuid4())
    for attempt in (1, 2):
        r = httpx.post(f"{PROVIDER}/v1/refunds",
                       headers={**AUTH, "Idempotency-Key": refund_key},
                       data={"payment_intent": first["id"],
                             "amount": "1299"})
        print(f"  attempt {attempt}: {r.status_code} {r.json()['id']} "
              f"replayed={r.headers.get('idempotent-replayed', 'false')}")
    time.sleep(0.5)


if __name__ == "__main__":
    main()
