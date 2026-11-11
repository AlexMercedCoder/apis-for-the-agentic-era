"""Harbor's assistant acts for Rosa through token exchange (RFC 8693).

0. The storefront asks Keycloak for a token just for the assistant, with
   only the delegation scope, so its only audience is the assistant and
   Harbor refuses it. The assistant never holds Rosa's storefront token.
1. The assistant authenticates as itself (client credentials) to get an
   actor token.
2. It exchanges that grant, plus its actor token, for a new token
   that names Rosa as the subject and the assistant as the actor (`act`),
   narrowed to orders:read, and bound to a key the assistant holds (DPoP,
   RFC 9449).
3. It calls Harbor with that token and a fresh DPoP proof, and then shows
   what a thief holding only the token, or a replayed proof, gets.

    uv run python code_flow.py rosa \
        "openid delegation:client:harbor-assistant" assistant-grant --quiet
    uv run python agent_exchange.py
"""

import hashlib
import json
import time
import uuid
from pathlib import Path

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from oauth import API, SECRET, b64url, claims, discover, show

TOKENS = json.loads(Path(__file__).with_name("tokens.json").read_text())
EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS = "urn:ietf:params:oauth:token-type:access_token"


class DPoPKey:
    """The assistant's private key. It never leaves this process."""

    def __init__(self):
        self.private = ec.generate_private_key(ec.SECP256R1())
        public = jwt.algorithms.ECAlgorithm.to_jwk(
            self.private.public_key(), as_dict=True)
        self.jwk = {k: public[k] for k in ("kty", "crv", "x", "y")}

    def proof(self, method, url, token=None):
        payload = {"jti": str(uuid.uuid4()), "htm": method, "htu": url,
                   "iat": int(time.time())}
        if token:
            payload["ath"] = b64url(hashlib.sha256(token.encode()).digest())
        return jwt.encode(payload, self.private, algorithm="ES256",
                          headers={"typ": "dpop+jwt", "jwk": self.jwk})


def main():
    meta = discover()
    token_url = meta["token_endpoint"]
    key = DPoPKey()
    grant = TOKENS["assistant-grant"]["access_token"]
    url = f"{API}/orders/ord-5003"

    print("== 0. the grant the storefront gives the assistant")
    picked = {k: v for k, v in claims(grant).items()
              if k in ("aud", "azp", "scope", "may_act")}
    print(json.dumps(picked, indent=2))
    direct = httpx.get(url, headers={"Authorization": f"Bearer {grant}"})
    print(f"the grant sent straight to Harbor: {direct.status_code} "
          f"{direct.json()['detail']}\n")

    print("== 1. the assistant's own token (client credentials)")
    actor = httpx.post(token_url, data={"grant_type": "client_credentials"},
                       headers={"DPoP": key.proof("POST", token_url)},
                       auth=("harbor-assistant", SECRET))
    print(show(actor))
    actor = actor.raise_for_status().json()["access_token"]

    print("\n== 2. token exchange: Rosa's grant in, a delegated token out")
    form = {"grant_type": EXCHANGE, "subject_token": grant,
            "subject_token_type": ACCESS, "actor_token": actor,
            "actor_token_type": ACCESS, "scope": "orders:read"}
    unbound = httpx.post(token_url, auth=("harbor-assistant", SECRET),
                         data=form)
    print(f"exchange without a DPoP proof: {unbound.status_code} "
          f"{unbound.json().get('error_description')}\n")
    resp = httpx.post(token_url, auth=("harbor-assistant", SECRET),
                      headers={"DPoP": key.proof("POST", token_url)},
                      data={"grant_type": EXCHANGE,
                            "subject_token": grant,
                            "subject_token_type": ACCESS,
                            "actor_token": actor,
                            "actor_token_type": ACCESS,
                            "scope": "orders:read"})
    print(show(resp))
    delegated = resp.raise_for_status().json()["access_token"]
    picked = {k: v for k, v in claims(delegated).items()
              if k in ("iss", "aud", "sub", "azp", "scope", "customer_id",
                       "act", "cnf", "exp")}
    print(json.dumps(picked, indent=2))

    print("\n== 3. the assistant reads Rosa's order with a fresh proof")
    proof = key.proof("GET", url, delegated)
    resp = httpx.get(url, headers={"Authorization": f"DPoP {delegated}",
                                   "DPoP": proof})
    print(show(resp, body_limit=4))

    print("\n== 4. a thief with the token but not the key")
    stolen = httpx.get(url, headers={"Authorization": f"Bearer {delegated}"})
    print(show(stolen))
    replay = httpx.get(url, headers={"Authorization": f"DPoP {delegated}",
                                     "DPoP": proof})
    print(f"\nreplayed proof: {replay.status_code} "
          f"{replay.json()['detail']}")

    print("\n== 5. the delegated token tries to cancel the order")
    cancel = f"{url}/cancellation"
    resp = httpx.put(cancel, json={"reason": "customer_request"},
                     headers={"Authorization": f"DPoP {delegated}",
                              "DPoP": key.proof("PUT", cancel, delegated)})
    print(f"{resp.status_code} {resp.headers['www-authenticate']}")


if __name__ == "__main__":
    main()
