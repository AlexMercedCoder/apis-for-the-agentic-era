"""The authorization code flow with PKCE, as Harbor's storefront runs it.

Prints the authorization request, the redirect back, the token request and
response (every secret redacted), and then a token request with the wrong
code verifier, which Keycloak refuses. The tokens go into the git-ignored
tokens.json for the other scripts.

    uv run python code_flow.py [username] [scopes]
"""

import json
import secrets
import sys
from pathlib import Path
from urllib.parse import urlencode

import httpx

from oauth import (CALLBACK, browser_login, discover, pkce_pair, redact,
                   authorization_url, show)

STORE = Path(__file__).with_name("tokens.json")


def main():
    user = sys.argv[1] if len(sys.argv) > 1 else "rosa"
    scope = (sys.argv[2] if len(sys.argv) > 2 else
             "openid orders:read orders:write "
             "delegation:client:harbor-assistant")
    meta = discover()
    verifier, challenge = pkce_pair()
    state, nonce = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
    url = authorization_url(meta, scope, challenge, state, nonce)
    print("== 1. authorization request (the browser is sent here)")
    print(url.replace("?", "\n  ?").replace("&", "\n  &"))

    callback = browser_login(url, user)
    print("\n== 2. redirect back to the storefront")
    print(f"{CALLBACK}?" + "&".join(
        f"{k}={redact(v) if k == 'code' else v}"
        for k, v in callback.items()))
    assert callback["state"] == state, "state mismatch: drop the response"
    assert callback["iss"] == meta["issuer"], "issuer mismatch (RFC 9207)"

    print("\n== 3. a token request with the wrong code verifier")
    wrong, _ = pkce_pair()
    bad = httpx.post(meta["token_endpoint"], data={
        "grant_type": "authorization_code", "code": callback["code"],
        "redirect_uri": CALLBACK, "client_id": "harbor-storefront",
        "code_verifier": wrong})
    print(show(bad))

    # A failed redemption burns the code, so log in again for the real one.
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    callback = browser_login(
        authorization_url(meta, scope, challenge, state, nonce), user)
    assert callback["state"] == state
    print("\n== 4. the token request with the right verifier")
    resp = httpx.post(meta["token_endpoint"], data={
        "grant_type": "authorization_code", "code": callback["code"],
        "redirect_uri": CALLBACK, "client_id": "harbor-storefront",
        "code_verifier": verifier})
    print(show(resp))
    resp.raise_for_status()
    tokens = json.loads(STORE.read_text()) if STORE.exists() else {}
    tokens[user] = resp.json()
    tokens[user]["nonce"] = nonce
    STORE.write_text(json.dumps(tokens, indent=2))


if __name__ == "__main__":
    main()
