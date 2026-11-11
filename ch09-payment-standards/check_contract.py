"""Validate every captured ACP message against the pinned OpenAPI schemas.

The schemas are ACP 2026-04-17, pinned in acp/ from
agentic-commerce-protocol/agentic-commerce-protocol at commit 7fdd78d.
In that version, CheckoutSessionWithOrder combines CheckoutSessionBase,
which sets additionalProperties to false, with an `order` member, so no
document can match it (the specification's own example fails too). The
completed session is checked as a CheckoutSession without `order`, and
`order` against the Order schema.

Usage: check_contract.py captures
"""

import json
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker

ACP = Path(__file__).parent / "acp"
CHECKS = [
    ("openapi.agentic_checkout.yaml", "CheckoutSessionCreateRequest",
     "acp-create-request.json"),
    ("openapi.agentic_checkout.yaml", "CheckoutSession",
     "acp-create-response.json"),
    ("openapi.agentic_checkout.yaml", "CheckoutSessionCompleteRequest",
     "acp-complete-request.json"),
    ("openapi.agentic_checkout.yaml", "CheckoutSession",
     "acp-complete-response.json#session"),
    ("openapi.agentic_checkout.yaml", "Order",
     "acp-complete-response.json#order"),
    ("openapi.delegate_payment.yaml", "DelegatePaymentRequest",
     "delegate-request.json"),
    ("openapi.delegate_payment.yaml", "DelegatePaymentResponse",
     "delegate-response.json"),
]


def main() -> int:
    captures = Path(sys.argv[1])
    failed = 0
    for doc_name, schema, capture in CHECKS:
        doc = yaml.safe_load((ACP / doc_name).read_text())
        validator = Draft202012Validator(
            {"components": doc["components"],
             "$ref": f"#/components/schemas/{schema}"},
            format_checker=FormatChecker())
        file, _, part = capture.partition("#")
        message = json.loads((captures / file).read_text())
        if part == "session":
            message = {k: v for k, v in message.items() if k != "order"}
        elif part == "order":
            message = message["order"]
        errors = list(validator.iter_errors(message))
        status = "[OK]  " if not errors else "[FAIL]"
        print(f"{status} {capture} is a valid {schema}")
        for e in errors:
            failed += 1
            print(f"       {list(e.path)}: {e.message[:120]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
