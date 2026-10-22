"""T-103 offline checks: Fabric client, scope binding and the scope endpoints.

Run: python -m app.manual_tests.t103_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
Fabric is replaced by a fake HTTP layer, so no sign-in or network is needed.
This checks the logic, not live access: the live steps are in MANUAL_TESTING.md.
"""

import tempfile
from pathlib import Path

import requests
from fastapi.testclient import TestClient

from app.api.deps import get_connector_factory, get_fabric_client, get_store
from app.api.main import app
from app.api.security import AuthContext, Principal, authenticate
from app.connectors.fabric_api import (
    FABRIC_API,
    FabricApiClient,
    FabricForbidden,
    FabricNotFound,
    FabricRateLimited,
    FabricTimeout,
    FabricUpstreamError,
)
from app.connectors.fabric_lakehouse import FabricLakehouseConnector
from app.connectors.fabric_sql_connector import FabricSQLConnector
from app.store.file_store import FileRunStore
from app.workflow.run import ArtifactRef, RunScope, SqlEndpoint
from app.workflow.states import RunState

WS = "11111111-1111-4111-8111-111111111111"
LH = "22222222-2222-4222-8222-222222222222"
LH_NO_SQL = "33333333-3333-4333-8333-333333333333"
RANDOM = "99999999-9999-4999-8999-999999999999"


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


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._body


class FakeFabric:
    """A fake `requests.get` that serves one canned reply per URL, or a queue."""

    def __init__(self):
        self.routes: dict[str, list] = {}
        self.calls: list[tuple[str, dict | None]] = []

    def set(self, url: str, *replies):
        self.routes[url] = list(replies)

    def __call__(self, url, headers=None, params=None, timeout=None):
        self.calls.append((url, params))
        replies = self.routes.get(url)
        if replies is None:
            return FakeResponse(404)
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeLakehouseConnector:
    def __init__(self, schemas):
        self.schemas = schemas

    def list_schemas(self):
        return [{"name": n} for n in self.schemas]


WORKSPACES_URL = f"{FABRIC_API}/workspaces"
LAKEHOUSES_URL = f"{FABRIC_API}/workspaces/{WS}/lakehouses"
LAKEHOUSES = {
    "value": [
        {
            "id": LH,
            "displayName": "SalesLakehouse",
            "workspaceId": WS,
            "properties": {
                "sqlEndpointProperties": {
                    "connectionString": "abc.datawarehouse.fabric.microsoft.com",
                    "provisioningStatus": "Success",
                }
            },
        },
        {"id": LH_NO_SQL, "displayName": "NewLakehouse", "workspaceId": WS, "properties": {}},
    ]
}


def fake_authenticate() -> AuthContext:
    """Stands in for token validation; T-105's own checks cover the real thing."""
    return AuthContext(
        principal=Principal(oid="oid-1", upn="tester@contoso.com", name="Tester"),
        token="test-token",
    )


def make_client(fake, sleeps=None):
    return FabricApiClient(
        token_provider=lambda: "token",
        http_get=fake,
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
    )


def is_envelope(r, status, code):
    body = r.json()
    return (
        r.status_code == status
        and set(body) == {"error"}
        and body["error"]["code"] == code
        and bool(body["error"]["message"])
    )


def main() -> None:
    # --- Fabric client: paging, mapping ---
    fake = FakeFabric()
    fake.set(
        WORKSPACES_URL,
        FakeResponse(200, {"value": [{"id": "w1", "displayName": "One", "type": "Workspace"}], "continuationToken": "p2"}),
        FakeResponse(200, {"value": [{"id": "w2", "displayName": "Two", "type": "Personal"}]}),
    )
    workspaces = make_client(fake).list_workspaces()
    check("workspaces: all pages read", [w["id"] for w in workspaces] == ["w1", "w2"])
    check("workspaces: second page sends continuationToken", fake.calls[1][1] == {"continuationToken": "p2"})
    check("workspaces: fields mapped", workspaces[0] == {"id": "w1", "display_name": "One", "type": "Workspace"})

    fake.set(LAKEHOUSES_URL, FakeResponse(200, LAKEHOUSES))
    lakehouses = make_client(fake).list_lakehouses(WS)
    check(
        "lakehouses: SQL endpoint filled in",
        lakehouses[0]["sql_endpoint"]
        == {"server": "abc.datawarehouse.fabric.microsoft.com", "database": "SalesLakehouse"}
        and lakehouses[0]["workspace_id"] == WS,
    )
    check("lakehouses: no endpoint is null", lakehouses[1]["sql_endpoint"] is None)

    fake.set(WORKSPACES_URL, FakeResponse(200, {"value": [], "continuationToken": "same"}))
    check(
        "repeating page token is an upstream error",
        raises(FabricUpstreamError, make_client(fake).list_workspaces) is not None,
    )

    # --- Fabric client: errors and retries ---
    sleeps: list = []
    fake.set(
        WORKSPACES_URL,
        FakeResponse(429, headers={"Retry-After": "2"}),
        FakeResponse(429, headers={"Retry-After": "2"}),
        FakeResponse(200, {"value": []}),
    )
    check("429 then success returns data", make_client(fake, sleeps).list_workspaces() == [])
    check("429 waits for Retry-After", sleeps == [2, 2])

    sleeps = []
    fake.set(WORKSPACES_URL, FakeResponse(429, headers={"Retry-After": "7", "RequestId": "rid-1"}))
    err = raises(FabricRateLimited, make_client(fake, sleeps).list_workspaces)
    check("429 gives up after 3 retries", err is not None and sleeps == [7, 7, 7])
    check("rate limit carries Retry-After and request id", err.retry_after == 7 and err.request_id == "rid-1")

    fake.set(WORKSPACES_URL, FakeResponse(500, headers={"RequestId": "rid-2"}))
    err = raises(FabricUpstreamError, make_client(fake).list_workspaces)
    check("5xx is upstream error with request id", err is not None and err.request_id == "rid-2")
    fake.set(WORKSPACES_URL, FakeResponse(403))
    check("403 is forbidden", raises(FabricForbidden, make_client(fake).list_workspaces) is not None)
    fake.set(WORKSPACES_URL, FakeResponse(404))
    check("404 is not found", raises(FabricNotFound, make_client(fake).list_workspaces) is not None)
    fake.set(WORKSPACES_URL, requests.Timeout())
    check("timeout is FabricTimeout", raises(FabricTimeout, make_client(fake).list_workspaces) is not None)
    fake.set(WORKSPACES_URL, requests.ConnectionError("boom secret"))
    err = raises(FabricUpstreamError, make_client(fake).list_workspaces)
    check("connection error is upstream error, no detail leak", err is not None and "secret" not in str(err))

    # --- Connector factories ---
    scope = RunScope(
        workspace_id=WS,
        lakehouse_id=LH,
        lakehouse_name="SalesLakehouse",
        sql_endpoint=SqlEndpoint(server="abc.fabric.microsoft.com", database="SalesLakehouse"),
    )
    lh = FabricLakehouseConnector.for_scope(scope)
    check(
        "Lakehouse connector reads the scope",
        lh.lakehouse_path == f"{WS}/{LH}" and lh.catalog_name == "SalesLakehouse.Lakehouse",
    )
    check(
        "Lakehouse connector accepts a dict scope",
        FabricLakehouseConnector.for_scope(scope.model_dump()).lakehouse_path == f"{WS}/{LH}",
    )
    sql = FabricSQLConnector.for_scope(scope)
    check("SQL connector reads the scope", (sql.server, sql.database) == ("abc.fabric.microsoft.com", "SalesLakehouse"))
    no_sql = scope.model_copy(update={"sql_endpoint": None})
    check("SQL connector refuses a scope with no endpoint", raises(ValueError, FabricSQLConnector.for_scope, no_sql) is not None)

    # --- API ---
    with tempfile.TemporaryDirectory() as tmp:
        store = FileRunStore(Path(tmp) / "runs")
        api_fake = FakeFabric()
        api_fake.set(WORKSPACES_URL, FakeResponse(200, {"value": [{"id": WS, "displayName": "Sandbox", "type": "Workspace"}]}))
        api_fake.set(LAKEHOUSES_URL, FakeResponse(200, LAKEHOUSES))
        sleeps = []
        connector = FakeLakehouseConnector(["saleslt", "nexora_test"])
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[authenticate] = fake_authenticate
        app.dependency_overrides[get_fabric_client] = lambda: make_client(api_fake, sleeps)
        app.dependency_overrides[get_connector_factory] = lambda: (lambda scope: connector)
        client = TestClient(app, raise_server_exceptions=False)

        r = client.get("/fabric/workspaces")
        check("GET /fabric/workspaces", r.status_code == 200 and r.json() == [{"id": WS, "display_name": "Sandbox", "type": "Workspace"}])
        r = client.get(f"/fabric/workspaces/{WS}/lakehouses")
        check(
            "GET lakehouses",
            r.status_code == 200
            and r.json()[0]["sql_endpoint"]["database"] == "SalesLakehouse"
            and r.json()[1]["sql_endpoint"] is None,
        )
        r = client.get(f"/fabric/workspaces/{RANDOM}/lakehouses")
        check("lakehouses of an unknown workspace is 404", is_envelope(r, 404, "NOT_FOUND"))

        run_id = client.post("/runs", json={"prompt": "Total sales by month"}).json()["run_id"]
        scope_url = f"/runs/{run_id}/scope"

        # Bind
        r = client.put(scope_url, json={"workspace_id": WS, "lakehouse_id": LH, "schemas": ["SalesLT"]})
        body = r.json()
        check("PUT scope is 200", r.status_code == 200)
        check(
            "scope saved with names, endpoint and canonical schema",
            body["scope"]["workspace_id"] == WS
            and body["scope"]["lakehouse_id"] == LH
            and body["scope"]["lakehouse_name"] == "SalesLakehouse"
            and body["scope"]["workspace_name"] == "Sandbox"
            and body["scope"]["schemas"] == ["saleslt"]
            and body["scope"]["sql_endpoint"]["server"] == "abc.datawarehouse.fabric.microsoft.com"
            and body["warnings"] == [],
        )
        check("GET /runs/{id} shows the scope", client.get(f"/runs/{run_id}").json()["scope"] == body["scope"])
        check("state is unchanged by binding", body["state"] == "CREATED" and len(body["history"]) == 1)
        saved_scope = client.get(f"/runs/{run_id}").json()["scope"]

        # Refusals keep the previous scope
        r = client.put(scope_url, json={"workspace_id": RANDOM, "lakehouse_id": LH})
        check("unknown workspace is 403 FORBIDDEN", is_envelope(r, 403, "FORBIDDEN"))
        r = client.put(scope_url, json={"workspace_id": WS, "lakehouse_id": RANDOM})
        check("unknown Lakehouse is 403 FORBIDDEN", is_envelope(r, 403, "FORBIDDEN"))
        r = client.put(scope_url, json={"workspace_id": WS, "lakehouse_id": LH, "schemas": ["does_not_exist"]})
        check("bad schema is 404 SCHEMA_NOT_FOUND", is_envelope(r, 404, "SCHEMA_NOT_FOUND"))
        check("error names the schema", "does_not_exist" in r.json()["error"]["message"])
        check("refused changes keep the previous scope", client.get(f"/runs/{run_id}").json()["scope"] == saved_scope)

        # Validation
        r = client.put(scope_url, json={"workspace_id": "not-a-guid", "lakehouse_id": LH})
        check("non-GUID workspace_id is 422", is_envelope(r, 422, "VALIDATION_ERROR"))
        r = client.put(scope_url, json={"workspace_id": WS})
        check("missing lakehouse_id is 422", is_envelope(r, 422, "VALIDATION_ERROR"))
        r = client.put(f"/runs/{RANDOM}/scope", json={"workspace_id": WS, "lakehouse_id": LH})
        check("unknown run is 404 RUN_NOT_FOUND", is_envelope(r, 404, "RUN_NOT_FOUND"))

        # No SQL endpoint: allowed with a warning
        r = client.put(scope_url, json={"workspace_id": WS, "lakehouse_id": LH_NO_SQL})
        check(
            "Lakehouse without SQL endpoint is allowed with a warning",
            r.status_code == 200 and r.json()["scope"]["sql_endpoint"] is None and len(r.json()["warnings"]) == 1,
        )

        # Locking
        payload = {"workspace_id": WS, "lakehouse_id": LH}

        def set_state(state, artifacts=None):
            run = store.get(run_id)
            run.state = state
            run.artifacts = artifacts or {}
            store.save(run)

        set_state(RunState.NEEDS_CLARIFICATION)
        check("scope can change in NEEDS_CLARIFICATION", client.put(scope_url, json=payload).status_code == 200)
        set_state(RunState.REQUIREMENT_APPROVED)
        check("scope can change in REQUIREMENT_APPROVED before harvest", client.put(scope_url, json=payload).status_code == 200)
        inventory = ArtifactRef(path="MetadataInventory.json", version=1, hash="sha256:a", status="approved", updated_at="2026-10-15T10:00:00Z")
        set_state(RunState.REQUIREMENT_APPROVED, {"MetadataInventory": inventory})
        api_fake.calls.clear()
        r = client.put(scope_url, json=payload)
        check("scope locked after harvest is 409 SCOPE_LOCKED", is_envelope(r, 409, "SCOPE_LOCKED"))
        check("locked check makes no Fabric calls", api_fake.calls == [])
        set_state(RunState.DATA_PROFILED)
        check("scope locked in DATA_PROFILED", is_envelope(client.put(scope_url, json=payload), 409, "SCOPE_LOCKED"))
        set_state(RunState.CREATED)

        # Upstream failures
        api_fake.set(LAKEHOUSES_URL, FakeResponse(429, headers={"Retry-After": "3"}))
        r = client.put(scope_url, json=payload)
        check("rate limit after retries is 429 with Retry-After", is_envelope(r, 429, "RATE_LIMITED") and r.headers.get("retry-after") == "3")
        check("retried 3 times", sleeps[-3:] == [3, 3, 3])
        api_fake.set(LAKEHOUSES_URL, FakeResponse(503, headers={"RequestId": "rid-9"}))
        r = client.put(scope_url, json=payload)
        check("5xx is 502 UPSTREAM_ERROR", is_envelope(r, 502, "UPSTREAM_ERROR"))
        check("502 includes the Fabric request ID", "rid-9" in r.json()["error"]["message"])
        api_fake.set(LAKEHOUSES_URL, requests.Timeout())
        check("timeout is 504 UPSTREAM_TIMEOUT", is_envelope(client.put(scope_url, json=payload), 504, "UPSTREAM_TIMEOUT"))
        api_fake.set(LAKEHOUSES_URL, FakeResponse(200, LAKEHOUSES))

        app.dependency_overrides.clear()

    print("All checks passed.")


if __name__ == "__main__":
    main()
