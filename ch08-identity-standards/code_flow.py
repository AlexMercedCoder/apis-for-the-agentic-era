"""The authorization code flow with PKCE, as Harbor's storefront runs it.

Prints the authorization request, the redirect back, the token request and
response (every secret redacted), the storefront's checks on the ID token,
and then a second login whose code is sent with the wrong code verifier,
which Keycloak refuses. The tokens go into the git-ignored tokens.json for
the other scripts, under the name given as the third argument.

    uv run python code_flow.py [username] [scopes] [name] [--quiet]
"""

import json
import secrets
import sys
from pathlib import Path

import httpx
import jwt

from oauth import (CALLBACK, browser_login, discover, pkce_pair, redact,
                   authorization_url, show)

STORE = Path(__file__).with_name("tokens.json")
CLIENT = "harbor-storefront"


def login(meta, user, scope, quiet):
    """One authorization request with its own state, nonce, and verifier."""
    verifier, challenge = pkce_pair()
    state, nonce = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
    url = authorization_url(meta, scope, challenge, state, nonce)
    if not quiet:
        print("== 1. authorization request (the browser is sent here)")
        print(url.replace("?", "\n  ?").replace("&", "\n  &"))
    callback = browser_login(url, user)
    if not quiet:
        print("\n== 2. redirect back to the storefront")
        print(f"{CALLBACK}?" + "&".join(
            f"{k}={redact(v) if k == 'code' else v}"
            for k, v in callback.items()))
    if callback["state"] != state:
        raise SystemExit("state mismatch: drop the response")
    if callback.get("iss") != meta["issuer"]:
        raise SystemExit("issuer mismatch (RFC 9207): drop the response")
    return callback["code"], verifier, nonce


def redeem(meta, code, verifier):
    return httpx.post(meta["token_endpoint"], data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": CALLBACK, "client_id": CLIENT,
        "code_verifier": verifier})


def check_id_token(meta, id_token, nonce):
    """OIDC Core 3.1.3.7: signature, iss, aud, azp, exp, and our nonce."""
    keys = jwt.PyJWKClient(meta["jwks_uri"])
    claims = jwt.decode(id_token, keys.get_signing_key_from_jwt(id_token).key,
                        algorithms=["RS256"], audience=CLIENT,
                        issuer=meta["issuer"],
                        options={"require": ["exp", "iat", "sub", "nonce"]})
    if claims["nonce"] != nonce or claims.get("azp", CLIENT) != CLIENT:
        raise SystemExit("ID token is not for this login")
    return claims


def main():
    args = [a for a in sys.argv[1:] if a != "--quiet"]
    quiet = "--quiet" in sys.argv
    user = args[0] if len(args) > 0 else "rosa"
    scope = args[1] if len(args) > 1 else "openid orders:read orders:write"
    name = args[2] if len(args) > 2 else user
    meta = discover()

    code, verifier, nonce = login(meta, user, scope, quiet)
    resp = redeem(meta, code, verifier)
    if not quiet:
        print("\n== 3. the token request with the code verifier")
        print(show(resp))
    resp.raise_for_status()
    tokens = resp.json()
    who = check_id_token(meta, tokens["id_token"], nonce)
    if not quiet:
        print(f"\nID token checked (signature, iss, aud, azp, exp, nonce): "
              f"{who['preferred_username']}")

        print("\n== 4. a second login's code with the wrong code verifier")
        code, _, _ = login(meta, user, scope, quiet=True)
        wrong, _ = pkce_pair()
        print(show(redeem(meta, code, wrong)))

    store = json.loads(STORE.read_text()) if STORE.exists() else {}
    store[name] = tokens
    STORE.write_text(json.dumps(store, indent=2))


if __name__ == "__main__":
    main()
