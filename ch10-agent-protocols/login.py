"""A scripted sign-in for the MCP client, adapted from Chapter 8's oauth.py.

The scripted browser stands in for Rosa at a browser. It fills Keycloak's
login and consent forms with the placeholder test users from
setup_realm.py and stops at the redirect to the client's callback, the
moment a real browser would hand the code to the app.
"""

import base64
import hashlib
import html
import re
import secrets
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx

CLIENT_ID = "harbor-mcp-client"
CALLBACK = "http://127.0.0.1:8090/callback"
PASSWORD = "not-a-real-password"      # placeholder test users only


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def pkce_pair():
    verifier = b64url(secrets.token_bytes(32))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def _form(resp):
    match = re.search(r'<form[^>]*action="([^"]+)"', resp.text)
    if not match:
        raise RuntimeError("no form on the page")
    return urljoin(str(resp.url), html.unescape(match.group(1)))


def browser_login(url, username):
    """Log in and consent as `username`; return the callback's query."""
    jar = {}

    def send(method, target, **kw):
        cookie = "; ".join(f"{k}={v}" for k, v in jar.items())
        resp = httpx.request(method, target, headers={"Cookie": cookie},
                             follow_redirects=False, **kw)
        for line in resp.headers.get_list("set-cookie"):
            name, _, rest = line.partition("=")
            jar[name] = rest.split(";")[0]
        return resp

    resp = send("GET", url)
    location = ""
    for _ in range(6):
        location = resp.headers.get("location", "")
        if location.startswith(CALLBACK):
            break
        if resp.status_code in (302, 303):
            resp = send("GET", location)
        elif 'name="accept"' in resp.text and "kc-form-login" not in resp.text:
            resp = send("POST", _form(resp), data={"accept": "Yes"})
        elif 'name="password"' in resp.text:
            resp = send("POST", _form(resp), data={
                "username": username, "password": PASSWORD})
        else:
            break
    if not location.startswith(CALLBACK):
        raise RuntimeError(f"login did not reach the callback: "
                           f"{resp.status_code} {resp.text[:200]}")
    return {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}


def sign_in(as_meta, resource, scope, username):
    """Authorization code + PKCE with an RFC 8707 resource indicator."""
    verifier, challenge = pkce_pair()
    state = b64url(secrets.token_bytes(16))
    query = urlencode({
        "response_type": "code", "client_id": CLIENT_ID,
        "redirect_uri": CALLBACK, "scope": scope, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "resource": resource})
    back = browser_login(f"{as_meta['authorization_endpoint']}?{query}",
                         username)
    if back.get("state") != state:
        raise RuntimeError("state mismatch")
    if back.get("iss") != as_meta["issuer"]:          # RFC 9207
        raise RuntimeError("issuer mismatch")
    resp = httpx.post(as_meta["token_endpoint"], data={
        "grant_type": "authorization_code", "code": back["code"],
        "redirect_uri": CALLBACK, "client_id": CLIENT_ID,
        "code_verifier": verifier, "resource": resource})
    return resp


def claims(token: str) -> dict:
    """Decode a JWT payload without checking it (display only)."""
    import json
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(
        payload + "=" * (-len(payload) % 4)))
