"""T-405 offline checks: Gate G3, plan edits, and Accept or Reject of the ExecutionPlan.

Run: python -m app.manual_tests.t405_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
The Lakehouse, SQL endpoint and model are fakes, and roles, conformed sources, the glossary and
the derivation rules are temporary files (extending the fixtures of t302 to t404), so no
sign-in or network is needed. The live steps and the Rayfin screen are in MANUAL_TESTING.md.
"""

import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.deps import (
    get_connector_factory,
    get_feasibility_llm,
    get_relationship_llm,
    get_runs_root,
    get_source_llm,
    get_sql_connector_factory,
    get_store,
)
from app.api.main import app
from app.api.security import AuthContext, Principal, authenticate
from app.contracts import business_contract as bc
from app.contracts.model_plan import ExecutionPlan, ModelSpec, load_yaml, save_yaml
from app.discovery import derivation_rules as rules_module
from app.discovery import glossary as glossary_module
from app.manual_tests import t302_offline_checks as base
from app.manual_tests import t401_offline_checks as t401
from app.manual_tests import t402_offline_checks as t402
from app.manual_tests import t403_offline_checks as t403
from app.manual_tests import t404_offline_checks as t404
from app.store.file_store import FileRunStore
from app.workflow import conformed as conformed_module
from app.workflow import roles as roles_module
from app.workflow.run import RunScope, SqlEndpoint
from app.workflow.states import RunState

check = base.check
WS, LH, RANDOM = base.WS, base.LH, base.RANDOM
UPN = "tester@contoso.com"
P, C, D, H = t401.P, t401.C, t401.D, t401.H
NR, MON, RET = t402.NR, t402.MON, t402.RET
HEADER_DATE = "SalesLT_SalesOrderHeader_Date"
SALES_HEADER = "Sales_SalesLT_SalesOrderHeader"


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        runs_root = tmp_dir / "runs"
        store = FileRunStore(runs_root)
        sql = t403.BridgeSql()
        roles_file, conformed_file, glossary_file, rules_file = (tmp_dir / n for n in ("roles.yaml", "conformed.yaml", "glossary.yaml", "rules.yaml"))
        roles_file.write_text(f"roles:\n  bi_engineer: [{UPN}]\n  business_owner: [other@contoso.com]\n", encoding="utf-8")
        conformed = [P, C, D, H, t404.DATE_TABLE]
        t401.write_conformed(conformed_file, conformed)
        glossary_file.write_text(
            "terms:\n  product: [product, products]\n  customer: [customer, customers]\n  sales: [sales, salesorderdetail]\n",
            encoding="utf-8",
        )
        rules_file.write_text("rules: []\n", encoding="utf-8")
        original = (roles_module.CONFIG_PATH, conformed_module.CONFIG_PATH, glossary_module.CONFIG_PATH, rules_module.CONFIG_PATH)
        roles_module.CONFIG_PATH, conformed_module.CONFIG_PATH, glossary_module.CONFIG_PATH, rules_module.CONFIG_PATH = (
            roles_file, conformed_file, glossary_file, rules_file,
        )

        user = {"upn": UPN}
        source_llm = t401.FakeSourceLlm(t402.SOURCE_ANSWER)
        feasibility_llm = t402.FakeFeasibilityLlm()
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_runs_root] = lambda: runs_root
        app.dependency_overrides[authenticate] = lambda: AuthContext(
            principal=Principal(oid="o", upn=user["upn"], name="T"), token="x"
        )
        app.dependency_overrides[get_connector_factory] = lambda: (lambda scope: base.FakeLakehouse())
        app.dependency_overrides[get_sql_connector_factory] = lambda: (lambda scope: sql)
        app.dependency_overrides[get_source_llm] = lambda: source_llm
        app.dependency_overrides[get_feasibility_llm] = lambda: feasibility_llm
        app.dependency_overrides[get_relationship_llm] = lambda: (lambda s, u, sc: json.dumps({"relationships": []}))
        client = TestClient(app, raise_server_exceptions=False)

        def envelope(r, status, code):
            return r.status_code == status and r.json()["error"]["code"] == code

        def new_run(kpis=(NR, MON), schemas=("saleslt", "nexora_test")):
            run = store.create("Show net revenue by product and customer")
            run.scope = RunScope(
                workspace_id=WS, lakehouse_id=LH, lakehouse_name="SalesLakehouse", schemas=list(schemas),
                sql_endpoint=SqlEndpoint(server="s.fabric", database="SalesLakehouse"),
            )
            run.state = RunState.REQUIREMENT_APPROVED
            store.save(run)
            run_dir = runs_root / run.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            bc.save(
                bc.BusinessContract(
                    run_id=run.run_id, business_objective="Revenue", entities=["Sales", "Product", "Customer"],
                    kpis=[bc.Kpi(name=k, grain="order line", currency="USD", **t402.KPIS[k]) for k in kpis],
                ),
                run_dir / "BusinessContract.yaml",
            )
            assert client.post(f"/runs/{run.run_id}/harvest").status_code == 200
            assert client.post(f"/runs/{run.run_id}/profile").status_code == 200
            candidates = [t404.DETAIL_TO_HEADER] + ([t404.HEADER_TO_DATE] if "nexora_test" in schemas else [])
            assert client.post(f"/runs/{run.run_id}/relationships/analyze", json={"candidates": candidates}).status_code == 200
            run = store.get(run.run_id)
            run.state = RunState.DATA_PROFILED
            store.save(run)
            assert client.post(f"/runs/{run.run_id}/plan/sources").status_code == 200
            feasibility_llm.answers = [t402.default_answer()]
            assert client.post(f"/runs/{run.run_id}/plan/feasibility").status_code == 200
            assert client.post(f"/runs/{run.run_id}/plan").status_code == 200
            return run.run_id

        def plan_hash(rid):
            return store.get(rid).artifacts["ExecutionPlan"].hash

        def g3(rid):
            r = client.post(f"/runs/{rid}/gates/G3/check")
            return r, {c["id"]: c for c in r.json().get("checks", [])}

        def patch(rid, ops, expected=None):
            return client.patch(f"/runs/{rid}/plan", json={"expected_hash": expected or plan_hash(rid), "ops": ops})

        def decide(rid, action="ACCEPT", expected=None, **extra):
            return client.post(
                f"/runs/{rid}/approvals", json={"artifact": "ExecutionPlan", "action": action, "expected_hash": expected or plan_hash(rid), **extra}
            )

        def approvals(rid):
            path = runs_root / rid / "approvals.jsonl"
            return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

        # a clean plan passes G3
        rid = new_run()
        r, checks = g3(rid)
        check("G3 passes on a clean plan: G3.1-G3.7 are present and pass", r.status_code == 200 and r.json()["passed"] is True and r.json()["gate"] == "G3"
              and sorted(checks) == [f"G3.{i}" for i in range(1, 8)] and all(c["status"] == "pass" for c in checks.values()))
        check("the gate result is about the ExecutionPlan hash and is saved", r.json()["artifact"] == "ExecutionPlan" and r.json()["artifact_hash"] == plan_hash(rid)
              and len(list((runs_root / rid / "gates").glob("G3-*.json"))) == 1)
        check("an unknown gate lists G3 as available", "G3" in client.post(f"/runs/{rid}/gates/G9/check").json()["error"]["message"])

        # the sources behind the plan are readable
        r = client.get(f"/runs/{rid}/plan/sources")
        check("GET /plan/sources returns the saved resolution with its rationale", r.status_code == 200 and r.json()["entities"][0]["selected"]["rationale"])

        # AC2: an edit re-validates
        before = (store.get(rid).artifacts["ModelSpec"].version, store.get(rid).artifacts["ExecutionPlan"].version)
        old_hash = plan_hash(rid)
        r = patch(rid, [{"op": "remove_relationship", "name": HEADER_DATE}])
        body = r.json()
        failed = {c["id"]: c for c in body["gate_result"]["checks"] if c["status"] == "fail"}
        check("removing the date relationship re-runs G3, which fails and names the measure that needs the date", r.status_code == 200 and body["gate_result"]["passed"] is False
              and list(failed) == ["G3.6"] and failed["G3.6"]["evidence"]["paths"] == [f"measures[{MON}]: needs Date related to Sales, but no relationship path connects them"])
        check("the edit made new versions of both files and kept the old ones", (store.get(rid).artifacts["ModelSpec"].version, store.get(rid).artifacts["ExecutionPlan"].version) == (before[0] + 1, before[1] + 1)
              and (runs_root / rid / "plans" / "ModelSpec.v1.yaml").is_file() and (runs_root / rid / "plans" / "ExecutionPlan.v1.yaml").is_file() and plan_hash(rid) != old_hash
              and all(x["name"] != HEADER_DATE for x in body["model_spec"]["relationships"]))
        check("the response carries the new plan, rebased on the new model", body["execution_plan"]["version"] == before[1] + 1
              and store.get(rid).artifacts["ExecutionPlan"].based_on == {"ModelSpec": store.get(rid).artifacts["ModelSpec"].hash})
        last = approvals(rid)[-1]
        check("an EDIT approval record names the operation", last["action"] == "EDIT" and last["artifact"] == "ExecutionPlan" and last["comment"] == f"remove_relationship {HEADER_DATE}" and last["role"] == "bi_engineer")
        check("a stale hash is 409 STALE_VERSION and changes nothing", envelope(patch(rid, [{"op": "remove_measure", "name": MON}], expected=old_hash), 409, "STALE_VERSION")
              and store.get(rid).artifacts["ExecutionPlan"].version == before[1] + 1)
        r = decide(rid)
        check("Accept with a failing G3 is 409 GATE_FAILED naming G3.6; the state doesn't change", envelope(r, 409, "GATE_FAILED") and r.json()["error"]["details"]["failed_checks"] == ["G3.6"]
              and store.get(rid).state is RunState.DATA_PROFILED)

        r = patch(rid, [{"op": "remove_relationship", "name": SALES_HEADER}])
        names = {p.split("]")[0].split("[")[1] for p in {c["id"]: c for c in r.json()["gate_result"]["checks"]}["G3.6"]["evidence"]["paths"]}
        check("removing the relationship to the order header fails both measures that need it", names == {NR, MON})
        r = patch(rid, [{"op": "remove_measure", "name": NR}, {"op": "remove_measure", "name": MON}])
        check("removing the measures makes G3.6 pass again, in one edit with two operations", r.status_code == 200 and r.json()["model_spec"]["measures"] == [])

        # edit validation
        version = store.get(rid).artifacts["ExecutionPlan"].version
        r = patch(rid, [{"op": "explode", "name": "x"}])
        check("an unknown op is 422 and lists the valid ops", r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
              and all(op in r.json()["error"]["message"] for op in ("remove_relationship", "set_storage_mode", "override_relationship", "remove_measure")))
        check("an unknown relationship is 422 and nothing is saved", envelope(patch(rid, [{"op": "remove_relationship", "name": "Nope"}]), 422, "VALIDATION_ERROR") and store.get(rid).artifacts["ExecutionPlan"].version == version)
        check("a bad op in a list saves none of the list", envelope(patch(rid, [{"op": "set_storage_mode", "value": "Import"}, {"op": "remove_measure", "name": "Nope"}]), 422, "VALIDATION_ERROR")
              and load_yaml(ModelSpec, runs_root / rid / "ModelSpec.yaml").storage_mode == "DirectLake")
        check("an invalid storage mode is 422", envelope(patch(rid, [{"op": "set_storage_mode", "value": "Excel"}]), 422, "VALIDATION_ERROR"))
        check("an empty op list is 422", envelope(patch(rid, []), 422, "VALIDATION_ERROR"))
        check("an override needs a rationale (422)", envelope(patch(rid, [{"op": "override_relationship", "name": "Sales_Product", "rationale": " "}]), 422, "VALIDATION_ERROR"))
        r = patch(rid, [{"op": "set_storage_mode", "value": "Import"}])
        check("set_storage_mode changes the model", r.status_code == 200 and r.json()["model_spec"]["storage_mode"] == "Import")
        r = patch(rid, [{"op": "override_relationship", "name": "Sales_Product", "rationale": "Confirmed with the data owner"}])
        check("override_relationship records the rationale", next(x for x in r.json()["model_spec"]["relationships"] if x["name"] == "Sales_Product")["override_rationale"] == "Confirmed with the data owner")

        # the other G3 checks
        def hand_edit(model_change=None, plan_change=None):
            if model_change:
                m = load_yaml(ModelSpec, runs_root / rid / "ModelSpec.yaml")
                model_change(m)
                save_yaml(m, runs_root / rid / "ModelSpec.yaml")
            if plan_change:
                p = load_yaml(ExecutionPlan, runs_root / rid / "ExecutionPlan.yaml")
                plan_change(p)
                save_yaml(p, runs_root / rid / "ExecutionPlan.yaml")
            store.get(rid)
            run = store.get(rid)
            from app.contracts.model_plan import to_data
            from app.workflow.harvest_stage import canonical_hash
            run.artifacts["ExecutionPlan"].hash = canonical_hash(to_data(load_yaml(ExecutionPlan, runs_root / rid / "ExecutionPlan.yaml")))
            store.save(run)

        assert client.post(f"/runs/{rid}/plan").status_code == 200  # regenerate
        r, checks = g3(rid)
        check("regenerating gives a plan that passes G3 again", r.json()["passed"] is True and load_yaml(ModelSpec, runs_root / rid / "ModelSpec.yaml").storage_mode == "DirectLake")

        def no_grain(m):
            next(t for t in m.tables if t.role == "fact").grain = None

        hand_edit(model_change=no_grain)
        r, checks = g3(rid)
        check("G3.1 fails when the fact has no grain, naming the table", checks["G3.1"]["status"] == "fail" and checks["G3.1"]["evidence"]["paths"] == ["tables[Sales].grain: no grain defined"])
        assert client.post(f"/runs/{rid}/plan").status_code == 200

        t401.write_conformed(conformed_file, [C, D, H, t404.DATE_TABLE])
        r, checks = g3(rid)
        check("G3.2 fails for a source that stopped being conformed (read now, not from the plan)", checks["G3.2"]["status"] == "fail"
              and checks["G3.2"]["evidence"]["paths"] == ["sources[saleslt.SalesLT_Product]: not conformed and no approved exception"])
        t401.write_conformed(conformed_file, conformed)

        def weak(m):
            evidence = json.loads((runs_root / rid / "MetadataInventory.json").read_text(encoding="utf-8"))["relationship_evidence"]
            m.relationships[0].evidence_ref = next(e["id"] for e in evidence if e["confidence"] == "low" and e["status"] == "candidate")

        hand_edit(model_change=weak)
        r, checks = g3(rid)
        check("G3.3 fails for a low-confidence relationship without an override", checks["G3.3"]["status"] == "fail" and "Sales_Product" in checks["G3.3"]["evidence"]["paths"][0])
        r = patch(rid, [{"op": "override_relationship", "name": "Sales_Product", "rationale": "Confirmed"}])
        check("an override fixes G3.3 through an edit", {c["id"]: c for c in r.json()["gate_result"]["checks"]}["G3.3"]["status"] == "pass")
        assert client.post(f"/runs/{rid}/plan").status_code == 200

        def bad_stages(p):
            p.stages[1].depends_on = ["nonexistent"]
            p.stages[0].depends_on = ["report"]  # report depends on semantic_model: a cycle

        hand_edit(plan_change=bad_stages)
        r, checks = g3(rid)
        paths = checks["G3.4"]["evidence"]["paths"]
        check("G3.4 fails for an unknown dependency", checks["G3.4"]["status"] == "fail" and "stages[report].depends_on: unknown stage 'nonexistent'" in paths)

        def cycle(p):
            p.stages[0].depends_on = ["deployment"]
            p.stages[1].depends_on = ["semantic_model"]
            p.stages[2].depends_on = ["report"]

        hand_edit(plan_change=cycle)
        check("G3.4 fails for a dependency cycle", "stages: the dependencies form a cycle" in g3(rid)[1]["G3.4"]["evidence"]["paths"])
        assert client.post(f"/runs/{rid}/plan").status_code == 200

        def reference(m):
            m.relationships[0].to.column = "NoSuchColumn"

        hand_edit(model_change=reference)
        r, checks = g3(rid)
        check("G3.7 fails for a reference to a column that doesn't exist, naming the path", checks["G3.7"]["status"] == "fail"
              and any("NoSuchColumn" in p for p in checks["G3.7"]["evidence"]["paths"]))
        assert client.post(f"/runs/{rid}/plan").status_code == 200

        # AC3 and AC4: approve
        rid2 = new_run()
        user["upn"] = "someone.else@contoso.com"
        r = decide(rid2)
        check("Accept without bi_engineer is 403 and changes nothing", envelope(r, 403, "FORBIDDEN") and r.json()["error"]["details"]["required_role"] == "bi_engineer"
              and store.get(rid2).state is RunState.DATA_PROFILED and approvals(rid2) == [])
        user["upn"] = "other@contoso.com"  # a business_owner is not enough either
        check("a business_owner can't accept the plan", envelope(decide(rid2), 403, "FORBIDDEN"))
        check("Reject without bi_engineer is 403", envelope(decide(rid2, "REJECT", comment="no"), 403, "FORBIDDEN"))
        user["upn"] = UPN
        check("Accept with a stale hash is 409 STALE_VERSION", envelope(decide(rid2, expected="sha256:old"), 409, "STALE_VERSION"))
        check("REJECT needs a comment (422)", envelope(decide(rid2, "REJECT"), 422, "VALIDATION_ERROR"))
        check("EXPLAIN is not available for the plan (422)", envelope(decide(rid2, "EXPLAIN"), 422, "VALIDATION_ERROR"))
        r = decide(rid2, comment="Looks right")
        run = store.get(rid2)
        record = approvals(rid2)[-1]
        check("a bi_engineer accepts: 200 and the run is PLAN_APPROVED", r.status_code == 200 and run.state is RunState.PLAN_APPROVED and r.json()["run"]["state"] == "PLAN_APPROVED"
              and r.json()["gate_result"]["passed"] is True)
        check("the approval record holds the ExecutionPlan hash, role, user and gate result", record["artifact"] == "ExecutionPlan" and record["action"] == "ACCEPT" and record["artifact_hash"] == run.artifacts["ExecutionPlan"].hash
              and record["role"] == "bi_engineer" and record["user"]["upn"] == UPN and record["gate_result_ref"].startswith("gates/G3-") and record["comment"] == "Looks right")
        check("both artifacts are approved", run.artifacts["ExecutionPlan"].status == "approved" and run.artifacts["ModelSpec"].status == "approved")
        check("the history names the actor", run.history[-1].to is RunState.PLAN_APPROVED and run.history[-1].actor == UPN)
        check("accepting again is 409 ALREADY_APPROVED", envelope(decide(rid2), 409, "ALREADY_APPROVED"))
        check("an approved plan can't be edited (409 WRONG_STATE)", envelope(patch(rid2, [{"op": "remove_measure", "name": NR}]), 409, "WRONG_STATE"))
        check("an approved plan can't be regenerated (409 WRONG_STATE)", envelope(client.post(f"/runs/{rid2}/plan"), 409, "WRONG_STATE"))

        # reject reopens an approved plan
        r = decide(rid2, "REJECT", comment="Grain is wrong")
        run = store.get(rid2)
        check("rejecting an approved plan reopens it: DATA_PROFILED, drafts, comment recorded", r.status_code == 200 and run.state is RunState.DATA_PROFILED
              and run.artifacts["ExecutionPlan"].status == "draft" and run.artifacts["ModelSpec"].status == "draft"
              and approvals(rid2)[-1]["action"] == "REJECT" and approvals(rid2)[-1]["comment"] == "Grain is wrong")
        count = len(approvals(rid2))
        r = decide(rid2, "REJECT", comment="Still not right")
        check("rejecting a draft plan records the comment and leaves the state", r.status_code == 200 and store.get(rid2).state is RunState.DATA_PROFILED and len(approvals(rid2)) == count + 1)
        check("the plan can be accepted again after a rejection", decide(rid2).status_code == 200 and store.get(rid2).state is RunState.PLAN_APPROVED)

        # G3.5: a blocking data-engineering action
        rid3 = new_run(kpis=(MON,), schemas=("saleslt",))
        r, checks = g3(rid3)
        check("G3.5 fails while a blocking data-engineering action is open, showing the action and its owner", checks["G3.5"]["status"] == "fail"
              and checks["G3.5"]["evidence"]["actions"][0]["kind"] == "create_date_table" and checks["G3.5"]["evidence"]["actions"][0]["owner"] is None)
        r = decide(rid3)
        check("Accept is blocked by G3.5 only", envelope(r, 409, "GATE_FAILED") and r.json()["error"]["details"]["failed_checks"] == ["G3.5"] and store.get(rid3).state is RunState.DATA_PROFILED)

        # errors
        bare = store.create("x")
        bare.state = RunState.DATA_PROFILED
        store.save(bare)
        check("PATCH before a plan is 404", envelope(client.patch(f"/runs/{bare.run_id}/plan", json={"expected_hash": "x", "ops": [{"op": "remove_measure", "name": "a"}]}), 404, "NOT_FOUND"))
        check("PATCH in the wrong state is 409 WRONG_STATE", envelope(client.patch(f"/runs/{store.create('w').run_id}/plan", json={"expected_hash": "x", "ops": [{"op": "remove_measure", "name": "a"}]}), 409, "WRONG_STATE"))
        check("G3 before a plan is 404", envelope(client.post(f"/runs/{store.create('y').run_id}/gates/G3/check"), 404, "NOT_FOUND"))
        check("unknown run is 404", envelope(client.post(f"/runs/{RANDOM}/gates/G3/check"), 404, "RUN_NOT_FOUND"))
        check("GET /plan/sources before sources is 404", envelope(client.get(f"/runs/{store.create('z').run_id}/plan/sources"), 404, "NOT_FOUND"))
        conformed_file.write_text("conformed: x\n", encoding="utf-8")
        check("an invalid conformed config is 500 CONFIG_ERROR", envelope(client.post(f"/runs/{rid3}/gates/G3/check"), 500, "CONFIG_ERROR"))

        roles_module.CONFIG_PATH, conformed_module.CONFIG_PATH, glossary_module.CONFIG_PATH, rules_module.CONFIG_PATH = original
        app.dependency_overrides.clear()

    print("All checks passed.")


if __name__ == "__main__":
    main()
