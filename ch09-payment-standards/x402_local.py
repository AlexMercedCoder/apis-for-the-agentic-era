"""x402 version 2 messages and the `exact` EVM scheme, with a local facilitator.

The message shapes, header names, and checks follow the x402 v2
specification and its HTTP transport (x402-foundation/x402,
specs/x402-specification-v2.md and specs/transports-v2/http.md). The
payment is an EIP-3009 transferWithAuthorization signed as EIP-712 typed
data, as the `exact` scheme on EVM networks specifies.

The facilitator here is a stand-in. It verifies signatures and amounts
the way a facilitator's /verify does, and "settles" by recording the
nonce. It broadcasts nothing, so the settlement response carries an empty
transaction hash, which the specification allows when no transaction was
broadcast. Network eip155:84532 is Base Sepolia, a test network.

Both keys below are placeholders derived from fixed strings, never
funded, and used only on 127.0.0.1.
"""

import base64
import hashlib
import json
import secrets
import time

from eth_account import Account
from eth_account.messages import encode_typed_data

NETWORK = "eip155:84532"
CHAIN_ID = 84532
USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"  # test-network USDC
HARBOR_KEY = "0x" + hashlib.sha256(b"harbor-x402-placeholder-key").hexdigest()
AGENT_KEY = "0x" + hashlib.sha256(b"agent-wallet-placeholder-key").hexdigest()
HARBOR_ADDRESS = Account.from_key(HARBOR_KEY).address

TYPES = {"TransferWithAuthorization": [
    {"name": "from", "type": "address"},
    {"name": "to", "type": "address"},
    {"name": "value", "type": "uint256"},
    {"name": "validAfter", "type": "uint256"},
    {"name": "validBefore", "type": "uint256"},
    {"name": "nonce", "type": "bytes32"},
]}
used_nonces: set[str] = set()


def b64(obj: dict) -> str:
    return base64.b64encode(json.dumps(obj, separators=(",", ":"))
                            .encode()).decode()


def unb64(value: str) -> dict:
    return json.loads(base64.b64decode(value))


def payment_required(url: str, amount: str, error: str) -> dict:
    return {
        "x402Version": 2,
        "error": error,
        "resource": {"url": url, "description": "One chapter excerpt",
                     "mimeType": "application/json"},
        "accepts": [{
            "scheme": "exact", "network": NETWORK, "amount": amount,
            "asset": USDC, "payTo": HARBOR_ADDRESS, "maxTimeoutSeconds": 60,
            "extra": {"name": "USDC", "version": "2"},
        }],
    }


def _domain(req: dict) -> dict:
    return {"name": req["extra"]["name"], "version": req["extra"]["version"],
            "chainId": CHAIN_ID, "verifyingContract": req["asset"]}


def _message(auth: dict) -> dict:
    return {"from": auth["from"], "to": auth["to"],
            "value": int(auth["value"]),
            "validAfter": int(auth["validAfter"]),
            "validBefore": int(auth["validBefore"]),
            "nonce": bytes.fromhex(auth["nonce"][2:])}


def sign_payment(required: dict, key: str = AGENT_KEY) -> dict:
    """Client side: accept the first requirement and sign for it."""
    req = required["accepts"][0]
    now = int(time.time())
    auth = {"from": Account.from_key(key).address, "to": req["payTo"],
            "value": req["amount"], "validAfter": str(now - 5),
            "validBefore": str(now + req["maxTimeoutSeconds"]),
            "nonce": "0x" + secrets.token_hex(32)}
    signed = Account.sign_typed_data(key, domain_data=_domain(req),
                                     message_types=TYPES,
                                     message_data=_message(auth))
    return {"x402Version": 2, "resource": required["resource"],
            "accepted": req,
            "payload": {"signature": "0x" + signed.signature.hex(),
                        "authorization": auth}}


def verify(payload: dict, required: dict) -> str | None:
    """Facilitator /verify: return an invalidReason, or None if valid."""
    if payload.get("x402Version") != 2:
        return "invalid_x402_version"
    req = required["accepts"][0]
    if payload.get("accepted") != req:
        return "invalid_payment_requirements"
    auth = payload["payload"]["authorization"]
    if auth["to"] != req["payTo"]:
        return "invalid_exact_evm_payload_recipient_mismatch"
    if auth["value"] != req["amount"]:
        return "invalid_exact_evm_payload_authorization_value_mismatch"
    now = int(time.time())
    if now < int(auth["validAfter"]):
        return "invalid_exact_evm_payload_authorization_valid_after"
    if now > int(auth["validBefore"]):
        return "invalid_exact_evm_payload_authorization_valid_before"
    signable = encode_typed_data(domain_data=_domain(req),
                                 message_types=TYPES,
                                 message_data=_message(auth))
    try:
        signer = Account.recover_message(
            signable, signature=payload["payload"]["signature"])
    except Exception:
        return "invalid_exact_evm_payload_signature"
    if signer != auth["from"]:
        return "invalid_exact_evm_payload_signature"
    if auth["nonce"] in used_nonces:
        return "invalid_transaction_state"
    return None


def settle(payload: dict) -> dict:
    """Facilitator /settle stand-in: spend the nonce, broadcast nothing."""
    auth = payload["payload"]["authorization"]
    used_nonces.add(auth["nonce"])
    return {"success": True, "transaction": "", "network": NETWORK,
            "payer": auth["from"]}
