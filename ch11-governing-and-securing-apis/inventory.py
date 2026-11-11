"""Compare what Harbor serves, what its contract says, and what the gateway
routes, and report the gaps: shadow routes (served, in no contract) and
deprecated routes still taking traffic (zombies once their Sunset passes).

    uv run python inventory.py [gateway access log]
"""

import json
import re
import sys
from pathlib import Path

import yaml

here = Path(__file__).parent
sys.path.insert(0, str(here.parent / "ch03-rest-apis"))
import harbor_api  # noqa: E402

contract = json.loads((here.parent / "ch03-rest-apis" /
                       "openapi.json").read_text())
documented = set(contract["paths"])
served = {r.path for r in harbor_api.app.routes}
gateway = yaml.safe_load((here / "apisix" / "apisix.yaml.tmpl").read_text())
log = Path(sys.argv[1]) if len(sys.argv) > 1 else \
    here / "captures" / "gateway" / "access.jsonl"
hits = [json.loads(line) for line in log.read_text().splitlines()]

print(f"contract: {len(documented)} paths; backend serves "
      f"{len(served)}; gateway has {len(gateway['routes'])} routes")
print("shadow (served, not in the contract):")
print("  " + (", ".join(sorted(served - documented)) or "none"))
for route in gateway["routes"]:
    headers = (route.get("plugins", {}).get("response-rewrite", {})
               .get("headers", {}).get("set", {}))
    if "Sunset" in headers:
        prefix = re.sub(r"\*$", "", route["uri"])
        used = [h for h in hits if h.get("uri", "").startswith(prefix)]
        callers = sorted({h.get("consumer", "-") for h in used})
        print(f"deprecated: {route['uri']}, Sunset {headers['Sunset']}")
        print(f"  still called: {len(used)} times, by "
              f"{', '.join(callers) or 'nobody'}")
