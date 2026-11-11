"""Pull the commit exchanges out of logproxy.py's log: every POST to a
table (a commit), written as commit-N.json with its status."""
import json
import sys
from pathlib import Path

log, out = Path(sys.argv[1]), Path(sys.argv[2])
n = 0
for line in log.read_text().splitlines():
    entry = json.loads(line)
    method, path = entry["request"].split(" ", 1)
    if method == "POST" and path.endswith("/tables/order_lines"):
        n += 1
        if entry["status"] == 200:  # keep the response's head only
            entry["response"] = {
                "metadata-location": entry["response"]["metadata-location"],
                "metadata": "..."}
        (out / f"commit-{n}.json").write_text(json.dumps(entry, indent=2))
        print(f"commit-{n}.json  {entry['status']}  {path}")
