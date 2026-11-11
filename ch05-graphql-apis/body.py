"""Print a GraphQL-over-HTTP request body for one operation in operations/.

    uv run python body.py OrderScreen '{"orderId": "ord-5002"}'
    uv run python body.py OrderScreen '{"orderId": "ord-5002"}' --id

With --id the body carries the trusted document's ID instead of the text.
"""

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

name, variables = sys.argv[1], json.loads(sys.argv[2])
text = (HERE / "operations" / f"{name}.graphql").read_text()
body = {"operationName": name, "variables": variables}
if "--id" in sys.argv:
    body["documentId"] = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
else:
    body["query"] = text
print(json.dumps(body))
