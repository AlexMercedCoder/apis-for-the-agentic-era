"""Signing and verifying webhooks the way Standard Webhooks 1.0.0 does.

The sender signs "<webhook-id>.<webhook-timestamp>.<body>" with HMAC-SHA256
and a shared secret, and sends the result as "v1,<base64>" in the
webhook-signature header. The receiver recomputes it, compares in constant
time, and refuses a timestamp outside its tolerance. Values are compared
as bytes, so a non-ASCII header is a failed match, not a crash.

SECRET is a placeholder for this demo, not a real key. A real secret is
random (24 to 64 bytes), generated per subscription, and kept out of Git.
"""

import base64
import hashlib
import hmac
import time

SECRET = "whsec_" + base64.b64encode(
    b"not-a-real-secret-for-harbor-hooks").decode()
TOLERANCE_SECONDS = 300  # the message in verify() quotes this value


class InvalidWebhook(Exception):
    pass


def _key(secret: str) -> bytes:
    return base64.b64decode(secret.removeprefix("whsec_"))


def sign(secret: str, msg_id: str, timestamp: str, body: bytes) -> str:
    signed = f"{msg_id}.{timestamp}.".encode() + body
    mac = hmac.new(_key(secret), signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


def verify(secret: str, headers, body: bytes, now=None) -> str:
    """Return the webhook-id if the delivery is genuine and fresh."""
    msg_id = headers.get("webhook-id")
    timestamp = headers.get("webhook-timestamp")
    signatures = headers.get("webhook-signature")
    if not (msg_id and timestamp and signatures):
        raise InvalidWebhook("missing webhook headers")
    if not (timestamp.isascii() and timestamp.isdigit()):
        raise InvalidWebhook("webhook-timestamp is not a number")
    now = int(time.time()) if now is None else now
    if abs(now - int(timestamp)) > TOLERANCE_SECONDS:
        raise InvalidWebhook("timestamp over 300 seconds from now")
    expected = sign(secret, msg_id, timestamp, body).partition(",")[2]
    for candidate in signatures.split():
        version, _, value = candidate.partition(",")
        if version == "v1" and hmac.compare_digest(
                value.encode(), expected.encode()):
            return msg_id
    raise InvalidWebhook("no signature matches")
