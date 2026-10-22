"""T-603 offline checks: the Gate G5 checks, `POST /runs/{id}/report/validate` and `/report/repair`.

Run: python -m app.manual_tests.t603_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
Fabric and Power BI are fakes (queries, exports, publishing), and the run, plan and approved model
are written by hand into a temporary folder, so no sign-in or network is needed. A real workspace
is in MANUAL_TESTING.md.
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
    get_report_llm,
    get_runs_root,
    get_sql_connector_factory,
    get_store,
)
from app.api.main import app
from app.api.security import AuthContext, Principal, authenticate
from app.connectors.fabric_api import FabricTimeout
from app.connectors.powerbi_api import DaxError, ExportUnavailable
from app.contracts import business_contract as bc
from app.contracts.model_plan import save_yaml, to_data
from app.contracts.validation_report import load_report
from app.deploy.fabric_items import FabricOperationFailed
from app.manual_tests import t501_offline_checks as t501  # noqa: F401  (keeps the fakes' module imported once)
from app.manual_tests import t502_offline_checks as t502
from app.manual_tests import t601_offline_checks as t601
from app.manual_tests import t602_offline_checks as t602
from app.report.pbir_reader import read_report
from app.report.validators import build_query, object_of
from app.repair.report_adapter import read_tree
from app.store.file_store import FileRunStore
from app.workflow.harvest_stage import canonical_hash
from app.workflow.run import ArtifactRef, utc_now
from app.workflow.states import RunState

check = t502.check
SANDBOX = t602.SANDBOX
PNG = b"\x89PNG\r\n\x1a\nfake-page-image"


class FakePbi:
    """executeQueries and the page export of Power BI."""

    def __init__(self):
        self.queries: list[str] = []
        self.broken: set[str] = set()  # measure names whose query fails in the engine
        self.empty: set[str] = set()  # measure names whose query returns no rows
        self.export_mode = "ok"  # ok | unavailable | timeout
        self.exports: list[str] = []

    def execute_dax(self, workspace_id, dataset_id, dax, impersonate=None):
        assert workspace_id == SANDBOX and dataset_id == "model-1"
        self.queries.append(dax)
        for name in self.broken:
            if f"[{name}]" in dax:
                raise DaxError(f"Failed to resolve the expression of measure '{name}'")
        if any(f"[{name}]" in dax for name in self.empty):
            return []
        return [{"[Net Revenue]": 1.0}]

    def export_report_page(self, workspace_id, report_id, page_name, timeout_s=300):
        self.exports.append(page_name)
        if self.export_mode == "unavailable":
            raise ExportUnavailable("Capacity not supported")
        if self.export_mode == "timeout":
            raise FabricTimeout("The export did not finish in time")
        return PNG + page_name.encode()


def main() -> None:
    os.environ["SANDBOX_WORKSPACE_ID"] = SANDBOX
    spec = t502.build_spec(dax=True)

    # --- objects and queries -------------------------------------------------------------------------
    check("object_of: visual, page and report files",
          object_of("definition/pages/p1/visuals/v3/visual.json") == "visual:v3" and object_of("definition/pages/p2/page.json") == "page:p2"
          and object_of("definition/version.json") == "report" and object_of("definition.pbir") == "report")

    with tempfile.TemporaryDirectory() as tmp:
        runs_root = Path(tmp) / "runs"
        store = FileRunStore(runs_root)
        llm = t601.FakeLlm(t601.good_answer())
        items, pbi = t602.FakeItems(), FakePbi()
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_runs_root] = lambda: runs_root
        app.dependency_overrides[get_report_llm] = lambda: llm
        app.dependency_overrides[get_items_client] = lambda: items
        app.dependency_overrides[get_powerbi_client] = lambda: pbi
        for dependency in (get_sql_connector_factory, get_dax_llm, get_recon_llm):
            app.dependency_overrides[dependency] = lambda: None
        app.dependency_overrides[authenticate] = lambda: AuthContext(principal=Principal(oid="o", upn="t@contoso.com", name="T"), token="x")
        client = TestClient(app, raise_server_exceptions=False)

        def envelope(r, status, code):
            return r.status_code == status and r.json()["error"]["code"] == code

        def new_run(state=RunState.REPORT_GENERATING, generated=True):
            run = store.create("Show net revenue by product")
            run_dir = runs_root / run.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            bc.save(t601.contract(run.run_id), run_dir / "BusinessContract.yaml")
            model = t502.build_spec(run_id=run.run_id, dax=True)
            save_yaml(model, run_dir / "ModelSpec.yaml")
            run.artifacts["ModelSpec"] = ArtifactRef(path="ModelSpec.yaml", version=1, hash=canonical_hash(to_data(model)), status="approved", updated_at=utc_now())
            run.artifacts["TMDLModel"] = ArtifactRef(path="model/v1", version=1, hash="sha256:tmdl", status="approved", updated_at=utc_now())
            run.artifacts["BusinessContract"] = ArtifactRef(path="BusinessContract.yaml", version=1, hash="sha256:contract", status="approved", updated_at=utc_now())
            run.state = RunState.MODEL_APPROVED
            store.save(run)
            if generated:
                assert client.post(f"/runs/{run.run_id}/report/plan").status_code == 200
                # a page filter on 2024, as the plan of the manual test has
                path = run_dir / "ReportPlan.json"
                data = json.loads(path.read_text(encoding="utf-8"))
                data["pages"][0]["filters"] = [{"table": "Date", "column": "Year", "values": [2024]}]
                path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                assert client.post(f"/runs/{run.run_id}/report/generate").status_code == 200
            run = store.get(run.run_id)
            run.state = state
            store.save(run)
            return run.run_id

        def visual_file(run_id, version=1, vid="v1"):
            return runs_root / run_id / "report" / f"v{version}" / "definition" / "pages" / "p1" / "visuals" / vid / "visual.json"

        def reset():
            items.calls.clear()
            pbi.queries.clear()
            pbi.exports.clear()
            pbi.broken.clear()
            pbi.empty.clear()
            pbi.export_mode = "ok"

        # --- a passing report ---------------------------------------------------------------------------
        run_id = new_run()
        reset()
        r = client.post(f"/runs/{run_id}/report/validate")
        body = r.json()
        entry = body["report"]
        by_id = {}
        for c in entry["checks"]:
            by_id.setdefault(c["id"], []).append(c)
        check("POST report/validate: 200, a Report entry that passed, the run stays REPORT_GENERATING",
              r.status_code == 200 and entry["artifact"] == "Report" and entry["passed"] and entry["artifact_version"] == 1
              and body["run"]["state"] == "REPORT_GENERATING" and store.get(run_id).state == RunState.REPORT_GENERATING)
        check("G5.SCHEMA passes, G5.BIND and G5.QUERY pass for each visual, G5.RENDER passes for the page",
              [c["status"] for c in by_id["G5.SCHEMA"]] == ["pass"]
              and sorted((c["object"], c["status"]) for c in by_id["G5.BIND"]) == [("visual:v1", "pass"), ("visual:v2", "pass")]
              and sorted((c["object"], c["status"]) for c in by_id["G5.QUERY"]) == [("visual:v1", "pass"), ("visual:v2", "pass")]
              and [(c["object"], c["status"]) for c in by_id["G5.RENDER"]] == [("page:p1", "pass")])
        check("a PNG per page is saved in renders/", (runs_root / run_id / "report" / "v1" / "renders" / "p1.png").read_bytes() == PNG + b"p1")
        q1 = next(c for c in by_id["G5.QUERY"] if c["object"] == "visual:v1")["evidence"]["query"]
        q2 = next(c for c in by_id["G5.QUERY"] if c["object"] == "visual:v2")["evidence"]["query"]
        check("the line chart's query groups by the shown hierarchy level, applies the 2024 filter and asks for the measure",
              q1 == "EVALUATE\nTOPN(100, SUMMARIZECOLUMNS('Date'[Year], TREATAS({2024}, 'Date'[Year]), \"Net Revenue\", [Net Revenue]))")
        check("the bar chart's query groups by its column and carries the page filter",
              "'Product'[Product Name]" in q2 and "TREATAS({2024}, 'Date'[Year])" in q2 and q2.startswith("EVALUATE\nTOPN(100, SUMMARIZECOLUMNS("))
        ref = store.get(run_id).artifacts
        check("the ValidationReport artifact is registered (passed) on the report's hash; the PbirReport hash ignores renders",
              ref["ValidationReport"].status == "passed" and "PbirReport" in ref["ValidationReport"].based_on
              and ref["PbirReport"].hash == entry["artifact_hash"])
        saved = load_report(runs_root / run_id / "ValidationReport.json", run_id)
        check("ValidationReport.json holds the entry; the model's readers see no model entry", saved.latest("Report").passed and saved.latest("SemanticModel") is None)
        check("the report was published as part of the validation (updated, one report)", len(items.calls) == 1 and items.calls[0][1].endswith("_Report"))

        # --- queries ------------------------------------------------------------------------------------------
        reset()
        pbi.empty = {"Net Revenue"}
        entry = client.post(f"/runs/{run_id}/report/validate").json()["report"]
        nodata = [c for c in entry["checks"] if c["id"] == "G5.QUERY"]
        check("a query with no rows is a G5.QUERY warning 'no data', and the report still passes",
              entry["passed"] and all(c["status"] == "warning" and c["code"] == "NO_DATA" and "no data" in c["message"] for c in nodata))
        reset()
        pbi.export_mode = "unavailable"
        entry = client.post(f"/runs/{run_id}/report/validate").json()["report"]
        render = [c for c in entry["checks"] if c["id"] == "G5.RENDER"]
        check("export unavailable: G5.RENDER is a warning 'export unavailable', not a pass, and the other checks decide",
              entry["passed"] and render and all(c["status"] == "warning" and "export unavailable" in c["message"] for c in render))
        reset()
        pbi.export_mode = "timeout"
        entry = client.post(f"/runs/{run_id}/report/validate").json()["report"]
        render = [c for c in entry["checks"] if c["id"] == "G5.RENDER"]
        check("export times out: G5.RENDER is a warning, the report passes", entry["passed"] and render[0]["status"] == "warning" and render[0]["code"] == "RENDER_TIMEOUT")

        # --- a binding failure, repaired narrowly ------------------------------------------------------------------
        reset()
        bad = new_run()
        path = visual_file(bad)
        path.write_text(path.read_text(encoding="utf-8").replace('"Net Revenue"', '"Net Revenue X"'), encoding="utf-8")
        publishes_before = len(items.calls)
        r = client.post(f"/runs/{bad}/report/validate")
        entry = r.json()["report"]
        failed = [c for c in entry["checks"] if c["status"] == "fail"]
        check("a binding to a missing measure: G5.BIND fails naming v1 and the measure; the run is REPORT_FAILED",
              r.status_code == 200 and not entry["passed"] and [(c["id"], c["object"]) for c in failed] == [("G5.BIND", "visual:v1")]
              and "Net Revenue X" in failed[0]["message"] and failed[0]["evidence"]["defect"] == "report" and store.get(bad).state == RunState.REPORT_FAILED)
        skipped_ids = {(c["id"], c["object"]) for c in entry["checks"] if c["status"] == "skipped"}
        check("the broken visual's query is skipped, the report isn't published and its pages aren't rendered",
              ("G5.QUERY", "visual:v1") in skipped_ids and ("G5.RENDER", "page:p1") in skipped_ids and len(items.calls) == publishes_before and not pbi.exports)
        before = read_tree(runs_root / bad / "report" / "v1")
        r = client.post(f"/runs/{bad}/report/repair")
        body = r.json()
        after = read_tree(runs_root / bad / "report" / "v2")
        changed = sorted(n for n in before.keys() | after.keys() if before.get(n) != after.get(n))
        check("repair: passed in one attempt, the run is back in REPORT_GENERATING",
              r.status_code == 200 and body["outcome"] == "passed" and body["final_state"] == "REPORT_GENERATING" and len(body["attempts"]) == 1
              and not body["attempts"][0]["discarded"])
        check("only v1's visual.json changes between the versions (the Report binding failure scenario)",
              changed == ["definition/pages/p1/visuals/v1/visual.json"])
        latest = load_report(runs_root / bad / "ValidationReport.json", bad).latest("Report")
        run = store.get(bad)
        check("the new version validates: a passing Report entry marked as repair attempt 1, PbirReport is v2, the repaired report was published",
              latest.passed and latest.repair_attempt == 1 and latest.artifact_version == 2 and run.artifacts["PbirReport"].version == 2
              and run.artifacts["PbirReport"].hash == latest.artifact_hash and len(items.calls) == publishes_before + 1)
        check("the repaired file is the plan's binding again", "Net Revenue X" not in visual_file(bad, 2).read_text(encoding="utf-8"))

        # --- a model defect is routed back -----------------------------------------------------------------------------
        reset()
        broken = new_run()
        pbi.broken = {"Net Revenue"}
        r = client.post(f"/runs/{broken}/report/validate")
        entry = r.json()["report"]
        failed = [c for c in entry["checks"] if c["status"] == "fail"]
        run = store.get(broken)
        check("a measure that errors at query time: G5.QUERY fails with defect 'model', and the run goes to MODEL_GENERATING with the reason",
              not entry["passed"] and failed and all(c["id"] == "G5.QUERY" and c["evidence"]["defect"] == "model" for c in failed)
              and run.state == RunState.MODEL_GENERATING and "Model defect" in run.history[-1].reason and "Net Revenue" in run.history[-1].reason
              and [h.to for h in run.history[-2:]] == [RunState.REPORT_FAILED, RunState.MODEL_GENERATING])
        model_latest = load_report(runs_root / broken / "ValidationReport.json", broken).latest("SemanticModel")
        check("the model gets a failed Gate G4 entry, so it can't be approved as it is, and the report isn't patched",
              model_latest is not None and not model_latest.passed and model_latest.checks[0].id == "G4.DAX" and model_latest.checks[0].code == "REPORT_QUERY_FAILED"
              and not (runs_root / broken / "report" / "v2").exists())
        pbi.broken.clear()

        # --- repair that can't fix it, and the other failures -----------------------------------------------------------
        reset()
        stuck = new_run()
        plan_path = runs_root / stuck / "ReportPlan.json"
        plan_path.write_text(plan_path.read_text(encoding="utf-8").replace('"Net Revenue"', '"Net Revenue X"'), encoding="utf-8")
        path = visual_file(stuck)
        path.write_text(path.read_text(encoding="utf-8").replace('"Net Revenue"', '"Net Revenue X"'), encoding="utf-8")
        client.post(f"/runs/{stuck}/report/validate")
        r = client.post(f"/runs/{stuck}/report/repair")
        run = store.get(stuck)
        check("when the plan itself binds a missing object nothing is regenerated: no progress, NEEDS_INPUT, resume REPORT_FAILED",
              r.status_code == 200 and r.json()["outcome"] == "no_progress" and r.json()["attempts"][0]["discarded"]
              and run.state == RunState.NEEDS_INPUT and run.resume_state == RunState.REPORT_FAILED and not (runs_root / stuck / "report" / "v2").exists())

        reset()
        schema_run = new_run()
        (runs_root / schema_run / "report" / "v1" / "definition" / "version.json").unlink()
        items.calls.clear()
        entry = client.post(f"/runs/{schema_run}/report/validate").json()["report"]
        fail = [c for c in entry["checks"] if c["status"] == "fail"]
        check("a missing PBIR file fails G5.SCHEMA on the report object (a report defect), nothing is published",
              fail and fail[0]["id"] == "G5.SCHEMA" and fail[0]["object"] == "report" and fail[0]["evidence"]["defect"] == "report" and not items.calls)
        r = client.post(f"/runs/{schema_run}/report/repair")
        check("a defect outside one visual or page is escalated: user_input_only, NEEDS_INPUT",
              r.json()["outcome"] == "user_input_only" and store.get(schema_run).state == RunState.NEEDS_INPUT and r.json()["attempts"] == [])

        reset()
        rejected = new_run()
        items.fail = FabricOperationFailed("InvalidDefinition: bad page", "req-1", {"code": "InvalidDefinition"})
        entry = client.post(f"/runs/{rejected}/report/validate").json()["report"]
        check("Fabric rejecting the folder after the local checks pass: a failed G5.RENDER PUBLISH_FAILED, REPORT_FAILED",
              not entry["passed"] and any(c["code"] == "PUBLISH_FAILED" and c["status"] == "fail" for c in entry["checks"])
              and store.get(rejected).state == RunState.REPORT_FAILED)
        items.fail = None

        # --- errors ---------------------------------------------------------------------------------------------------------
        reset()
        check("validate in another state: 409 WRONG_STATE", envelope(client.post(f"/runs/{new_run(state=RunState.MODEL_APPROVED)}/report/validate"), 409, "WRONG_STATE"))
        check("repair in another state: 409 WRONG_STATE", envelope(client.post(f"/runs/{new_run()}/report/repair"), 409, "WRONG_STATE"))
        check("validate before the report exists: 404 NOT_FOUND", envelope(client.post(f"/runs/{new_run(generated=False, state=RunState.REPORT_GENERATING)}/report/validate"), 404, "NOT_FOUND"))
        undeployed = new_run()
        items.deployed = False
        check("the model isn't in the sandbox: 404 NOT_FOUND", envelope(client.post(f"/runs/{undeployed}/report/validate"), 404, "NOT_FOUND"))
        items.deployed = True
        nothing = new_run(state=RunState.REPORT_GENERATING)
        client.post(f"/runs/{nothing}/report/validate")
        run = store.get(nothing)
        run.state = RunState.REPORT_FAILED
        store.save(run)
        check("repair with no failed check: 409 WRONG_STATE 'Nothing to repair'", envelope(client.post(f"/runs/{nothing}/report/repair"), 409, "WRONG_STATE"))
        run = store.get(nothing)
        run.state = RunState.MODEL_FAILED
        store.save(run)
        r = client.post(f"/runs/{nothing}/model/repair")
        check("the model repair only reads the model's entries: with only report entries there is nothing to repair",
              envelope(r, 409, "WRONG_STATE") and "Nothing to repair" in r.json()["error"]["message"])

        # --- queries of other visual types --------------------------------------------------------------------------------
        pages = read_report(runs_root / run_id / "report" / "v1")
        card = pages[0].visuals[0]
        card.fields = [f for f in card.fields if f.kind == "measure"]
        card.filters = []
        pages[0].filters = []
        check("a visual with only a measure is a single-row query, with no TOPN",
              build_query(card, pages[0], spec) == 'EVALUATE\nSUMMARIZECOLUMNS("Net Revenue", [Net Revenue])')
        app.dependency_overrides.clear()

    print("All checks passed.")


if __name__ == "__main__":
    main()
