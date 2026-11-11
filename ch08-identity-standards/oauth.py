"""Shared helpers for Chapter 8: discovery, PKCE, a scripted browser login,
and token redaction for captures.

The scripted login stands in for a person at a browser. It fills Keycloak's
login and consent forms with the placeholder test users from
setup_realm.py, and stops at the redirect to the storefront's callback, the
moment a real browser would hand the code to the app.
"""

import base64
import hashlib
import html
import json
import re
import secrets
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx

ISSUER = "http://127.0.0.1:8080/realms/harbor"
API = "http://127.0.0.1:8008"
CALLBACK = "http://127.0.0.1:8090/callback"
PASSWORD = "not-a-real-password"      # placeholder test users only
SECRET = "not-a-real-secret"          # placeholder client secrets only


def discover():
    url = f"{ISSUER}/.well-known/openid-configuration"
    return httpx.get(url).raise_for_status().json()


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def pkce_pair():
    verifier = b64url(secrets.token_bytes(32))         # 43 characters
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def authorization_url(meta, scope, challenge, state, nonce,
                      client_id="harbor-storefront"):
    query = urlencode({
        "response_type": "code", "client_id": client_id,
        "redirect_uri": CALLBACK, "scope": scope, "state": state,
        "nonce": nonce, "code_challenge": challenge,
        "code_challenge_method": "S256"})
    return f"{meta['authorization_endpoint']}?{query}"


def _form(resp):
    match = re.search(r'<form[^>]*action="([^"]+)"', resp.text)
    if not match:
        raise RuntimeError("no form on the page")
    return urljoin(str(resp.url), html.unescape(match.group(1)))


def browser_login(url, username):
    """Log in and consent as `username`; return the callback's query.

    Keycloak marks its cookies Secure. Browsers send Secure cookies to
    127.0.0.1 over plain HTTP, Python's cookie jar doesn't, so this
    stand-in carries them by hand.
    """
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
    for _ in range(6):
        location = resp.headers.get("location", "")
        if location.startswith(CALLBACK):
            break
        if resp.status_code in (302, 303):
            resp = send("GET", location)
        elif 'name="accept"' in resp.text and "kc-form-login" not in resp.text:
            # The consent screen: grant what the client asked for.
            resp = send("POST", _form(resp), data={"accept": "Yes"})
        elif 'name="password"' in resp.text:
            resp = send("POST", _form(resp), data={
                "username": username, "password": PASSWORD})
        else:
            break
    if not location.startswith(CALLBACK):
        raise RuntimeError(f"login did not reach the callback: "
                           f"{resp.status_code} {location[:80]}")
    return {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}


def claims(token: str) -> dict:
    """Decode a JWT payload without checking it (display only)."""
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def header(token: str) -> dict:
    head = token.split(".")[0]
    return json.loads(base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)))


def redact(token: str) -> str:
    """Show a token's shape without its value."""
    try:
        header(token)
        return f"<JWT {len(token)} chars>"
    except ValueError:
        return f"<{len(token)} chars>"


TOKEN_FIELDS = {"access_token", "refresh_token", "id_token", "subject_token",
                "actor_token", "code", "code_verifier", "client_secret"}


def show(resp, keep=("content-type", "authorization", "dpop",
                     "www-authenticate", "location", "cache-control"),
         body_limit=None):
    """Render one exchange as raw HTTP, with every secret redacted."""
    req = resp.request
    url = req.url
    lines = [f"{req.method} {url.raw_path.decode()} HTTP/1.1",
             f"Host: {url.netloc.decode()}"]
    for raw_name, raw_value in req.headers.raw:
        name, value = raw_name.decode(), raw_value.decode()
        if name.lower() in keep:
            if name.lower() == "authorization":
                scheme, _, cred = value.partition(" ")
                value = f"{scheme} " + ("<client_id:client_secret>"
                                       if scheme == "Basic" else redact(cred))
            if name.lower() == "dpop":
                value = redact(value)
            lines.append(f"{name}: {value}")
    if req.content:
        form = parse_qs(req.content.decode())
        if form and "=" in req.content.decode():
            lines.append("")
            pairs = [f"{k}={redact(v[0]) if k in TOKEN_FIELDS else v[0]}"
                     for k, v in form.items()]
            lines.append("&\n".join(pairs))
    lines += ["", f"HTTP/1.1 {resp.status_code} {resp.reason_phrase}"]
    for name, value in resp.headers.items():
        if name.lower() in keep:
            lines.append(f"{name}: {value}")
    if resp.content:
        lines.append("")
        try:
            body = resp.json()
            if isinstance(body, dict):
                body = {k: (redact(v) if k in TOKEN_FIELDS else v)
                        for k, v in body.items()}
            text = json.dumps(body, indent=2)
        except ValueError:
            text = resp.text
        lines.append(text if body_limit is None
                     else "\n".join(text.splitlines()[:body_limit]))
    return "\n".join(lines)
