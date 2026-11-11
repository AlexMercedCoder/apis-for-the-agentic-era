"""Print one operation from the pinned openapi.json as YAML.

  uv run python show_operation.py post /orders

OpenAPI documents can be written in JSON or YAML; Harbor pins JSON because
FastAPI generates it, and this script prints YAML because it is shorter to
read. Responses that share a schema are listed once with all their codes.
"""

import json
import sys
from pathlib import Path

import yaml

SPEC = json.loads(Path(__file__).with_name("openapi.json").read_text())


def main(method, path):
    op = SPEC["paths"][path][method]
    print(yaml.safe_dump({path: {method: op}}, sort_keys=False, width=66),
          end="")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
