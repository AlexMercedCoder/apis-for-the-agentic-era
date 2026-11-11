"""Chapter 9's payment-provider stand-in, unchanged, posting to this chapter.

One setting moves: its webhooks go to Harbor's refunds API on 8123
instead of Chapter 9's payment service on 8009. Every key is a placeholder.

    uv run python payment_provider.py                # 127.0.0.1:8091
"""

import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "ch09-payment-standards"))
import provider as ch09  # noqa: E402

ch09.WEBHOOK_URL = "http://127.0.0.1:8123/hooks/payments"

if __name__ == "__main__":
    uvicorn.run(ch09.app, host="127.0.0.1", port=8091, log_level="warning")
