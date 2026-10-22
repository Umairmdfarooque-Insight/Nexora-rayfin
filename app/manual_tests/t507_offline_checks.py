"""T-507 offline checks: the model review data, the role approvals of the SemanticModel and the move
to MODEL_APPROVED.

Run: python -m app.manual_tests.t507_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
Fabric, Power BI, the SQL endpoint and the models are fakes (extending the fixtures of t502 to t504),
roles are a temporary file, and the run is written by hand into a temporary folder, so no sign-in or
network is needed. The Rayfin screen and the live steps are in MANUAL_TESTING.md.
"""

import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.deps import (
    get_dax_llm,
    get_items_client,
    get_powerbi_client,
    get_recon_llm,
    get_runs_root,
    get_sql_connector_factory,
    get_store,
)
from app.api.main import app
from app.api.security import AuthContext, Principal, authenticate
from app.contracts import business_contract as bc
from app.contracts.model_plan import ModelSpec, RlsRole, load_yaml, save_yaml, to_data
from app.manual_tests import t502_offline_checks as t502
from app.manual_tests import t503_offline_checks as t503
from app.manual_tests import t504_offline_checks as t504
from app.store.file_store import FileRunStore
from app.workflow import roles as roles_module
from app.workflow.harvest_stage import canonical_hash, write_json
from app.workflow.run import ArtifactRef, RunScope, utc_now
from app.workflow.states import RunState

check = t502.check
NR, MON = t503.NR, t503.MON
BI, OWNER, SEC, BOTH, NOBODY = "bi@contoso.com", "owner@contoso.com", "sec@contoso.com", "both@contoso.com", "nobody@contoso.com"


def main() -> None:
    import os

    os.environ["SANDBOX_WORKSPACE_ID"] = t504.SANDBOX
    with tempfile.TemporaryDirectory() as tmp:
        runs_root = Path(tmp) / "runs"
        roles_file = Path(tmp) / "roles.yaml"
        roles_file.write_text(
            f"roles:\n  bi_engineer: [{BI}, {BOTH}]\n  business_owner: [{OWNER}, {BOTH}]\n  security_reviewer: [{SEC}]\n", encoding="utf-8"
        )
        original_roles = roles_module.CONFIG_PATH
        roles_module.CONFIG_PATH = roles_file
        store = FileRunStore(runs_root)
        items = t504.FakeItems()
        pbi = t504.FakePbi()
        sql = t504.FakeSql()
        recon = t504.ReconLlm({NR: t504.RECON_ANSWER, MON: t504.MON_RECON_ANSWER})
        dax_llm = t503.FakeLlm()
        user = {"upn": BI}
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_runs_root] = lambda: runs_root
        app.dependency_overrides[authenticate] = lambda: AuthContext(principal=Principal(oid="o", upn=user["upn"], name="T"), token="x")
        app.dependency_overrides[get_dax_llm] = lambda: dax_llm
        app.dependency_overrides[get_items_client] = lambda: items
        app.dependency_overrides[get_powerbi_client] = lambda: pbi
        app.dependency_overrides[get_sql_connector_factory] = lambda: (lambda scope: sql)
        app.dependency_overrides[get_recon_llm] = lambda: recon
        client = TestClient(app, raise_server_exceptions=False)

        def envelope(r, status, code):
            return r.status_code == status and r.json()["error"]["code"] == code

        def new_run(validate=True, security=False):
            run = store.create("Show net revenue by product")
            spec = t503.spec_with_measures(run_id=run.run_id)
            if security:
                spec.roles = [RlsRole(name="RLS1", description="Reps see their region")]
            run_dir = runs_root / run.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            plan = t502.build_plan(spec)
            save_yaml(spec, run_dir / "ModelSpec.yaml")
            save_yaml(plan, run_dir / "ExecutionPlan.yaml")
            write_json(run_dir / "MetadataInventory.json", t502.build_inventory(spec).to_dict())
            bc.save(bc.BusinessContract(run_id=run.run_id, business_objective="Revenue", entities=["Sales"], kpis=[t503.KPI_NR, t503.KPI_MON], security=[bc.SecurityRequirement(description="Reps see their region")] if security else []), run_dir / "BusinessContract.yaml")
            run.scope = RunScope(workspace_id=t502.WS, lakehouse_id=t502.LH, lakehouse_name="SalesLakehouse", sql_endpoint=t502.ENDPOINT)
            for name, model in (("ModelSpec", spec), ("ExecutionPlan", plan)):
                run.artifacts[name] = ArtifactRef(path=f"{name}.yaml", version=1, hash=canonical_hash(to_data(model)), status="approved", updated_at=utc_now())
            run.state = RunState.PLAN_APPROVED
            store.save(run)
            user["upn"] = BI
            assert client.post(f"/runs/{run.run_id}/model/generate").status_code == 200
            dax_llm.answers = {NR: [t503.NR_ANSWER], MON: [t503.MON_ANSWER]}
            assert client.post(f"/runs/{run.run_id}/model/measures").status_code == 200
            if security:  # the role's filter, so that G4.RLS passes
                folder = run_dir / "model" / "v2" / "definition" / "roles"
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "RLS1.tmdl").write_bytes(b"role RLS1\n\tmodelPermission: read\n\n\ttablePermission Sales = 'Sales Order'[Status] <> 6\n")
            if validate:
                assert client.post(f"/runs/{run.run_id}/model/validate").json()["report"]["passed"] is True
            return run.run_id

        def review(rid):
            return client.get(f"/runs/{rid}/model/review")

        def approve(rid, as_user, role, action="ACCEPT", comment=None, expected=None, **extra):
            user["upn"] = as_user
            body = {"artifact": "SemanticModel", "action": action, "expected_hash": expected or review(rid).json()["model_hash"], "role": role, **extra}
            if comment:
                body["comment"] = comment
            return client.post(f"/runs/{rid}/approvals", json=body)

        def statuses(rid):
            return {s["role"]: s["status"] for s in review(rid).json()["approvals_status"]}

        # 1. The review
        rid = new_run()
        user["upn"] = BI
        r = review(rid)
        data = r.json()
        nr = next(m for m in data["measures"] if m["name"] == NR)
        check("review: 200 with the model hash of the deployed files", r.status_code == 200 and data["model_hash"].startswith("sha256:") and data["version"] == 2 and data["state"] == "MODEL_GENERATING")
        check("review: the Net Revenue DAX next to its approved definition",
              nr["dax"] == t503.NR_DAX and nr["kpi_definition"]["formula_intent"] == "Sum of line totals excluding cancelled orders" and nr["kpi_definition"]["exclusions"] == ["Cancelled orders have Status = 6"]
              and nr["description"].startswith("Approved: Sum of line totals"))
        check("review: the reconciliation values", nr["reconciliation"]["dax_value"] == 1000.0 and nr["reconciliation"]["sql_value"] == 1000.0 and nr["reconciliation"]["expected"] is None and nr["reconciliation"]["status"] == "pass")
        check("review: tables, relationships and the validation report", {t["name"] for t in data["tables"]} >= {"Sales", "Product", "Date"} and len(data["relationships"]) == 3
              and data["validation_report"]["passed"] is True and data["validation_current"] is True and data["gate"]["passed"] is True and data["roles"] == [])
        check("review: bi_engineer and business_owner are required, both pending, and no security_reviewer without RLS",
              data["approvals_required"] == ["bi_engineer", "business_owner"] and statuses(rid) == {"bi_engineer": "pending", "business_owner": "pending"} and data["approvals_done"] == [])

        # 2. Partial approval
        r = approve(rid, BI, "bi_engineer")
        check("approve as bi_engineer: 200, the run stays in MODEL_GENERATING", r.status_code == 200 and r.json()["run"]["state"] == "MODEL_GENERATING" and r.json()["approval"]["role"] == "bi_engineer")
        user["upn"] = BI
        check("business_owner is still pending", statuses(rid) == {"bi_engineer": "approved", "business_owner": "pending"})
        r = approve(rid, BI, "bi_engineer")
        check("approving twice as the same role: 409 ALREADY_APPROVED", envelope(r, 409, "ALREADY_APPROVED"))

        # 3. Complete
        r = approve(rid, OWNER, "business_owner")
        check("business_owner approves: MODEL_APPROVED", r.status_code == 200 and r.json()["run"]["state"] == "MODEL_APPROVED" and r.json()["gate_result"]["passed"] is True)
        run = store.get(rid)
        check("the model artifacts are approved", run.artifacts["TMDLModel"].status == "approved" and run.artifacts["ModelSpec"].status == "approved")
        user["upn"] = BI
        records = [a for a in client.get(f"/runs/{rid}/approvals").json() if a["artifact"] == "SemanticModel"]
        check("one attributable record per role, on the model hash", [(a["role"], a["user"]["upn"], a["action"]) for a in records] == [("bi_engineer", BI, "ACCEPT"), ("business_owner", OWNER, "ACCEPT")]
              and all(a["artifact_hash"] == data["model_hash"] and a["gate_result_ref"] for a in records))
        check("an approved model can't be accepted again", envelope(approve(rid, OWNER, "business_owner"), 409, "ALREADY_APPROVED"))

        # 4. One user, several roles: each is a separate action
        rid = new_run()
        r = approve(rid, BOTH, "bi_engineer")
        user["upn"] = BOTH
        check("a user with two roles approves one at a time: the other stays pending", r.status_code == 200 and statuses(rid) == {"bi_engineer": "approved", "business_owner": "pending"} and r.json()["run"]["state"] == "MODEL_GENERATING")
        r = approve(rid, BOTH, "business_owner")
        check("the second explicit action completes it", r.status_code == 200 and r.json()["run"]["state"] == "MODEL_APPROVED")

        # 5. Roles
        rid = new_run()
        check("a role the model doesn't need (security_reviewer without RLS): 422", envelope(approve(rid, SEC, "security_reviewer"), 422, "VALIDATION_ERROR"))
        check("no role given: 422", envelope(approve(rid, BI, None), 422, "VALIDATION_ERROR"))
        check("acting as a role the user doesn't hold: 403", envelope(approve(rid, NOBODY, "bi_engineer"), 403, "FORBIDDEN") and envelope(approve(rid, BI, "business_owner"), 403, "FORBIDDEN"))
        stale = review(rid).json()["model_hash"]
        check("a stale hash: 409 STALE_VERSION", envelope(approve(rid, BI, "bi_engineer", expected="sha256:old"), 409, "STALE_VERSION"))
        check("nothing was recorded by the refusals", review(rid).json()["approvals_done"] == [])

        # 6. Gate G4
        rid = new_run(validate=False)
        r = approve(rid, BI, "bi_engineer")
        check("approve before any validation: 409 GATE_FAILED (G4.VALIDATED)", envelope(r, 409, "GATE_FAILED") and "G4.VALIDATED" in r.json()["error"]["details"]["failed_checks"])
        sql.total = 900.0
        user["upn"] = BI
        assert client.post(f"/runs/{rid}/model/validate").json()["run"]["state"] == "MODEL_FAILED"
        sql.total = 1000.0
        check("a failed model can't be approved: WRONG_STATE", envelope(approve(rid, BI, "bi_engineer"), 409, "WRONG_STATE"))
        store.transition(rid, RunState.MODEL_GENERATING, "test", "t")  # as the repair service would
        r = approve(rid, BI, "bi_engineer")
        check("G4 not passing: 409 GATE_FAILED naming the failed checks", envelope(r, 409, "GATE_FAILED") and "G4.RECON" in r.json()["error"]["details"]["failed_checks"])
        rv = review(rid).json()
        recon_check = next(m for m in rv["measures"] if m["name"] == NR)["reconciliation"]
        check("the review shows the failing reconciliation, and nothing was recorded", recon_check["status"] == "fail" and recon_check["dax_value"] == 1000.0 and recon_check["sql_value"] == 900.0
              and rv["gate"]["passed"] is False and rv["approvals_done"] == [])

        # 7. Invalidation when the model changes
        rid = new_run()
        approve(rid, BI, "bi_engineer")
        user["upn"] = BI
        product = runs_root / rid / "model" / "v2" / "definition" / "tables" / "Product.tmdl"
        product.write_bytes(product.read_bytes() + b"\t// changed after the approval\n")
        rv = review(rid).json()
        done = rv["approvals_done"][0]
        check("a changed model invalidates the earlier approval, shown as such",
              statuses(rid)["bi_engineer"] == "invalidated" and done["current"] is False and done["note"] == "invalidated: model changed"
              and next(s for s in rv["approvals_status"] if s["role"] == "bi_engineer")["note"] == "invalidated: model changed" and rv["validation_current"] is False)
        r = approve(rid, OWNER, "business_owner")
        check("the new hash needs a validation first: GATE_FAILED (G4.CURRENT)", envelope(r, 409, "GATE_FAILED") and "G4.CURRENT" in r.json()["error"]["details"]["failed_checks"])
        user["upn"] = BI
        assert client.post(f"/runs/{rid}/model/validate").json()["report"]["passed"] is True
        approve(rid, OWNER, "business_owner")
        check("the owner's approval on the new hash alone doesn't complete it: bi_engineer must approve again", store.get(rid).state is RunState.MODEL_GENERATING and statuses(rid) == {"bi_engineer": "invalidated", "business_owner": "approved"})
        r = approve(rid, BI, "bi_engineer")
        check("approving again on the current hash completes it", r.status_code == 200 and r.json()["run"]["state"] == "MODEL_APPROVED")

        # 8. A model with RLS roles needs the security reviewer too
        rid = new_run(security=True)
        rv = review(rid).json()
        check("RLS roles: the security_reviewer is required and the roles are listed", rv["approvals_required"] == ["bi_engineer", "business_owner", "security_reviewer"] and rv["roles"][0]["name"] == "RLS1")
        approve(rid, BI, "bi_engineer")
        approve(rid, OWNER, "business_owner")
        check("two of three approvals leave the run in MODEL_GENERATING", store.get(rid).state is RunState.MODEL_GENERATING and statuses(rid)["security_reviewer"] == "pending")
        r = approve(rid, SEC, "security_reviewer")
        check("the third approval completes it", r.status_code == 200 and r.json()["run"]["state"] == "MODEL_APPROVED")

        # 9. Reject
        rid = new_run()
        check("a Reject needs a comment: 422", envelope(approve(rid, BI, "bi_engineer", action="REJECT"), 422, "VALIDATION_ERROR"))
        r = approve(rid, BI, "bi_engineer", action="REJECT", comment="The date relationship is wrong")
        check("a Reject is recorded as the role's decision and the run stays", r.status_code == 200 and statuses(rid)["bi_engineer"] == "rejected" and r.json()["run"]["state"] == "MODEL_GENERATING" and r.json()["gate_result"] is None)
        check("definition_wrong is only for a business_owner's Reject", envelope(approve(rid, BI, "bi_engineer", action="REJECT", comment="x", definition_wrong=True), 422, "VALIDATION_ERROR"))
        contract_before = (runs_root / rid / "BusinessContract.yaml").read_bytes()
        r = approve(rid, OWNER, "business_owner", action="REJECT", comment="Net Revenue should also exclude returns", definition_wrong=True)
        check("a business_owner's Reject of the definition sends the run to NEEDS_INPUT", r.status_code == 200 and r.json()["run"]["state"] == "NEEDS_INPUT" and "KPI definition is wrong" in store.get(rid).history[-1].reason)
        check("and it can't change the KPI", (runs_root / rid / "BusinessContract.yaml").read_bytes() == contract_before)

        # 10. Errors outside
        rid = new_run()
        check("EXPLAIN isn't available for the model: 422", envelope(approve(rid, BI, "bi_engineer", action="EXPLAIN"), 422, "VALIDATION_ERROR"))
        no_model = store.create("x").run_id
        check("review before the model exists: 404", envelope(review(no_model), 404, "NOT_FOUND"))
        check("unknown run: 404", envelope(review("nope"), 404, "RUN_NOT_FOUND"))

        roles_module.CONFIG_PATH = original_roles
        app.dependency_overrides.clear()

    print("All checks passed.")


if __name__ == "__main__":
    main()
