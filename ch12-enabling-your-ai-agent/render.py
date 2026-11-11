"""Render apisix/apisix.yaml from the template and the issuer's key."""

import json
from pathlib import Path

import issuer

here = Path(__file__).parent / "apisix"
text = (here / "apisix.yaml.tmpl").read_text()
pem = json.dumps(issuer.public_pem())[1:-1]       # one line, \n escaped
(here / "apisix.yaml").write_text(text.replace("{PEM}", pem))
