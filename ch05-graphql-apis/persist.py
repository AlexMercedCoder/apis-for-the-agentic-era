"""Write trusted-documents.json: each operation in operations/ by its hash.

The file is the server's allow-list in --trusted-only mode, and a client
sends {"documentId": "sha256:..."} instead of the query text.
"""

import hashlib
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> None:
    docs = {}
    for path in sorted((HERE / "operations").glob("*.graphql")):
        text = path.read_text()
        docs["sha256:" + hashlib.sha256(text.encode()).hexdigest()] = text
    out = HERE / "trusted-documents.json"
    out.write_text(json.dumps(docs, indent=2) + "\n")
    for doc_id, text in docs.items():
        name = re.search(r"(?:query|mutation|subscription)\s+(\w+)",
                         text).group(1)
        print(f"{doc_id[:19]}...  {name}")


if __name__ == "__main__":
    main()
