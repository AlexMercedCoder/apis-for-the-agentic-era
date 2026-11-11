"""Refresh tokens rotate in Harbor's setup, and reusing one ends the
session. A JWT the API checks locally keeps working until it expires;
introspection (RFC 7662) sees the session is gone at once.

Uses the `reader` login that run.sh makes before Rosa's main one.

    uv run python refresh_demo.py
"""

import json
from pathlib import Path

import httpx

from oauth import API, SECRET, discover

TOKENS = json.loads(Path(__file__).with_name("tokens.json").read_text())


def refresh(meta, token):
    return httpx.post(meta["token_endpoint"], data={
        "grant_type": "refresh_token", "refresh_token": token,
        "client_id": "harbor-storefront"})


def main():
    meta = discover()
    first = TOKENS["reader"]["refresh_token"]
    resp = refresh(meta, first)
    print(f"refresh, first use:          {resp.status_code}")
    fresh = resp.json()
    resp = refresh(meta, first)
    print(f"refresh, same token again:   {resp.status_code} "
          f"{resp.json()['error']}")
    resp = refresh(meta, fresh["refresh_token"])
    print(f"refresh, the newer token:    {resp.status_code} "
          f"{resp.json()['error']} ({resp.json()['error_description']})")
    access = fresh["access_token"]
    api = httpx.get(f"{API}/orders/ord-5003",
                    headers={"Authorization": f"Bearer {access}"})
    print(f"Harbor, checking the JWT:    {api.status_code}")
    seen = httpx.post(meta["introspection_endpoint"],
                      data={"token": access},
                      auth=("harbor-fulfillment", SECRET))
    print(f"introspection of that token: active={seen.json()['active']}")


if __name__ == "__main__":
    main()
