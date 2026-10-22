"""T-505 offline checks: the repair policy, fingerprints, the `repair` graph, the scope guard and
`POST /runs/{id}/model/repair`.

Run: python -m app.manual_tests.t505_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
Fabric, Power BI, the SQL endpoint and the model are fakes (extending the fixtures of t503 and
t504), and the run is written by hand into a temporary folder, so no sign-in or network is needed.
The live steps are in MANUAL_TESTING.md.
"""

import json
import os
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
from app.compiler.critical_terms import ConfigError
from app.connectors.powerbi_api import DaxError, RefreshFailed
from app.contracts import business_contract as bc
from app.contracts.model_plan import ModelSpec, load_yaml, save_yaml, to_data
from app.contracts.validation_report import ValidationCheck, fingerprint, normalize_message
from app.deploy.fabric_items import FabricOperationFailed
from app.graphs.repair import build_repair_graph
from app.manual_tests import t502_offline_checks as t502
from app.manual_tests import t503_offline_checks as t503
from app.manual_tests import t504_offline_checks as t504
from app.repair.model_adapter import read_tree
from app.repair.policy import load_policy, missing_column, repair_code
from app.repair.service import UnsupportedArtifact, repair
from app.workflow.compile_stage import WrongState
from app.store.file_store import FileRunStore
from app.workflow.harvest_stage import canonical_hash, write_json
from app.workflow.run import ArtifactRef, RunScope, utc_now
from app.workflow.states import RunState

check = t502.check
NR, MON = t503.NR, t503.MON
SANDBOX = t504.SANDBOX
GREEK = {2: "Alpha", 3: "Beta", 4: "Gamma", 5: "Delta", 6: "Epsilon"}


class RepairItems(t504.FakeItems):
    """Rejects a deployment whose files hold a given text, like Fabric rejecting a bad format string."""

    def __init__(self):
        super().__init__()
        self.reject_text: str | None = None

    def upsert_semantic_model(self, workspace_id, display_name, tmdl_dir):
        if self.reject_text:
            text = "".join(p.read_text(encoding="utf-8") for p in (Path(tmdl_dir) / "definition" / "tables").glob("*.tmdl"))
            if self.reject_text in text:
                self.calls.append((workspace_id, display_name, Path(tmdl_dir)))
                raise FabricOperationFailed("Invalid format string for measure 'Net Revenue'", "req-3", {"errorCode": "InvalidFormatString"})
        return super().upsert_semantic_model(workspace_id, display_name, tmdl_dir)


class RepairPbi(t504.FakePbi):
    """Fails Net Revenue while the deployed files hold the typo, or in a scripted way."""

    def __init__(self, items):
        super().__init__()
        self.items = items
        self.vary = False  # a different error message for every version, so each attempt shows "progress"
        self.constant = None  # the same error whatever is deployed

    def execute_dax(self, workspace_id, dataset_id, dax, impersonate=None):
        folder = self.items.calls[-1][2]
        if "COUNTROWS" not in dax and f"[{NR}]" in dax and "CALCULATE" not in dax:
            text = "".join(p.read_text(encoding="utf-8") for p in (Path(folder) / "definition" / "tables").glob("*.tmdl"))
            if self.constant:
                raise DaxError(self.constant)
            if self.vary:
                raise DaxError(f"Column '{GREEK[int(Path(folder).name[1:])]}' not found in table 'Sales'")
            if "LineTotalX" in text:
                raise DaxError("Column 'LineTotalX' not found in table 'Sales'")
        return super().execute_dax(workspace_id, dataset_id, dax, impersonate)


def main() -> None:
    os.environ["SANDBOX_WORKSPACE_ID"] = SANDBOX

    # --- fingerprints and policy ----------------------------------------------------------------
    check("fingerprint: numbers and ids don't change it",
          fingerprint("G4.RECON", "measure:A", "RECON_MISMATCH", "DAX value 1000.5 differs from the SQL value 900 by 100.5")
          == fingerprint("G4.RECON", "measure:A", "RECON_MISMATCH", "DAX value 7 differs from the SQL value 3 by 4"))
    check("fingerprint: another object or code changes it", fingerprint("G4.DAX", "measure:A", "X", "m") != fingerprint("G4.DAX", "measure:B", "X", "m")
          and fingerprint("G4.DAX", "measure:A", "X", "m") != fingerprint("G4.DAX", "measure:A", "Y", "m"))
    check("normalize: GUIDs and numbers", normalize_message("req 3f2b1c4d-aaaa-bbbb-cccc-1234567890ab row 12.5") == "req # row #")
    policy = load_policy()
    check("policy: max_attempts and classes from config/repair_policy.yaml", policy.max_attempts == 3 and policy.classify("DAX_REFERENCE") == "llm"
          and policy.classify("FORMAT_STRING_INVALID") == "deterministic" and policy.classify("SOURCE_COLUMN_MISSING") == "user_input" and policy.classify("UNHEARD_OF") == "user_input")
    with tempfile.TemporaryDirectory() as d:
        bad = Path(d) / "p.yaml"
        bad.write_text("max_attempts: 0\npolicy: {}\n", encoding="utf-8")
        try:
            load_policy(bad)
            ok = False
        except ConfigError as exc:
            ok = "max_attempts" in str(exc)
        bad.write_text("max_attempts: 3\npolicy: {A: magic}\n", encoding="utf-8")
        try:
            load_policy(bad)
            ok2 = False
        except ConfigError as exc:
            ok2 = "magic" not in str(exc) and "A must be one of" in str(exc)
        check("policy: invalid files are ConfigErrors", ok and ok2)

    def failed(id_, code, message, obj="measure:X"):
        return ValidationCheck(id=id_, object=obj, status="fail", severity="error", code=code, message=message, fingerprint=fingerprint(id_, obj, code, message))

    check("repair codes: references, syntax, relationships, format, refresh", [
        repair_code(failed("G4.REF", "UNKNOWN_REFERENCE", "column 'A' not found")),
        repair_code(failed("G4.DAX", "DAX_ERROR", "Column 'A' not found in table 'Sales'")),
        repair_code(failed("G4.DAX", "DAX_ERROR", "A single value was expected near '+'")),
        repair_code(failed("G4.REL", "RELATIONSHIP_INVALID", "3 duplicate key value(s)")),
        repair_code(failed("G4.DAX", "DEPLOY_FAILED", "Fabric rejected the model: Invalid format string")),
        repair_code(failed("G4.DAX", "REFRESH_FAILED", "The refresh failed: Column 'Qty' not found in table 'T'")),
        repair_code(failed("G4.DAX", "REFRESH_FAILED", "The refresh failed: capacity paused")),
        repair_code(failed("G4.RECON", "RECON_MISMATCH", "differs")),
    ] == ["DAX_REFERENCE", "DAX_REFERENCE", "DAX_SYNTAX", "REL_DUPLICATE_KEYS", "FORMAT_STRING_INVALID", "SOURCE_COLUMN_MISSING", "REFRESH_FAILED", "RECON_MISMATCH"])
    check("the missing column is named", missing_column("The refresh failed: Column 'Qty' not found in table 'T'") == "Qty")

    # --- runs ------------------------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        runs_root = Path(tmp) / "runs"
        store = FileRunStore(runs_root)
        items = RepairItems()
        pbi = RepairPbi(items)
        sql = t504.FakeSql()
        recon = t504.ReconLlm({NR: t504.RECON_ANSWER, MON: t504.MON_RECON_ANSWER})
        dax_llm = t503.FakeLlm()
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_runs_root] = lambda: runs_root
        app.dependency_overrides[authenticate] = lambda: AuthContext(principal=Principal(oid="o", upn="t@contoso.com", name="T"), token="x")
        app.dependency_overrides[get_dax_llm] = lambda: dax_llm
        app.dependency_overrides[get_items_client] = lambda: items
        app.dependency_overrides[get_powerbi_client] = lambda: pbi
        app.dependency_overrides[get_sql_connector_factory] = lambda: (lambda scope: sql)
        app.dependency_overrides[get_recon_llm] = lambda: recon
        client = TestClient(app, raise_server_exceptions=False)

        def envelope(r, status, code):
            return r.status_code == status and r.json()["error"]["code"] == code

        def reset():
            items.reject_text, items.error = None, None
            pbi.vary, pbi.constant, pbi.refresh_error = False, None, None
            sql.total = 1000.0
            dax_llm.answers = {NR: [t503.NR_ANSWER], MON: [t503.MON_ANSWER]}
            dax_llm.calls.clear()

        def new_run(measure_format=None):
            run = store.create("Show net revenue by product")
            spec = t503.spec_with_measures(run_id=run.run_id)
            spec.measures[0].format_string = measure_format
            run_dir = runs_root / run.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            plan = t502.build_plan(spec)
            save_yaml(spec, run_dir / "ModelSpec.yaml")
            save_yaml(plan, run_dir / "ExecutionPlan.yaml")
            write_json(run_dir / "MetadataInventory.json", t502.build_inventory(spec).to_dict())
            bc.save(bc.BusinessContract(run_id=run.run_id, business_objective="Revenue", entities=["Sales"], kpis=[t503.KPI_NR, t503.KPI_MON]), run_dir / "BusinessContract.yaml")
            run.scope = RunScope(workspace_id=t502.WS, lakehouse_id=t502.LH, lakehouse_name="SalesLakehouse", sql_endpoint=t502.ENDPOINT)
            for name, model in (("ModelSpec", spec), ("ExecutionPlan", plan)):
                run.artifacts[name] = ArtifactRef(path=f"{name}.yaml", version=1, hash=canonical_hash(to_data(model)), status="approved", updated_at=utc_now())
            run.state = RunState.PLAN_APPROVED
            store.save(run)
            assert client.post(f"/runs/{run.run_id}/model/generate").status_code == 200
            reset()
            assert client.post(f"/runs/{run.run_id}/model/measures").status_code == 200
            return run.run_id

        def sales_file(rid, v):
            return runs_root / rid / "model" / f"v{v}" / "definition" / "tables" / "Sales.tmdl"

        def broken_run(typo=True, extra_edit=False, measure_format=None):
            rid = new_run(measure_format)
            if typo:
                path = sales_file(rid, 2)
                path.write_bytes(path.read_bytes().replace(b"Sales[LineTotal]", b"Sales[LineTotalX]"))
            if extra_edit:
                product = runs_root / rid / "model" / "v2" / "definition" / "tables" / "Product.tmdl"
                product.write_bytes(product.read_bytes() + b"\t// edited by hand\n")
            return rid

        def validate(rid):
            return client.post(f"/runs/{rid}/model/validate")

        def do_repair(rid):
            return client.post(f"/runs/{rid}/model/repair")

        # 1. Targeted repair: one attempt, only that measure's table file changes
        rid = broken_run()
        r = validate(rid)
        check("setup: the typo fails validation and the run is MODEL_FAILED", r.json()["run"]["state"] == "MODEL_FAILED")
        history = len(store.get(rid).history)
        llm_calls = len(dax_llm.calls)
        r = do_repair(rid)
        body = r.json()
        check("repair: 200, one attempt, passed, state MODEL_GENERATING", r.status_code == 200 and len(body["attempts"]) == 1 and body["outcome"] == "passed"
              and body["final_state"] == "MODEL_GENERATING" and store.get(rid).state is RunState.MODEL_GENERATING)
        attempt = body["attempts"][0]
        check("the attempt records what it fixed, what remains and the version", len(attempt["fixed"]) == 2 and attempt["remaining"] == [] and attempt["version"] == 3 and not attempt["discarded"])
        v2, v3 = runs_root / rid / "model" / "v2", runs_root / rid / "model" / "v3"
        a, b = read_tree(v2), read_tree(v3)
        check("only the failing measure's table file changed between v2 and v3", {n for n in a.keys() | b.keys() if a.get(n) != b.get(n)} == {"definition/tables/Sales.tmdl"})
        check("the model was asked once, with the failure as feedback", len(dax_llm.calls) - llm_calls == 1 and "LineTotalX" in dax_llm.calls[-1][1] and "failed validation" in dax_llm.calls[-1][1])
        run = store.get(rid)
        check("artifacts follow: TMDLModel v3, ModelSpec unchanged (the files were restored from it), report passed",
              run.artifacts["TMDLModel"].version == 3 and run.artifacts["TMDLModel"].path == "model/v3" and run.artifacts["ModelSpec"].version == 2
              and run.artifacts["ModelSpec"].status == "approved" and run.artifacts["ModelSpec"].hash == canonical_hash(to_data(load_yaml(ModelSpec, runs_root / rid / "ModelSpec.yaml")))
              and run.artifacts["ValidationReport"].status == "passed")
        check("the graph made no transition of its own: one transition for the whole call", len(run.history) - history == 1 and run.history[-1].to is RunState.MODEL_GENERATING)
        report = json.loads((runs_root / rid / "ValidationReport.json").read_text(encoding="utf-8"))
        runs_ = report["runs"]
        check("ValidationReport.json: the failed run, then the repair attempt with its number, version and fingerprints",
              len(runs_) == 2 and runs_[0]["passed"] is False and runs_[1]["passed"] is True and runs_[1]["repair_attempt"] == 1 and runs_[1]["artifact_version"] == 3
              and runs_[0]["repair_attempt"] is None and all(f in [c["fingerprint"] for c in runs_[0]["checks"]] for f in attempt["fixed"]))
        check("the repaired measure passes validation again", validate(rid).json()["report"]["passed"] is True)

        # 2. Escalation: a missing source column is user input, no model call
        reset()
        rid = new_run()
        pbi.refresh_error = RefreshFailed("The refresh failed: Column 'Qty' not found in table 'SalesLT_SalesOrderDetail'")
        validate(rid)
        pbi.refresh_error = None
        calls, history = len(dax_llm.calls), len(store.get(rid).history)
        r = do_repair(rid)
        body = r.json()
        check("a source column that no longer exists escalates: no attempts, no model call, NEEDS_INPUT",
              r.status_code == 200 and body["attempts"] == [] and body["outcome"] == "user_input_only" and body["final_state"] == "NEEDS_INPUT" and len(dax_llm.calls) == calls)
        check("the message names the column", "Qty" in body["message"] and "need your input" in body["message"])
        run = store.get(rid)
        check("NEEDS_INPUT with resume_state MODEL_FAILED, one transition, no new version",
              run.state is RunState.NEEDS_INPUT and run.resume_state is RunState.MODEL_FAILED and len(run.history) - history == 1 and not (runs_root / rid / "model" / "v3").exists())

        # 3. The retry limit: a different failure after every attempt counts as progress, but the limit stops it
        reset()
        rid = new_run()
        pbi.vary = True
        validate(rid)
        refs = [t503.col("Sales", "LineTotal"), t503.col("Sales Order", "Status")]
        dax_llm.answers[NR] = [t503.answer(NR, t503.NR_DAX + f"\n-- attempt {n}", refs) for n in (1, 2, 3)]  # a new measure text each time
        history = len(store.get(rid).history)
        r = do_repair(rid)
        body = r.json()
        check("a failure the model can't fix stops at 3 attempts and escalates", len(body["attempts"]) == 3 and body["outcome"] == "limit_reached" and body["final_state"] == "NEEDS_INPUT")
        check("each attempt wrote a version (v3 to v5) and the message says what remains", [a["version"] for a in body["attempts"]] == [3, 4, 5] and "attempt limit" in body["message"])
        check("a new fingerprint each time is progress; every attempt is in the report with its number",
              [x["repair_attempt"] for x in json.loads((runs_root / rid / "ValidationReport.json").read_text(encoding="utf-8"))["runs"]] == [None, 1, 2, 3])
        run = store.get(rid)
        check("only the outcome's transition was made, not one per attempt", len(run.history) - history == 1 and run.history[-1].to is RunState.NEEDS_INPUT and run.resume_state is RunState.MODEL_FAILED)
        check("artifacts point at the last version", run.artifacts["TMDLModel"].version == 5 and run.artifacts["ValidationReport"].status == "failed")

        # 4. No progress: the same failure after an attempt stops early
        reset()
        rid = new_run()
        pbi.constant = "Syntax error near 'x' at position 5"
        validate(rid)
        r = do_repair(rid)
        body = r.json()
        check("the same failure after an attempt stops with no_progress after 1 attempt", len(body["attempts"]) == 1 and body["outcome"] == "no_progress" and body["final_state"] == "NEEDS_INPUT")

        # 5. A reconciliation mismatch the model can't change: the same DAX again is no change
        reset()
        rid = new_run()
        sql.total = 900.0
        validate(rid)
        r = do_repair(rid)
        body = r.json()
        check("a regenerated measure that doesn't change is a discarded attempt, then no_progress", body["outcome"] == "no_progress" and body["attempts"][0]["discarded"] is True
              and "no fix could be produced" in (body["attempts"][0]["note"] or "") and not (runs_root / rid / "model" / "v3").exists())
        sql.total = 1000.0

        # 6. The scope guard: a change outside the failing objects drops the attempt
        reset()
        rid = broken_run(extra_edit=True)
        validate(rid)
        calls = len(dax_llm.calls)
        r = do_repair(rid)
        body = r.json()
        attempt = body["attempts"][0]
        check("an attempt that changes another table's file is discarded and counted", attempt["discarded"] is True and "Product.tmdl" in attempt["note"]
              and body["outcome"] == "no_progress" and len(dax_llm.calls) - calls == 1)
        check("a discarded attempt writes nothing: no v3, no repair staging folder, ModelSpec unchanged",
              not (runs_root / rid / "model" / "v3").exists() and not (runs_root / rid / "model" / ".repair.tmp").exists()
              and load_yaml(ModelSpec, runs_root / rid / "ModelSpec.yaml").version == 2)
        check("the hand edit is still there, so nothing was lost", b"edited by hand" in (runs_root / rid / "model" / "v2" / "definition" / "tables" / "Product.tmdl").read_bytes())

        # 7. A model that can't produce acceptable DAX
        reset()
        rid = new_run()
        sql.total = 900.0
        validate(rid)
        sql.total = 1000.0
        dax_llm.answers = {NR: [json.dumps({"measure_name": NR, "dax": "SUM(Sales[Nope])", "referenced_objects": [t503.col("Sales", "Nope")], "explanation": ""})], MON: [t503.MON_ANSWER]}
        body = do_repair(rid).json()
        check("DAX the pre-check rejects changes nothing: discarded, with the reason", body["attempts"][0]["discarded"] is True and "rejected" in body["attempts"][0]["note"] and body["outcome"] == "no_progress")

        # 8. A deterministic fix: no model call
        reset()
        rid = new_run(measure_format="$#,0.00")
        items.reject_text = "formatString: $#,0.00"
        validate(rid)
        check("setup: an invalid format string is rejected at deployment", store.get(rid).state is RunState.MODEL_FAILED and "format string" in json.dumps(json.loads((runs_root / rid / "ValidationReport.json").read_text(encoding="utf-8"))))
        calls = len(dax_llm.calls)
        r = do_repair(rid)
        body = r.json()
        a, b = read_tree(runs_root / rid / "model" / "v2"), read_tree(runs_root / rid / "model" / "v3")
        check("a format string error is fixed by code: passed, no model call, only the measure's table file changed",
              body["outcome"] == "passed" and len(body["attempts"]) == 1 and len(dax_llm.calls) == calls
              and {n for n in a.keys() | b.keys() if a.get(n) != b.get(n)} == {"definition/tables/Sales.tmdl"} and b"formatString: $#,0.00" not in b["definition/tables/Sales.tmdl"])
        check("the ModelSpec lost only that format string", load_yaml(ModelSpec, runs_root / rid / "ModelSpec.yaml").measures[0].format_string is None)
        items.reject_text = None

        # 9. Graph path
        reset()
        rid = broken_run()
        validate(rid)
        from app.graphs.base import run_graph
        from app.repair.model_adapter import ModelRepairAdapter
        from app.workflow.validate_stage import Environment

        env = Environment(store, runs_root, rid)
        run = store.get(rid)
        adapter = ModelRepairAdapter(env, 2, runs_root / rid / "model" / "v2", items_client=items, pbi=pbi, sql_connector_factory=lambda s: sql, recon_llm=recon)
        report = json.loads((runs_root / rid / "ValidationReport.json").read_text(encoding="utf-8"))
        errors = [ValidationCheck(**c) for c in report["runs"][-1]["checks"] if c["status"] == "fail"]
        final = run_graph(build_repair_graph(dax_llm), {"run_id": rid, "artifact": "SemanticModel", "adapter": adapter, "policy": policy, "version": 2, "errors": errors, "attempt": 0, "attempts": [], "seen_fingerprints": []},
                          run_id=rid, stage="repair", recursion_limit=30)
        check("graph path of a passing attempt", final["node_path"] == ["classify", "deterministic_fix", "llm_fix", "scope_guard", "validate", "check_progress"] and final["outcome"] == "passed")
        check("the graph itself never changed the run state", store.get(rid).state is RunState.MODEL_FAILED)

        # 10. Errors outside the loop
        reset()
        rid = new_run()
        validate(rid)
        check("repair of a run that isn't MODEL_FAILED: 409 WRONG_STATE", envelope(do_repair(rid), 409, "WRONG_STATE"))
        store.transition(rid, RunState.MODEL_FAILED, "test", "t")
        r = do_repair(rid)
        check("nothing failed: 409 'nothing to repair'", envelope(r, 409, "WRONG_STATE") and "Nothing to repair" in r.json()["error"]["message"])
        try:
            repair(store, runs_root, rid, "Report", llm=dax_llm, items_client=items, pbi=pbi, sql_connector_factory=lambda s: sql, recon_llm=recon)
            raised = None
        except UnsupportedArtifact as exc:
            raised = exc
        except WrongState as exc:  # T-603 added the Report adapter: it only repairs a run in REPORT_FAILED
            raised = exc
        check("a report is repaired only in REPORT_FAILED (T-603): another state is a WrongState", isinstance(raised, WrongState) and "REPORT_FAILED" in str(raised))
        check("unknown run: 404", envelope(do_repair("nope"), 404, "RUN_NOT_FOUND"))
        app.dependency_overrides.clear()

    print("All checks passed.")


if __name__ == "__main__":
    main()
