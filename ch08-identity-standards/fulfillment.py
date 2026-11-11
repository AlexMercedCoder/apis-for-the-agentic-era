"""Harbor's fulfillment service reads an order as itself, with no person
involved: the client credentials grant.

    uv run python fulfillment.py
"""

import json

import httpx

from oauth import API, SECRET, claims, discover, show


def main():
    meta = discover()
    resp = httpx.post(meta["token_endpoint"],
                      data={"grant_type": "client_credentials"},
                      auth=("harbor-fulfillment", SECRET))
    print(show(resp))
    token = resp.raise_for_status().json()["access_token"]
    picked = {k: v for k, v in claims(token).items()
              if k in ("aud", "sub", "azp", "scope", "customer_id")}
    print(json.dumps(picked, indent=2))
    order = httpx.get(f"{API}/orders/ord-5002",
                      headers={"Authorization": f"Bearer {token}"})
    print(f"\nGET /orders/ord-5002: {order.status_code} "
          f"{order.json()['customer_id']} {order.json()['status']}")
    put = httpx.put(f"{API}/orders/ord-5002/cancellation",
                    json={"reason": "other"},
                    headers={"Authorization": f"Bearer {token}"})
    print(f"PUT /orders/ord-5002/cancellation: {put.status_code} "
          f"{put.json()['detail']}")


if __name__ == "__main__":
    main()
