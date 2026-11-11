"""Check a captured webhook delivery against asyncapi.yaml.

    uv run python check_contract.py captures/delivery.http

Validates the delivery's webhook headers and JSON body against the
orderShipped message's headers and payload schemas.
"""

import json
import sys

import jsonschema
import yaml

spec = yaml.safe_load(open("asyncapi.yaml"))
message = spec["components"]["messages"]["orderShipped"]
head, _, body = open(sys.argv[1]).read().partition("\n\n")
headers = dict(line.split(": ", 1) for line in head.splitlines()[1:])
headers.pop("content-type")
checker = jsonschema.FormatChecker()
jsonschema.validate(headers, message["headers"], format_checker=checker)
jsonschema.validate(json.loads(body), message["payload"],
                    format_checker=checker)
print(f"{sys.argv[1]} matches {message['name']} in AsyncAPI "
      f"{spec['asyncapi']}")
