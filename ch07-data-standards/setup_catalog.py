"""Create Harbor's warehouse bucket and Polaris catalog, plus a
read-only principal for the analytics agent. Run with
`--grant harbor.order_lines` after the table exists to let that
principal read that one table.

Everything here goes through REST: the object store's S3 API (through
PyArrow) and Polaris's management API. The credentials are placeholders
for the local stack in docker-compose.yml. Prints the agent principal's
client ID and secret to agent-credentials.json (git-ignored).
"""
import json
from pathlib import Path

import httpx
from pyarrow import fs

POLARIS = "http://127.0.0.1:8181/api"
ROOT = ("root", "not-a-real-secret")
REALM = {"Polaris-Realm": "POLARIS"}


def token(client_id, secret):
    r = httpx.post(f"{POLARIS}/catalog/v1/oauth/tokens", headers=REALM,
                   data={"grant_type": "client_credentials",
                         "client_id": client_id, "client_secret": secret,
                         "scope": "PRINCIPAL_ROLE:ALL"})
    r.raise_for_status()
    return r.json()["access_token"]


def call(method, path, tok, body=None, ok=(200, 201, 204, 409)):
    r = httpx.request(method, f"{POLARIS}/management/v1{path}", json=body,
                      headers={**REALM, "Authorization": f"Bearer {tok}"})
    if r.status_code not in ok:
        raise SystemExit(f"{method} {path}: {r.status_code} {r.text}")
    return r


def main():
    s3 = fs.S3FileSystem(access_key="harbor-admin",
                         secret_key="not-a-real-secret",
                         endpoint_override="http://127.0.0.1:9000",
                         scheme="http", region="us-east-1",
                         allow_bucket_creation=True)
    if s3.get_file_info("harbor-lake").type == fs.FileType.NotFound:
        s3.create_dir("harbor-lake")
    tok = token(*ROOT)
    call("POST", "/catalogs", tok, {"catalog": {
        "name": "harbor", "type": "INTERNAL",
        "properties": {"default-base-location": "s3://harbor-lake"},
        "storageConfigInfo": {
            "storageType": "S3",
            "allowedLocations": ["s3://harbor-lake"],
            "endpoint": "http://127.0.0.1:9000",
            "endpointInternal": "http://rustfs:9000",
            "stsEndpoint": "http://rustfs:9000",
            "pathStyleAccess": True,
            "region": "us-east-1"}}})
    call("PUT", "/catalogs/harbor/catalog-roles/catalog_admin/grants",
         tok, {"grant": {"type": "catalog",
                         "privilege": "CATALOG_MANAGE_CONTENT"}})
    # A reader role that can list what exists and nothing else yet;
    # grant_table() adds read access to one table once it exists.
    call("POST", "/catalogs/harbor/catalog-roles", tok,
         {"catalogRole": {"name": "order_history_reader"}})
    for priv in ("NAMESPACE_LIST", "TABLE_LIST"):
        call("PUT", "/catalogs/harbor/catalog-roles/order_history_reader"
             "/grants", tok, {"grant": {"type": "catalog",
                                        "privilege": priv}})
    call("POST", "/principal-roles", tok,
         {"principalRole": {"name": "analytics_agent_role"}})
    call("PUT", "/principal-roles/analytics_agent_role/catalog-roles/harbor",
         tok, {"catalogRole": {"name": "order_history_reader"}})
    creds_file = Path("agent-credentials.json")
    r = call("POST", "/principals", tok,
             {"principal": {"name": "analytics-agent"}})
    if r.status_code == 201:
        c = r.json()["credentials"]
        creds_file.write_text(json.dumps(
            {"client_id": c["clientId"], "client_secret": c["clientSecret"]}))
    call("PUT", "/principals/analytics-agent/principal-roles", tok,
         {"principalRole": {"name": "analytics_agent_role"}})
    print("catalog harbor on s3://harbor-lake; principal analytics-agent "
          "holds order_history_reader")


def grant_table(namespace, table):
    """TABLE_READ_DATA on one table only, not on the whole catalog."""
    call("PUT", "/catalogs/harbor/catalog-roles/order_history_reader/grants",
         token(*ROOT), {"grant": {"type": "table", "namespace": [namespace],
                                  "tableName": table,
                                  "privilege": "TABLE_READ_DATA"}})
    print(f"order_history_reader granted TABLE_READ_DATA on "
          f"{namespace}.{table}")


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["--grant"]:
        grant_table(*sys.argv[2].split("."))
    else:
        main()
