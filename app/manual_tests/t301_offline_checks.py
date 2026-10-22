"""T-301 offline checks: the metadata harvester and the harvest/inventory endpoints.

Run: python -m app.manual_tests.t301_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
The Lakehouse is replaced by a fake connector, so no sign-in or network is needed.
The live steps (real Lakehouse) are in MANUAL_TESTING.md.
"""

import json
import tempfile
from pathlib import Path

import requests
from fastapi.testclient import TestClient

from app.api.deps import get_connector_factory, get_runs_root, get_store
from app.api.main import app
from app.api.security import AuthContext, Principal, authenticate
from app.connectors.fabric_lakehouse import FabricLakehouseConnector
from app.store.file_store import FileRunStore
from app.workflow.run import ArtifactRef, RunScope
from app.workflow.states import RunState

WS = "11111111-1111-4111-8111-111111111111"
LH = "22222222-2222-4222-8222-222222222222"
RANDOM = "99999999-9999-4999-8999-999999999999"


def check(name: str, condition: bool) -> None:
    print(("PASS " if condition else "FAIL ") + name)
    if not condition:
        raise SystemExit(1)


def http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"HTTP {status}", response=response)


def col(name, type_name="string", nullable=True, comment=None):
    return {"name": name, "type_name": type_name, "nullable": nullable, "comment": comment}


class FakeConnector:
    def __init__(self, data: dict[str, dict[str, object]]):
        # {schema: {table: metadata dict | Exception}}
        self.data = data
        self.calls: list[tuple] = []

    def list_schemas(self):
        return [{"name": n} for n in self.data]

    def list_tables(self, schema):
        self.calls.append(("list_tables", schema))
        return [{"name": t} for t in self.data[schema]]

    def get_table_metadata(self, schema, table):
        self.calls.append(("get", schema, table))
        result = self.data[schema][table]
        if isinstance(result, Exception):
            raise result
        return result


SALES = {
    "SalesLT_Product": {
        "comment": "Products",
        "columns": [col("ProductID", "int", False, "Key"), col("Name"), col("Pris ünïcode")],
    },
    "SalesLT_Customer": {"columns": [col("CustomerID", "int", False)]},
    "Broken": http_error(403),
    "Timeouts": requests.Timeout(),
    "NoColumns": {"columns": []},
}
TEST = {f"t{i}": {"columns": [col("id", "int", False)]} for i in range(1, 6)}


def main() -> None:
    # --- connector paging ---
    pages = [
        {"tables": [{"name": "a"}], "next_page_token": "p2"},
        {"tables": [{"name": "b"}], "next_page_token": "p2"},  # repeated token: stop
    ]
    seen_params = []
    original_get = requests.get

    class Resp:
        def __init__(self, body):
            self._b = body

        def raise_for_status(self):
            pass

        def json(self):
            return self._b

    def fake_get(url, headers=None, params=None, timeout=None):
        seen_params.append(dict(params))
        return Resp(pages.pop(0))

    requests.get = fake_get
    try:
        lh = FabricLakehouseConnector(WS, LH, "L", token_provider=lambda: "t")
        names = [t["name"] for t in lh.list_tables("s")]
    finally:
        requests.get = original_get
    check("list_tables follows next_page_token", names == ["a", "b"])
    check("second page sends page_token", seen_params[1].get("page_token") == "p2" and "page_token" not in seen_params[0])

    # --- API ---
    with tempfile.TemporaryDirectory() as tmp:
        runs_root = Path(tmp) / "runs"
        store = FileRunStore(runs_root)
        connector = FakeConnector({"saleslt": SALES, "nexora_test": TEST})
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_runs_root] = lambda: runs_root
        app.dependency_overrides[authenticate] = lambda: AuthContext(
            principal=Principal(oid="o", upn="tester@contoso.com", name="T"), token="x"
        )
        app.dependency_overrides[get_connector_factory] = lambda: (lambda scope: connector)
        client = TestClient(app, raise_server_exceptions=False)

        def envelope(r, status, code):
            return r.status_code == status and r.json()["error"]["code"] == code

        def new_run(schemas=("saleslt",), state=RunState.REQUIREMENT_APPROVED, scope=True):
            run = store.create("Show sales by month")
            if scope:
                run.scope = RunScope(
                    workspace_id=WS, lakehouse_id=LH, lakehouse_name="SalesLakehouse", schemas=list(schemas)
                )
            run.state = state
            run.artifacts["BusinessContract"] = ArtifactRef(
                path="BusinessContract.yaml", version=2, hash="sha256:abc", status="approved", updated_at="2026-10-15T10:00:00Z"
            )
            store.save(run)
            return run.run_id

        # 1. Harvest
        rid = new_run()
        r = client.post(f"/runs/{rid}/harvest")
        body = r.json()
        check("harvest is 200", r.status_code == 200)
        inv_path = runs_root / rid / "MetadataInventory.json"
        inv = json.loads(inv_path.read_text(encoding="utf-8"))
        check("inventory file written, v1", inv["version"] == 1 and inv["run_id"] == rid and inv["schema_version"] == 1)
        check("scope recorded", inv["scope"] == {"workspace_id": WS, "lakehouse_id": LH, "schemas": ["saleslt"]})
        names = [t["table_name"] for t in inv["tables"]]
        check("failed tables are not in tables, others are", names == ["NoColumns", "SalesLT_Customer", "SalesLT_Product"])
        check(
            "summary matches the file",
            body["summary"]
            == {
                "tables": len(inv["tables"]),
                "columns": sum(len(t["columns"]) for t in inv["tables"]),
                "errors": len(inv["errors"]),
            },
        )
        product = next(t for t in inv["tables"] if t["table_name"] == "SalesLT_Product")
        check(
            "columns, types, nullability, descriptions kept",
            product["schema"] == "saleslt"
            and product["description"] == "Products"
            and product["columns"][0] == {"name": "ProductID", "data_type": "int", "nullable": False, "description": "Key"}
            and product["profile"] is None
            and product["errors"] == [],
        )
        check("unicode and spaces preserved", product["columns"][2]["name"] == "Pris ünïcode")
        codes = {e["table"]: e["code"] for e in inv["errors"]}
        check(
            "per-table errors recorded without aborting",
            codes == {"saleslt.Broken": "FORBIDDEN", "saleslt.Timeouts": "UPSTREAM_TIMEOUT", "saleslt.NoColumns": "NO_COLUMNS"},
        )
        nocol = next(t for t in inv["tables"] if t["table_name"] == "NoColumns")
        check("table with no columns kept with NO_COLUMNS", nocol["columns"] == [] and nocol["errors"][0]["code"] == "NO_COLUMNS")
        check("relationship_evidence empty", inv["relationship_evidence"] == [])
        art = body["artifact"]
        run = store.get(rid)
        check(
            "artifact registered with hash, based_on and path",
            art["name"] == "MetadataInventory"
            and art["version"] == 1
            and art["hash"] == run.artifacts["MetadataInventory"].hash
            and run.artifacts["MetadataInventory"].based_on == {"BusinessContract": "sha256:abc"},
        )
        check("version copy saved", (runs_root / rid / "inventories" / "MetadataInventory.v1.json").is_file())
        check("state unchanged by first harvest", run.state is RunState.REQUIREMENT_APPROVED)

        # GET inventory
        r = client.get(f"/runs/{rid}/inventory")
        check("GET inventory returns the file (alias 'schema')", r.status_code == 200 and r.json()["tables"][0]["schema"] == "saleslt" and r.json()["version"] == 1)
        check("GET inventory before harvest is 404", envelope(client.get(f"/runs/{new_run()}/inventory"), 404, "NOT_FOUND"))
        check("GET inventory of unknown run is 404 RUN_NOT_FOUND", envelope(client.get(f"/runs/{RANDOM}/inventory"), 404, "RUN_NOT_FOUND"))

        # 5. Versioning
        first_hash = run.artifacts["MetadataInventory"].hash
        r = client.post(f"/runs/{rid}/harvest")
        inv2 = json.loads(inv_path.read_text(encoding="utf-8"))
        check("second harvest is version 2", inv2["version"] == 2 and r.json()["artifact"]["version"] == 2)
        check("artifact entry updated, v1 kept", store.get(rid).artifacts["MetadataInventory"].version == 2 and (runs_root / rid / "inventories" / "MetadataInventory.v1.json").is_file())
        check("hash changes with the harvest", r.json()["artifact"]["hash"] != first_hash)

        # 3. Another schema, not hardcoded
        rid2 = new_run(schemas=())
        r = client.post(f"/runs/{rid2}/harvest", json={"schemas": ["NEXORA_TEST"]})
        inv3 = json.loads((runs_root / rid2 / "MetadataInventory.json").read_text(encoding="utf-8"))
        check(
            "explicit schemas override the scope (canonical spelling)",
            r.status_code == 200 and inv3["scope"]["schemas"] == ["nexora_test"] and [t["table_name"] for t in inv3["tables"]] == ["t1", "t2", "t3", "t4", "t5"],
        )
        check("run scope is not changed by the override", store.get(rid2).scope.schemas == [])
        rid3 = new_run(schemas=())
        client.post(f"/runs/{rid3}/harvest")
        inv4 = json.loads((runs_root / rid3 / "MetadataInventory.json").read_text(encoding="utf-8"))
        check("empty scope harvests all schemas", inv4["scope"]["schemas"] == ["nexora_test", "saleslt"])
        r = client.post(f"/runs/{rid3}/harvest", json={})
        check("empty body is accepted", r.status_code == 200)

        # 4. Wrong state
        for state in (RunState.CREATED, RunState.NEEDS_CLARIFICATION, RunState.PLAN_APPROVED):
            check(f"harvest in {state.value} is 409 WRONG_STATE", envelope(client.post(f"/runs/{new_run(state=state)}/harvest"), 409, "WRONG_STATE"))

        # Missing scope, unknown schema, unknown run
        check("no scope is 400 BAD_REQUEST", envelope(client.post(f"/runs/{new_run(scope=False)}/harvest"), 400, "BAD_REQUEST"))
        rid4 = new_run()
        r = client.post(f"/runs/{rid4}/harvest", json={"schemas": ["gone"]})
        check("unknown schema is 404 SCHEMA_NOT_FOUND", envelope(r, 404, "SCHEMA_NOT_FOUND"))
        check("nothing written for an unknown schema", not (runs_root / rid4 / "MetadataInventory.json").exists() and "MetadataInventory" not in store.get(rid4).artifacts)
        check("unknown run is 404", envelope(client.post(f"/runs/{RANDOM}/harvest"), 404, "RUN_NOT_FOUND"))
        check("non-list schemas is 422", envelope(client.post(f"/runs/{rid4}/harvest", json={"schemas": "saleslt"}), 422, "VALIDATION_ERROR"))

        # Re-harvest from DATA_PROFILED
        rid5 = new_run(state=RunState.DATA_PROFILED)
        r = client.post(f"/runs/{rid5}/harvest")
        run5 = store.get(rid5)
        check(
            "re-harvest in DATA_PROFILED moves back to REQUIREMENT_APPROVED",
            r.status_code == 200
            and run5.state is RunState.REQUIREMENT_APPROVED
            and run5.history[-1].actor == "tester@contoso.com"
            and run5.history[-1].to is RunState.REQUIREMENT_APPROVED,
        )

        # Zero tables and listing failures
        connector.data["empty"] = {}
        rid6 = new_run(schemas=("empty",))
        r = client.post(f"/runs/{rid6}/harvest")
        inv6 = json.loads((runs_root / rid6 / "MetadataInventory.json").read_text(encoding="utf-8"))
        check(
            "0 tables gives an empty inventory and a warning",
            r.status_code == 200 and inv6["tables"] == [] and len(inv6["warnings"]) == 1 and r.json()["warnings"] == inv6["warnings"],
        )
        orig = connector.list_tables
        connector.list_tables = lambda schema: (_ for _ in ()).throw(http_error(403))
        r = client.post(f"/runs/{new_run()}/harvest")
        check("listing denied is 403 FORBIDDEN", envelope(r, 403, "FORBIDDEN"))
        connector.list_tables = lambda schema: (_ for _ in ()).throw(requests.Timeout())
        check("listing timeout is 504", envelope(client.post(f"/runs/{new_run()}/harvest"), 504, "UPSTREAM_TIMEOUT"))
        connector.list_tables = orig

        # 500+ tables with progress logging
        connector.data["big"] = {f"t{i}": {"columns": [col("id")]} for i in range(520)}
        rid7 = new_run(schemas=("big",))
        with_logs = []
        import logging

        class Capture(logging.Handler):
            def emit(self, record):
                with_logs.append(record.getMessage())

        handler = Capture()
        logging.getLogger("app.discovery.harvester").addHandler(handler)
        logging.getLogger("app.discovery.harvester").setLevel(logging.INFO)
        r = client.post(f"/runs/{rid7}/harvest")
        check("520 tables harvested", r.json()["summary"]["tables"] == 520)
        check("progress logged every 50 tables", len(with_logs) == 10)

        app.dependency_overrides.clear()

    print("All checks passed.")


if __name__ == "__main__":
    main()
