"""Try to approve a refund without Rosa: the attacks the approval must stop.

Runs as the host would, with the assistant's client credentials and no
device secret. It asks for an approval, then tries to approve it through
the phone endpoints itself, and checks that no refund token is issued.
It also sends a CIBA request without the openid scope.

    uv run python forge_approval.py
"""

import json
import uuid

import httpx

import issuer

FORM = {"client_id": "harbor-assistant", "client_secret": "not-a-real-secret"}
detail = [{"type": "harbor_refund", "actions": ["refund"],
           "order_id": "ord-5001", "book_id": "bk-1001", "max_amount": 1,
           "currency": "USD", "request_id": str(uuid.uuid4())}]
ask = httpx.post(f"{issuer.ISSUER}/bc-authorize", data={
    **FORM, "scope": "openid orders:refund", "login_hint": "rosa",
    "binding_message": "Refund 0.01 USD for a bookmark?",
    "authorization_details": json.dumps(detail)}).json()
pending = httpx.get(f"{issuer.ISSUER}/phone/pending",
                    params={"person": "rosa"})
print(f"host reads Rosa's phone queue: {pending.status_code}")
for header in ({}, {"Authorization": "Device guess"}):
    r = httpx.post(f"{issuer.ISSUER}/phone/decide", headers=header,
                   data={"auth_req_id": ask["auth_req_id"],
                         "decision": "approve"})
    print(f"host approves its own request ({header or 'no secret'}): "
          f"{r.status_code} {r.json()['error']}")
r = httpx.post(f"{issuer.ISSUER}/token", data={
    **FORM, "grant_type": issuer.CIBA, "auth_req_id": ask["auth_req_id"]})
print(f"token after the forged approvals: {r.status_code} "
      f"{r.json().get('error')}")
r = httpx.post(f"{issuer.ISSUER}/bc-authorize", data={
    **FORM, "scope": "orders:refund", "login_hint": "rosa",
    "authorization_details": json.dumps(detail)})
print(f"CIBA request without openid: {r.status_code} {r.json()['error']}")
