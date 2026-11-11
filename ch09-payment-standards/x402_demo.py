"""An agent host buys one excerpt from Harbor under x402 version 2.

GET without payment gets 402 and a PAYMENT-REQUIRED header. The host
checks the price against its budget, signs the exact-scheme payment with
its wallet key (a placeholder; see x402_local.py), and retries with a
PAYMENT-SIGNATURE header. Then it replays the same payment and sends one
whose expiry was moved after signing.

Usage: x402_demo.py --dump captures/x402.http
"""

import argparse
import copy
import json

import httpx

import x402_local

URL = "http://127.0.0.1:8009/books/bk-1001/excerpt"
BUDGET_PER_CALL = 50000   # the host's rule: at most 0.05 USDC per request


def headers_text(r: httpx.Response, names: list[str]) -> str:
    return "\n".join(f"{n}: {r.headers[n]}" for n in names
                     if n in r.headers)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dump")
    args = parser.parse_args()
    out = []

    r = httpx.get(URL)
    required = x402_local.unb64(r.headers["payment-required"])
    out += [f"GET {URL[len('http://127.0.0.1:8009'):]} HTTP/1.1", "",
            f"HTTP/1.1 {r.status_code} Payment Required",
            "payment-required: <base64 of the JSON below>", "",
            json.dumps(required, indent=2), ""]
    price = int(required["accepts"][0]["amount"])
    print(f"first GET: {r.status_code}, price {price}, "
          f"budget {BUDGET_PER_CALL}")
    if price > BUDGET_PER_CALL:
        raise SystemExit("over budget; ask the person")

    payment = x402_local.sign_payment(required)
    r = httpx.get(URL, headers={
        "PAYMENT-SIGNATURE": x402_local.b64(payment)})
    settled = x402_local.unb64(r.headers["payment-response"])
    out += [f"GET {URL[len('http://127.0.0.1:8009'):]} HTTP/1.1",
            "payment-signature: <base64 of the signed payment>", "",
            f"HTTP/1.1 {r.status_code} OK",
            "payment-response: <base64 of the JSON below>", "",
            json.dumps(settled, indent=2)]
    print(f"paid GET: {r.status_code} {r.json()['excerpt'][:16]}...")
    if args.dump:
        with open(args.dump, "w") as f:
            f.write("\n".join(out) + "\n")
        with open(args.dump.replace(".http", "-payment.json"), "w") as f:
            json.dump(payment, f, indent=2)

    r = httpx.get(URL, headers={
        "PAYMENT-SIGNATURE": x402_local.b64(payment)})
    reason = x402_local.unb64(r.headers["payment-required"])["error"]
    print(f"same payment again: {r.status_code} {reason}")

    tampered = copy.deepcopy(x402_local.sign_payment(required))
    auth = tampered["payload"]["authorization"]
    auth["validBefore"] = str(int(auth["validBefore"]) + 3600)
    r = httpx.get(URL, headers={
        "PAYMENT-SIGNATURE": x402_local.b64(tampered)})
    reason = x402_local.unb64(r.headers["payment-required"])["error"]
    print(f"expiry moved after signing: {r.status_code} {reason}")


if __name__ == "__main__":
    main()
