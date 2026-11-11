"""Create Harbor's realm for Chapter 10 in the local Keycloak.

Realm `harbor` gets Chapter 8's two order scopes, a client that stands for
Harbor's MCP server (its resource URL is the MCP endpoint, so Keycloak's
RFC 8707 support can put that URL in `aud`), a public MCP client that uses
the authorization code flow with PKCE, and Chapter 8's two customers.
Every password here is a placeholder for this local stack.

    uv run python setup_realm.py
"""

import sys
import time

import httpx

KC = "http://127.0.0.1:8080"
REALM = "harbor"
MCP = "http://127.0.0.1:8010/mcp"      # Harbor's MCP server, the resource
CALLBACK = "http://127.0.0.1:8090/callback"
ADMIN = ("admin", "not-a-real-password")
CUSTOMERS = [("rosa", "cus-001", "Rosa", "Delgado", "rosa@example.com"),
             ("sam", "cus-002", "Sam", "Okafor", "sam@example.com")]
SCOPES = {"orders:read": "Read your orders and the catalog",
          "orders:write": "Place and cancel orders for you"}


def admin_client():
    # Keycloak answers its realm page before the admin login is ready, so
    # retry the token request for up to a minute instead of racing it.
    for attempt in range(60):
        try:
            token = httpx.post(
                f"{KC}/realms/master/protocol/openid-connect/token",
                data={"grant_type": "password", "client_id": "admin-cli",
                      "username": ADMIN[0], "password": ADMIN[1]})
            token.raise_for_status()
            break
        except httpx.HTTPError:
            if attempt == 59:
                raise
            time.sleep(1)
    return httpx.Client(
        base_url=f"{KC}/admin/realms",
        headers={"Authorization": f"Bearer {token.json()['access_token']}"})


def ok(resp):
    if resp.status_code >= 400:
        sys.exit(f"{resp.request.method} {resp.request.url}: "
                 f"{resp.status_code} {resp.text}")
    return resp


def main():
    kc = admin_client()
    if kc.get(f"/{REALM}").status_code == 200:
        ok(kc.delete(f"/{REALM}"))
    ok(kc.post("", json={"realm": REALM, "enabled": True,
                         "accessTokenLifespan": 300,
                         "defaultDefaultClientScopes": []}))
    profile = ok(kc.get(f"/{REALM}/users/profile")).json()
    profile["attributes"].append({
        "name": "customer_id", "displayName": "Harbor customer ID",
        "permissions": {"view": ["admin"], "edit": ["admin"]},
        "validations": {"pattern": {"pattern": "^cus-[0-9]{3}$"}}})
    ok(kc.put(f"/{REALM}/users/profile", json=profile))

    for name, text in SCOPES.items():
        ok(kc.post(f"/{REALM}/client-scopes", json={
            "name": name, "description": text, "protocol": "openid-connect",
            "attributes": {"include.in.token.scope": "true",
                           "display.on.consent.screen": "true",
                           "consent.screen.text": text}}))
    scopes = {s["name"]: s["id"]
              for s in ok(kc.get(f"/{REALM}/client-scopes")).json()}

    clients = [
        # The MCP server as a resource server: no flows of its own, only a
        # resource URL that Keycloak matches against the `resource` parameter.
        {"clientId": "harbor-mcp", "publicClient": False,
         "bearerOnly": False, "standardFlowEnabled": False,
         "directAccessGrantsEnabled": False,
         "attributes": {"resource_url": MCP}},
        {"clientId": "harbor-mcp-client", "publicClient": True,
         "standardFlowEnabled": True, "directAccessGrantsEnabled": False,
         "consentRequired": True, "redirectUris": [CALLBACK],
         "attributes": {"pkce.code.challenge.method": "S256"},
         "protocolMappers": [
             {"name": "audience-harbor-mcp", "protocol": "openid-connect",
              "protocolMapper": "oidc-audience-mapper",
              "config": {"included.client.audience": "harbor-mcp",
                         "access.token.claim": "true",
                         "id.token.claim": "false"}},
             {"name": "customer-id", "protocol": "openid-connect",
              "protocolMapper": "oidc-usermodel-attribute-mapper",
              "config": {"user.attribute": "customer_id",
                         "claim.name": "customer_id",
                         "jsonType.label": "String",
                         "access.token.claim": "true",
                         "id.token.claim": "false",
                         "userinfo.token.claim": "false"}}]},
    ]
    ids = {}
    for body in clients:
        body.setdefault("attributes", {})[
            "access.token.header.type.rfc9068"] = "true"
        ok(kc.post(f"/{REALM}/clients", json=body))
        ids[body["clientId"]] = ok(kc.get(
            f"/{REALM}/clients",
            params={"clientId": body["clientId"]})).json()[0]["id"]
    for client in ids:
        for kind in ("default", "optional"):
            path = f"/{REALM}/clients/{ids[client]}/{kind}-client-scopes"
            for s in ok(kc.get(path)).json():
                ok(kc.delete(f"{path}/{s['id']}"))
    for kind, names in (("default", ["basic"]),
                        ("optional", list(SCOPES))):
        for name in names:
            ok(kc.put(f"/{REALM}/clients/{ids['harbor-mcp-client']}/"
                      f"{kind}-client-scopes/{scopes[name]}"))

    for username, customer, first, last, email in CUSTOMERS:
        ok(kc.post(f"/{REALM}/users", json={
            "username": username, "enabled": True, "email": email,
            "emailVerified": True, "firstName": first, "lastName": last,
            "attributes": {"customer_id": [customer]},
            "credentials": [{"type": "password",
                             "value": "not-a-real-password",
                             "temporary": False}]}))
    print(f"realm {REALM}: scopes {', '.join(SCOPES)}; clients "
          f"{', '.join(ids)}; customers "
          f"{', '.join(c[0] for c in CUSTOMERS)}")


if __name__ == "__main__":
    main()
