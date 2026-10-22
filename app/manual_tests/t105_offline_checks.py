"""T-105 offline checks: token validation, attribution, OBO and the auth gate.

Run: python -m app.manual_tests.t105_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
Tokens are signed with a throwaway RSA key and validated by the real validator
against a fake key set, so no Entra tenant or network is needed. This checks the
logic, not your tenant setup: the live steps are in MANUAL_TESTING.md.
"""

import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import app.api.security as security
from app.api.deps import get_connector_factory, get_fabric_client, get_store
from app.api.main import app
from app.api.obo import ConsentRequired, OboFailed, get_msal_app, obo_token_provider
from app.api.security import (
    AuthConfigError,
    AuthError,
    AuthUpstreamError,
    TokenValidator,
    get_validator,
)
from app.auth import FABRIC_SCOPE, STORAGE_SCOPE
from app.connectors.fabric_api import FabricApiClient
from app.connectors.fabric_lakehouse import FabricLakehouseConnector
from app.store.file_store import FileRunStore
from app.workflow.run import RunScope

TENANT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_TENANT = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
API_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
OID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def check(name: str, condition: bool) -> None:
    print(("PASS " if condition else "FAIL ") + name)
    if not condition:
        raise SystemExit(1)


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc as e:
        return e
    return None


class FakeJwks:
    def get_signing_key_from_jwt(self, token):
        return SimpleNamespace(key=KEY.public_key())


class BrokenJwks:
    def get_signing_key_from_jwt(self, token):
        raise jwt.PyJWKClientConnectionError("unreachable")


def make_token(key=KEY, **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "aud": API_ID,
        "tid": TENANT,
        "oid": OID,
        "preferred_username": "alice@contoso.com",
        "name": "Alice Example",
        "scp": "access_as_user",
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


def is_envelope(r, status, code):
    body = r.json()
    return (
        r.status_code == status
        and set(body) == {"error"}
        and body["error"]["code"] == code
        and bool(body["error"]["message"])
    )


class FakeMsal:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def acquire_token_on_behalf_of(self, user_assertion, scopes):
        self.calls.append((user_assertion, scopes))
        return self.result


def main() -> None:
    validator = TokenValidator(TENANT, API_ID, jwks_client=FakeJwks())

    # --- Token validation ---
    principal = validator.validate(make_token())
    check(
        "valid v2 token gives the principal",
        (principal.oid, principal.upn, principal.name)
        == (OID, "alice@contoso.com", "Alice Example"),
    )
    v1 = make_token(
        iss=f"https://sts.windows.net/{TENANT}/",
        aud=f"api://{API_ID}",
        preferred_username=None,
        upn="alice@contoso.com",
    )
    check("valid v1 token (upn claim, api:// audience) is accepted", validator.validate(v1).upn == "alice@contoso.com")
    no_name = validator.validate(make_token(name=None))
    check("missing name falls back to the upn", no_name.name == "alice@contoso.com")

    def code_of(token):
        err = raises(AuthError, validator.validate, token)
        return err.code if err else None

    check("expired token is TOKEN_EXPIRED", code_of(make_token(exp=int(time.time()) - 600)) == "TOKEN_EXPIRED")
    check("wrong audience is UNAUTHENTICATED", code_of(make_token(aud="someone-else")) == "UNAUTHENTICATED")
    check(
        "wrong issuer is UNAUTHENTICATED",
        code_of(make_token(iss=f"https://login.microsoftonline.com/{OTHER_TENANT}/v2.0")) == "UNAUTHENTICATED",
    )
    check("wrong tenant (tid) is UNAUTHENTICATED", code_of(make_token(tid=OTHER_TENANT)) == "UNAUTHENTICATED")
    check("missing access_as_user scope is UNAUTHENTICATED", code_of(make_token(scp="User.Read")) == "UNAUTHENTICATED")
    check(
        "app-only token (roles, no scp) is refused",
        code_of(make_token(scp=None, roles=["Some.Role"])) == "UNAUTHENTICATED",
    )
    check("token signed with another key is refused", code_of(make_token(key=OTHER_KEY)) == "UNAUTHENTICATED")
    check(
        "HS256 forgery is refused",
        code_of(jwt.encode({"aud": API_ID, "exp": int(time.time()) + 600}, "secret-secret-secret-secret-1234", algorithm="HS256")) == "UNAUTHENTICATED",
    )
    check(
        "alg=none token is refused",
        code_of(jwt.encode({"aud": API_ID, "exp": int(time.time()) + 600}, None, algorithm="none")) == "UNAUTHENTICATED",
    )
    check("garbage token is refused", code_of("not.a.jwt") == "UNAUTHENTICATED")
    check("token with no oid is refused", code_of(make_token(oid=None)) == "UNAUTHENTICATED")
    check(
        "token with no user name is refused",
        code_of(make_token(preferred_username=None)) == "UNAUTHENTICATED",
    )
    broken = TokenValidator(TENANT, API_ID, jwks_client=BrokenJwks())
    check(
        "unreachable key set is an upstream error, not a 401",
        raises(AuthUpstreamError, broken.validate, make_token()) is not None,
    )

    # --- OBO ---
    msal_ok = FakeMsal({"access_token": "obo-token"})
    check(
        "OBO provider returns the exchanged token",
        obo_token_provider(msal_ok, "user-token", FABRIC_SCOPE)() == "obo-token"
        and msal_ok.calls == [("user-token", [FABRIC_SCOPE])],
    )
    for error in ("interaction_required", "consent_required", "invalid_grant"):
        check(
            f"OBO error {error} means consent is required",
            raises(ConsentRequired, obo_token_provider(FakeMsal({"error": error, "error_description": "secret detail"}), "t", FABRIC_SCOPE)) is not None,
        )
    err = raises(OboFailed, obo_token_provider(FakeMsal({"error": "server_error", "error_description": "secret detail"}), "t", FABRIC_SCOPE))
    check("other OBO errors are OboFailed with no detail leak", err is not None and "secret" not in str(err))

    client = get_fabric_client("user-token", msal_ok)
    check("Fabric client calls Fabric with the OBO token", client._token() == "obo-token" and msal_ok.calls[-1] == ("user-token", [FABRIC_SCOPE]))
    scope = RunScope(workspace_id="w", lakehouse_id="l", lakehouse_name="Lake")
    connector = get_connector_factory("user-token", msal_ok)(scope)
    check(
        "Lakehouse connector reads OneLake with the OBO token",
        connector._get_headers()["Authorization"] == "Bearer obo-token"
        and msal_ok.calls[-1] == ("user-token", [STORAGE_SCOPE]),
    )
    check(
        "connector without a provider still uses the local credential path",
        FabricLakehouseConnector("w", "l", "Lake")._token_provider is None,
    )

    # --- API ---
    with tempfile.TemporaryDirectory() as tmp:
        store = FileRunStore(Path(tmp) / "runs")
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_validator] = lambda: validator
        client = TestClient(app, raise_server_exceptions=False)

        def auth(token=None):
            return {"Authorization": f"Bearer {token or make_token()}"}

        r = client.get("/runs")
        check("no Authorization header is 401 UNAUTHENTICATED", is_envelope(r, 401, "UNAUTHENTICATED"))
        check("401 carries WWW-Authenticate", r.headers.get("www-authenticate") == "Bearer")
        check("non-Bearer scheme is 401", is_envelope(client.get("/runs", headers={"Authorization": "Basic abc"}), 401, "UNAUTHENTICATED"))
        check("empty bearer token is 401", is_envelope(client.get("/runs", headers={"Authorization": "Bearer "}), 401, "UNAUTHENTICATED"))
        r = client.get("/runs", headers=auth(make_token(exp=int(time.time()) - 600)))
        check("expired token is 401 TOKEN_EXPIRED", is_envelope(r, 401, "TOKEN_EXPIRED"))
        r = client.get("/runs", headers=auth(make_token(aud="other")))
        check("wrong audience is 401 UNAUTHENTICATED", is_envelope(r, 401, "UNAUTHENTICATED"))
        check("401 never echoes the token", make_token() not in client.get("/runs", headers=auth(make_token(aud="other"))).text)
        check("health stays open", client.get("/health").status_code == 200)
        check("valid token is accepted", client.get("/runs", headers=auth()).status_code == 200)

        # Every endpoint except /health is protected
        paths = [
            (method.upper(), path)
            for path, item in app.openapi()["paths"].items()
            for method in item
            if path != "/health"
        ]
        unprotected = []
        for method, path in paths:
            url = path.replace("{run_id}", "x").replace("{workspace_id}", "x")
            response = client.request(method, url, json={})
            if response.status_code != 401:
                unprotected.append((method, path, response.status_code))
        check(f"all {len(paths)} non-health endpoints require a token", unprotected == [])

        # Attribution
        r = client.post("/runs", json={"prompt": "Total sales by month"}, headers=auth())
        run = r.json()
        check(
            "run records created_by from the token",
            r.status_code == 201
            and run["created_by"] == {"oid": OID, "upn": "alice@contoso.com", "name": "Alice Example"},
        )
        check("first history actor is the UPN", run["history"][0]["actor"] == "alice@contoso.com")
        check(
            "created_by shows on GET and in the list",
            client.get(f"/runs/{run['run_id']}", headers=auth()).json()["created_by"]["upn"] == "alice@contoso.com"
            and client.get("/runs", headers=auth()).json()["items"][0]["created_by"]["upn"] == "alice@contoso.com",
        )
        cli_run = store.create("Created by the CLI")
        check(
            "store-created runs (CLI) keep working with actor system",
            cli_run.created_by is None and cli_run.history[0].actor == "system",
        )

        # Consent required surfaces as 403 CONSENT_REQUIRED
        consent = FakeMsal({"error": "interaction_required", "error_description": "secret detail"})
        app.dependency_overrides[get_msal_app] = lambda: consent
        r = client.get("/fabric/workspaces", headers=auth())
        check("missing consent is 403 CONSENT_REQUIRED", is_envelope(r, 403, "CONSENT_REQUIRED"))
        check("consent error hides the Entra detail", "secret detail" not in r.text)
        app.dependency_overrides[get_msal_app] = lambda: FakeMsal({"error": "server_error"})
        check("OBO failure is 502 UPSTREAM_ERROR", is_envelope(client.get("/fabric/workspaces", headers=auth()), 502, "UPSTREAM_ERROR"))

        # User with no workspaces gets an empty list
        class EmptyHttp:
            def __call__(self, url, headers=None, params=None, timeout=None):
                self.headers = headers
                return SimpleNamespace(status_code=200, headers={}, json=lambda: {"value": []})

        http = EmptyHttp()
        msal_user = FakeMsal({"access_token": "obo-token"})
        app.dependency_overrides[get_msal_app] = lambda: msal_user
        app.dependency_overrides[get_fabric_client] = lambda: FabricApiClient(
            token_provider=obo_token_provider(msal_user, "user-token", FABRIC_SCOPE),
            http_get=http,
        )
        r = client.get("/fabric/workspaces", headers=auth())
        check("a user with no workspaces gets 200 []", r.status_code == 200 and r.json() == [])
        check("Fabric was called with the OBO token, not the user's token", http.headers["Authorization"] == "Bearer obo-token")
        app.dependency_overrides.clear()

        # --- Fails closed when not configured (no bypass) ---
        saved = (security.ENTRA_TENANT_ID, security.ENTRA_API_CLIENT_ID)
        security.ENTRA_TENANT_ID = security.ENTRA_API_CLIENT_ID = None
        security._configured_validator.cache_clear()
        app.dependency_overrides[get_store] = lambda: store
        try:
            r = client.get("/runs", headers=auth())
            check("unconfigured API is 500 CONFIG_ERROR, not open", is_envelope(r, 500, "CONFIG_ERROR"))
            check("config error names the missing settings", "ENTRA_TENANT_ID" in r.json()["error"]["message"])
            check("health is still open when unconfigured", client.get("/health").status_code == 200)
            check("validator refuses to build", raises(AuthConfigError, get_validator) is not None)
        finally:
            security.ENTRA_TENANT_ID, security.ENTRA_API_CLIENT_ID = saved
            security._configured_validator.cache_clear()
            app.dependency_overrides.clear()

    print("All checks passed.")


if __name__ == "__main__":
    main()
