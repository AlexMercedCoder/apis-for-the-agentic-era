"""Open Harbor's Iceberg REST catalog with PyIceberg.

By default the client goes through logproxy.py on port 8180 so every
exchange is logged; pass direct=True to talk to Polaris on 8181. The
root credential is a placeholder for the local stack.
"""
import json
from pathlib import Path

from pyiceberg.catalog.rest import RestCatalog

ROOT = "root:not-a-real-secret"


def agent_credential():
    c = json.loads(Path(__file__).with_name(
        "agent-credentials.json").read_text())
    return f"{c['client_id']}:{c['client_secret']}"


def open_catalog(credential=ROOT, direct=False, name="harbor"):
    port = 8181 if direct else 8180
    return RestCatalog(name, **{
        "uri": f"http://127.0.0.1:{port}/api/catalog",
        "warehouse": "harbor",
        "credential": credential,
        "scope": "PRINCIPAL_ROLE:ALL",
        "header.X-Iceberg-Access-Delegation": "vended-credentials",
    })
