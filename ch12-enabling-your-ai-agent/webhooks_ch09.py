"""Chapter 9's Standard Webhooks signing code (from Chapter 6), reused."""

import importlib.util
from pathlib import Path

_path = (Path(__file__).resolve().parent.parent / "ch09-payment-standards"
         / "webhooks.py")
_spec = importlib.util.spec_from_file_location("webhooks_ch09", _path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

SECRET, sign, verify = _mod.SECRET, _mod.sign, _mod.verify
InvalidWebhook = _mod.InvalidWebhook
