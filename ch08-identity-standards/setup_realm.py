"""Create Harbor's realm in the local Keycloak through its admin REST API.

Realm `harbor` gets three scopes for Harbor's REST API, three clients, two
customers, and the permission that lets the assistant act for a customer.
Every password and client secret here is a placeholder for this local
stack, never a value to reuse.

    uv run python setup_realm.py
"""

import sys
import time

import httpx

KC = "http://127.0.0.1:8080"
REALM = "harbor"
API = "http://127.0.0.1:8008"          # Harbor's resource identifier
CALLBACK = "http://127.0.0.1:8090/callback"
ADMIN = ("admin", "not-a-real-password")
SECRET = "not-a-real-secret"
CUSTOMERS = [("rosa", "cus-001", "Rosa", "Delgado", "rosa@example.com"),
             ("sam", "cus-002", "Sam", "Okafor", "sam@example.com")]
API_SCOPES = {
    "orders:read": "Read your orders",
    "orders:write": "Place and cancel orders for you",
    "orders:read:all": "Read any order (fulfillment)",
}


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


def scope_body(name, description):
    mappers = [{
        "name": "harbor-api-audience",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-audience-mapper",
        "config": {"included.custom.audience": API,
                   "access.token.claim": "true",
                   "id.token.claim": "false"}}]
    if name != "orders:read:all":
        mappers.append({
            "name": "customer-id",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-usermodel-attribute-mapper",
            "config": {"user.attribute": "customer_id",
                       "claim.name": "customer_id",
                       "jsonType.label": "String",
                       "access.token.claim": "true",
                       "id.token.claim": "false",
                       "userinfo.token.claim": "false"}})
    return {"name": name, "description": description,
            "protocol": "openid-connect",
            "attributes": {"include.in.token.scope": "true",
                           "display.on.consent.screen": "true",
                           "consent.screen.text": description},
            "protocolMappers": mappers}


def main():
    kc = admin_client()
    if kc.get(f"/{REALM}").status_code == 200:
        ok(kc.delete(f"/{REALM}"))
    ok(kc.post("", json={
        "realm": REALM, "enabled": True,
        "accessTokenLifespan": 300,
        "revokeRefreshToken": True,     # rotate: each refresh token works once
        "refreshTokenMaxReuse": 0,
        "adminPermissionsEnabled": True,
        "defaultDefaultClientScopes": [],
    }))

    # customer_id is a user attribute only admins may set.
    profile = ok(kc.get(f"/{REALM}/users/profile")).json()
    profile["attributes"].append({
        "name": "customer_id", "displayName": "Harbor customer ID",
        "permissions": {"view": ["admin"], "edit": ["admin"]},
        "validations": {"pattern": {"pattern": "^cus-[0-9]{3}$"}}})
    ok(kc.put(f"/{REALM}/users/profile", json=profile))

    for name, description in API_SCOPES.items():
        ok(kc.post(f"/{REALM}/client-scopes",
                   json=scope_body(name, description)))
    scopes = {s["name"]: s["id"]
              for s in ok(kc.get(f"/{REALM}/client-scopes")).json()}

    clients = [
        {"clientId": "harbor-storefront", "publicClient": True,
         "standardFlowEnabled": True, "directAccessGrantsEnabled": False,
         "consentRequired": True, "redirectUris": [CALLBACK],
         "attributes": {"pkce.code.challenge.method": "S256",
                        "dpop.bound.access.tokens": "false"}},
        {"clientId": "harbor-fulfillment", "publicClient": False,
         "secret": SECRET, "standardFlowEnabled": False,
         "directAccessGrantsEnabled": False,
         "serviceAccountsEnabled": True},
        {"clientId": "harbor-assistant", "publicClient": False,
         "secret": SECRET, "standardFlowEnabled": False,
         "directAccessGrantsEnabled": False,
         "serviceAccountsEnabled": True,
         "attributes": {"standard.token.exchange.enabled": "true",
                        # every token for the assistant must be DPoP-bound
                        "dpop.bound.access.tokens": "true"}},
    ]
    ids = {}
    for body in clients:
        # Access tokens typed at+jwt (RFC 9068), so an API can tell them
        # from ID tokens; Keycloak's default header type is plain JWT.
        body.setdefault("attributes", {})[
            "access.token.header.type.rfc9068"] = "true"
        ok(kc.post(f"/{REALM}/clients", json=body))
        found = ok(kc.get(f"/{REALM}/clients",
                          params={"clientId": body["clientId"]})).json()
        ids[body["clientId"]] = found[0]["id"]

    for client in ids:   # start each client with no scopes at all
        for kind in ("default", "optional"):
            path = f"/{REALM}/clients/{ids[client]}/{kind}-client-scopes"
            for s in ok(kc.get(path)).json():
                ok(kc.delete(f"{path}/{s['id']}"))

    def attach(client, kind, names):
        for name in names:
            ok(kc.put(f"/{REALM}/clients/{ids[client]}/{kind}-client-scopes/"
                      f"{scopes[name]}"))

    attach("harbor-storefront", "default", ["basic", "profile"])
    attach("harbor-storefront", "optional",
           ["orders:read", "orders:write", "delegation:client"])
    attach("harbor-fulfillment", "default", ["basic", "orders:read:all"])
    attach("harbor-assistant", "default", ["basic"])
    attach("harbor-assistant", "optional", ["orders:read"])

    for username, customer, first, last, email in CUSTOMERS:
        ok(kc.post(f"/{REALM}/users", json={
            "username": username, "enabled": True, "email": email,
            "emailVerified": True, "firstName": first, "lastName": last,
            "attributes": {"customer_id": [customer]},
            "credentials": [{"type": "password",
                             "value": "not-a-real-password",
                             "temporary": False}]}))

    # Fine-grained admin permission: harbor-assistant may be a delegate.
    perms = ok(kc.get(f"/{REALM}/clients",
                      params={"clientId": "admin-permissions"})).json()[0]
    authz = f"/{REALM}/clients/{perms['id']}/authz/resource-server"
    ok(kc.post(f"{authz}/policy/client", json={
        "name": "is-harbor-assistant", "logic": "POSITIVE",
        "clients": [ids["harbor-assistant"]]}))
    ok(kc.post(f"{authz}/permission/scope", json={
        "name": "assistant-may-delegate", "resourceType": "Users",
        "scopes": ["delegate"], "policies": ["is-harbor-assistant"],
        "decisionStrategy": "UNANIMOUS"}))
    print(f"realm {REALM}: scopes {', '.join(API_SCOPES)}; clients "
          f"{', '.join(ids)}; customers "
          f"{', '.join(c[0] for c in CUSTOMERS)}")


if __name__ == "__main__":
    main()
