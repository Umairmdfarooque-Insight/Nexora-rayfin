"""T-304 offline checks: Gate G2, exceptions, data gaps and Accept of the metadata inventory.

Run: python -m app.manual_tests.t304_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
The SQL endpoint, the Lakehouse and the roles and conformed-source files are fakes or
temporary files (extending the fixtures of t302 and t303), so no sign-in or network is needed.
The live steps and the Rayfin screen are in MANUAL_TESTING.md.
"""

import json
import tempfile
from pathlib import Path

import requests
from fastapi.testclient import TestClient

from app.api.deps import (
    get_connector_factory,
    get_relationship_llm,
    get_runs_root,
    get_sql_connector_factory,
    get_store,
)
from app.api.main import app
from app.api.security import AuthContext, Principal, authenticate
from app.compiler.critical_terms import ConfigError
from app.contracts import business_contract as bc
from app.manual_tests import t302_offline_checks as base
from app.manual_tests import t303_offline_checks as t303  # extends the t302 fixtures
from app.store.file_store import FileRunStore
from app.workflow import conformed as conformed_module
from app.workflow import roles as roles_module
from app.workflow.run import RunScope, SqlEndpoint
from app.workflow.states import RunState

check = base.check
WS, LH, RANDOM = base.WS, base.LH, base.RANDOM
UPN = "tester@contoso.com"
P, C, CP = "saleslt.SalesLT_Product", "saleslt.SalesLT_Customer", "saleslt.SalesLT_CustomerProfile"


class FailingLakehouse(base.FakeLakehouse):
    """Like the T-302 fake Lakehouse, but some tables return HTTP 403."""

    failing: set[str] = set()

    def get_table_metadata(self, schema, table):
        if table in self.failing:
            response = requests.Response()
            response.status_code = 403
            raise requests.HTTPError("403", response=response)
        return super().get_table_metadata(schema, table)


def write_roles(path: Path, data_engineers: list[str]) -> None:
    path.write_text(
        "roles:\n  data_engineer: [" + ", ".join(data_engineers) + "]\n  bi_engineer: [other@contoso.com]\n",
        encoding="utf-8",
    )


def write_conformed(path: Path, names: list[str]) -> None:
    path.write_text("conformed:\n" + "".join(f"  - {n}\n" for n in names), encoding="utf-8")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        # --- conformed_sources.yaml ---
        path = tmp_dir / "conformed_sources.yaml"
        try:
            conformed_module.load_conformed(path)
            named = False
        except ConfigError as exc:
            named = "conformed_sources.yaml" in str(exc)
        check("missing conformed_sources.yaml raises ConfigError naming the file", named)
        for label, body in (("not a mapping", "- a\n"), ("not a list", "conformed: x\n"), ("bad name", "conformed: [nodot]\n")):
            path.write_text(body, encoding="utf-8")
            try:
                conformed_module.load_conformed(path)
                raised = False
            except ConfigError as exc:
                raised = "conformed_sources.yaml" in str(exc)
            check(f"invalid conformed_sources.yaml ({label}) raises ConfigError", raised)
        write_conformed(path, ["SalesLT.SalesLT_Product"])
        check("names are matched ignoring case", conformed_module.load_conformed(path) == {"saleslt.saleslt_product"})
        shipped = conformed_module.load_conformed()
        check("the shipped config lists the sample conformed tables", "saleslt.saleslt_customer" in shipped and "nexora_test.dim_date" in shipped)

        runs_root = tmp_dir / "runs"
        store = FileRunStore(runs_root)
        sql = t303.EvidenceSql()
        roles_file, conformed_file = tmp_dir / "roles.yaml", tmp_dir / "conformed.yaml"
        write_roles(roles_file, [UPN])
        write_conformed(conformed_file, [P, C, CP])
        original = (roles_module.CONFIG_PATH, conformed_module.CONFIG_PATH)
        roles_module.CONFIG_PATH, conformed_module.CONFIG_PATH = roles_file, conformed_file

        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_runs_root] = lambda: runs_root
        app.dependency_overrides[authenticate] = lambda: AuthContext(
            principal=Principal(oid="o", upn=UPN, name="T"), token="x"
        )
        app.dependency_overrides[get_connector_factory] = lambda: (lambda scope: FailingLakehouse())
        app.dependency_overrides[get_sql_connector_factory] = lambda: (lambda scope: sql)
        app.dependency_overrides[get_relationship_llm] = lambda: (lambda s, u, sc: json.dumps({"relationships": []}))
        client = TestClient(app, raise_server_exceptions=False)

        def envelope(r, status, code):
            return r.status_code == status and r.json()["error"]["code"] == code

        def new_run(entities=("Products", "Customers"), state=RunState.REQUIREMENT_APPROVED, profile=True, harvest=True):
            run = store.create("Show sales by product and customer")
            run.scope = RunScope(
                workspace_id=WS, lakehouse_id=LH, lakehouse_name="SalesLakehouse", schemas=["saleslt"],
                sql_endpoint=SqlEndpoint(server="s.fabric", database="SalesLakehouse"),
            )
            run.state = state
            store.save(run)
            run_dir = runs_root / run.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            bc.save(bc.BusinessContract(run_id=run.run_id, business_objective="Sales", entities=list(entities)), run_dir / "BusinessContract.yaml")
            if harvest and state is RunState.REQUIREMENT_APPROVED:
                assert client.post(f"/runs/{run.run_id}/harvest").status_code == 200
                if profile:
                    assert client.post(f"/runs/{run.run_id}/profile").status_code == 200
            return run.run_id

        def g2(rid):
            r = client.post(f"/runs/{rid}/gates/G2/check")
            return r, {c["id"]: c for c in r.json().get("checks", [])}

        def hash_of(rid):
            return store.get(rid).artifacts["MetadataInventory"].hash

        def accept(rid, expected=None, **extra):
            return client.post(
                f"/runs/{rid}/approvals",
                json={"artifact": "MetadataInventory", "action": "ACCEPT", "expected_hash": expected or hash_of(rid), **extra},
            )

        # --- AC1: pass on conformed data, then Accept ---
        rid = new_run(profile=False)
        r, checks = g2(rid)
        check("G2 before profiling: G2.3 fails and names the tables", r.status_code == 200 and r.json()["passed"] is False and checks["G2.3"]["status"] == "fail"
              and set(checks["G2.3"]["evidence"]["tables"]) == {P, C, CP})
        check("accept with a failing gate is 409 GATE_FAILED naming the checks", envelope(accept(rid), 409, "GATE_FAILED")
              and "G2.3" in accept(rid).json()["error"]["details"]["failed_checks"])
        check("a refused accept leaves the state and writes no record", store.get(rid).state is RunState.REQUIREMENT_APPROVED and not (runs_root / rid / "approvals.jsonl").exists())

        assert client.post(f"/runs/{rid}/profile").status_code == 200
        r, checks = g2(rid)
        body = r.json()
        check("G2 passes on conformed, profiled data", body["passed"] is True and body["gate"] == "G2" and body["artifact"] == "MetadataInventory" and body["artifact_hash"] == hash_of(rid))
        check("candidates are the tables matching the contract", set(checks["G2.1"]["evidence"]["candidates"]) == {P, C, CP})
        check("G2.1, G2.2, G2.3 and G2.5 pass", all(checks[i]["status"] == "pass" for i in ("G2.1", "G2.2", "G2.3", "G2.5")))
        check("G2.4 only ever passes or warns", checks["G2.4"]["status"] in ("pass", "warning"))
        check("gate result saved under gates/G2-*.json", len(list((runs_root / rid / "gates").glob("G2-*.json"))) >= 2)

        check("accept with a stale hash is 409 STALE_VERSION", envelope(accept(rid, "sha256:old"), 409, "STALE_VERSION"))
        r = accept(rid, comment="Sources look fine")
        run = store.get(rid)
        check("Accept: 200 and the run is DATA_PROFILED", r.status_code == 200 and run.state is RunState.DATA_PROFILED and r.json()["run"]["state"] == "DATA_PROFILED")
        check("inventory artifact is approved", run.artifacts["MetadataInventory"].status == "approved")
        record = [json.loads(line) for line in (runs_root / rid / "approvals.jsonl").read_text(encoding="utf-8").splitlines()][-1]
        check(
            "approval record: artifact, hash, role, user, gate result",
            record["artifact"] == "MetadataInventory" and record["action"] == "ACCEPT" and record["artifact_hash"] == hash_of(rid)
            and record["role"] == "data_engineer" and record["user"]["upn"] == UPN and record["gate_result_ref"].startswith("gates/G2-")
            and record["comment"] == "Sources look fine",
        )
        check("history names the actor", run.history[-1].to is RunState.DATA_PROFILED and run.history[-1].actor == UPN)
        check("accept again is 409 WRONG_STATE", envelope(accept(rid), 409, "WRONG_STATE"))

        # Re-harvest after G2 passed
        r = client.post(f"/runs/{rid}/harvest")
        run = store.get(rid)
        check("re-harvest returns the run to REQUIREMENT_APPROVED with a draft inventory", r.status_code == 200 and run.state is RunState.REQUIREMENT_APPROVED
              and run.artifacts["MetadataInventory"].status == "draft" and run.artifacts["MetadataInventory"].version == 2)
        r, checks = g2(rid)
        check("G2 must be run again: the new inventory has no profiles", r.json()["passed"] is False and checks["G2.3"]["status"] == "fail")

        # --- Other actions and roles ---
        rid2 = new_run()
        r = client.post(f"/runs/{rid2}/approvals", json={"artifact": "MetadataInventory", "action": "REJECT", "comment": "no", "expected_hash": hash_of(rid2)})
        check("only ACCEPT exists for the inventory (422)", envelope(r, 422, "VALIDATION_ERROR"))
        write_roles(roles_file, [])
        check("accept without data_engineer is 403 FORBIDDEN", envelope(accept(rid2), 403, "FORBIDDEN") and store.get(rid2).state is RunState.REQUIREMENT_APPROVED)
        write_roles(roles_file, [UPN])

        # --- AC2: exception required ---
        write_conformed(conformed_file, [P, CP])  # Customer is no longer conformed
        r, checks = g2(rid2)
        check("G2.5 fails naming the non-conformed table", r.json()["passed"] is False and checks["G2.5"]["status"] == "fail" and checks["G2.5"]["evidence"]["missing_exceptions"] == [C])
        r = accept(rid2)
        check("accept is blocked by G2.5", envelope(r, 409, "GATE_FAILED") and r.json()["error"]["details"]["failed_checks"] == ["G2.5"])

        # --- AC3: exception approved ---
        write_roles(roles_file, [])
        check("exception without data_engineer is 403", envelope(client.post(f"/runs/{rid2}/exceptions", json={"table": C, "rationale": "needed"}), 403, "FORBIDDEN"))
        write_roles(roles_file, [UPN])
        check("exception for a table that is not a candidate is 422", envelope(client.post(f"/runs/{rid2}/exceptions", json={"table": "saleslt.Extra", "rationale": "x"}), 422, "VALIDATION_ERROR"))
        check("exception with no rationale is 422", envelope(client.post(f"/runs/{rid2}/exceptions", json={"table": C, "rationale": " "}), 422, "VALIDATION_ERROR"))
        check("exception with a bad table name is 422", envelope(client.post(f"/runs/{rid2}/exceptions", json={"table": "nodot", "rationale": "x"}), 422, "VALIDATION_ERROR"))
        r = client.post(f"/runs/{rid2}/exceptions", json={"table": "SALESLT.salesLT_customer", "rationale": "Customer master is the only source"})
        body = r.json()
        check("exception is 201 with id, canonical table, approver and time", r.status_code == 201 and body["table"] == C and body["approved_by"] == UPN and body["exception_id"] and body["at"])
        records = client.get(f"/runs/{rid2}/approvals").json()
        exc = [x for x in records if x["artifact"].startswith("Exception:")]
        check("the exception is an approval record", len(exc) == 1 and exc[0]["artifact"] == f"Exception:{C}" and exc[0]["comment"] == "Customer master is the only source" and exc[0]["role"] == "data_engineer" and exc[0]["action"] == "ACCEPT")
        r, checks = g2(rid2)
        check("G2.5 passes with the exception", checks["G2.5"]["status"] == "pass" and checks["G2.5"]["evidence"]["excepted"] == [C] and r.json()["passed"] is True)
        check("Accept now succeeds", accept(rid2).status_code == 200 and store.get(rid2).state is RunState.DATA_PROFILED)
        check("exception after the state moved on is 409 WRONG_STATE", envelope(client.post(f"/runs/{rid2}/exceptions", json={"table": C, "rationale": "again"}), 409, "WRONG_STATE"))
        write_conformed(conformed_file, [P, C, CP])

        # --- failed tables ---
        FailingLakehouse.failing = {"SalesLT_Customer"}
        rid3 = new_run()
        inv = client.get(f"/runs/{rid3}/inventory").json()
        check("a table that failed to harvest is not in the inventory", C not in {f"{t['schema']}.{t['table_name']}" for t in inv["tables"]} and inv["errors"][0]["code"] == "FORBIDDEN")
        r, checks = g2(rid3)
        check("G2.1 fails for a candidate that failed to harvest", checks["G2.1"]["status"] == "fail" and checks["G2.1"]["evidence"]["errors"] == {C: ["FORBIDDEN"]} and C in checks["G2.1"]["evidence"]["candidates"])
        FailingLakehouse.failing = set()
        base.ERRORS[("saleslt", "SalesLT_Customer")] = ("42S02", "[42S02] Invalid object name")
        rid4 = new_run()
        r, checks = g2(rid4)
        check("G2.1 fails when a candidate can't be profiled", checks["G2.1"]["status"] == "fail" and checks["G2.1"]["evidence"]["errors"] == {C: ["TABLE_NOT_FOUND"]})
        del base.ERRORS[("saleslt", "SalesLT_Customer")]

        # --- no candidates, data gap ---
        rid5 = new_run(entities=("Warehouses",))
        r, checks = g2(rid5)
        check("no candidate tables: G2.1 fails with the reason", checks["G2.1"]["status"] == "fail" and "no candidate tables" in checks["G2.1"]["evidence"]["reason"])
        r = client.post(f"/runs/{rid5}/data-gaps", json={"description": "Warehouse data is not in the Lakehouse"})
        body = r.json()
        check("data gap: 200, NEEDS_INPUT, resume state kept", r.status_code == 200 and body["state"] == "NEEDS_INPUT" and body["resume_state"] == "REQUIREMENT_APPROVED")
        check("the gap is shown on the run detail", [g["description"] for g in body["data_gaps"]] == ["Warehouse data is not in the Lakehouse"] and body["data_gaps"][0]["raised_by"] == UPN
              and [g["description"] for g in client.get(f"/runs/{rid5}").json()["data_gaps"]] == ["Warehouse data is not in the Lakehouse"])
        check("the history carries the description", "Warehouse data" in store.get(rid5).history[-1].reason)
        check("a second gap while NEEDS_INPUT is 409", envelope(client.post(f"/runs/{rid5}/data-gaps", json={"description": "again"}), 409, "WRONG_STATE"))
        check("empty description is 422", envelope(client.post(f"/runs/{new_run()}/data-gaps", json={"description": "  "}), 422, "VALIDATION_ERROR"))
        check("data gap in CREATED is 409 WRONG_STATE", envelope(client.post(f"/runs/{new_run(state=RunState.CREATED)}/data-gaps", json={"description": "x"}), 409, "WRONG_STATE"))
        rid6 = new_run()
        assert accept(rid6).status_code == 200
        check("a data gap can be raised from DATA_PROFILED", client.post(f"/runs/{rid6}/data-gaps", json={"description": "Need more"}).json()["resume_state"] == "DATA_PROFILED")

        # --- errors ---
        check("unknown gate is 404", envelope(client.post(f"/runs/{rid}/gates/G9/check"), 404, "NOT_FOUND"))
        rid7 = new_run(harvest=False)
        check("G2 before harvest is 404", envelope(client.post(f"/runs/{rid7}/gates/G2/check"), 404, "NOT_FOUND"))
        check("accept before harvest is 404", envelope(client.post(f"/runs/{rid7}/approvals", json={"artifact": "MetadataInventory", "action": "ACCEPT", "expected_hash": "sha256:x"}), 404, "NOT_FOUND"))
        check("unknown run is 404", envelope(client.post(f"/runs/{RANDOM}/exceptions", json={"table": C, "rationale": "x"}), 404, "RUN_NOT_FOUND"))
        conformed_file.write_text("conformed: x\n", encoding="utf-8")
        check("invalid conformed config is 500 CONFIG_ERROR", envelope(client.post(f"/runs/{rid3}/gates/G2/check"), 500, "CONFIG_ERROR"))

        roles_module.CONFIG_PATH, conformed_module.CONFIG_PATH = original
        app.dependency_overrides.clear()

    print("All checks passed.")


if __name__ == "__main__":
    main()
