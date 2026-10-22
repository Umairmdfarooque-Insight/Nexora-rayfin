"""T-701 offline checks: returning a run, impact analysis at object level, stale marking and the
guards that block on it, and targeted regeneration of the model and the report.

Run: python -m app.manual_tests.t701_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
Fabric, Power BI, the SQL endpoint and the models are fakes (extending the fixtures of t502-t604), the
roles are a temporary file and the runs are built by hand in a temporary folder, so no sign-in or
network is needed. The live steps are in MANUAL_TESTING.md.
"""

import json
import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.deps import (
    get_dax_llm,
    get_feasibility_llm,
    get_items_client,
    get_powerbi_client,
    get_recon_llm,
    get_report_llm,
    get_report_services,
    get_runs_root,
    get_source_llm,
    get_sql_connector_factory,
    get_store,
)
from app.api.main import app
from app.api.security import AuthContext, Principal, authenticate
from app.contracts import business_contract as bc
from app.contracts.model_plan import ModelSpec, load_yaml, save_yaml, to_data
from app.manual_tests import t502_offline_checks as t502
from app.manual_tests import t503_offline_checks as t503
from app.manual_tests import t504_offline_checks as t504
from app.manual_tests import t601_offline_checks as t601
from app.manual_tests import t602_offline_checks as t602
from app.manual_tests import t603_offline_checks as t603
from app.store.base import IllegalTransition, StaleArtifacts
from app.store.file_store import FileRunStore
from app.workflow import impact, regenerate_stale, returns, roles as roles_module, scope_guard
from app.workflow.compile_stage import save_contract_version
from app.workflow.gates import GateCheck, GateResult
from app.workflow.harvest_stage import canonical_hash, write_json
from app.workflow.plan_stage import write_new_versions
from app.workflow.report_review import ReportServices
from app.workflow.run import ArtifactRef, RunScope, utc_now
from app.workflow.source_stage import _restricted
from app.workflow.stale import assert_fresh
from app.workflow.states import RunState

check = t502.check
NR, MON = t503.NR, t503.MON
SANDBOX = t602.SANDBOX
USER = "both@contoso.com"
S = RunState


class Items:
    """The Fabric items client: models and reports in one fake."""

    def __init__(self):
        self.reports: dict[str, str] = {}

    def upsert_semantic_model(self, workspace_id, display_name, tmdl_dir):
        return {"item_id": "model-1", "operation": "updated"}

    def get_item_by_name(self, workspace_id, name, item_type):
        return {"id": "model-1", "displayName": name, "type": item_type}

    def upsert_report(self, workspace_id, name, pbir_dir):
        operation = "updated" if name in self.reports else "created"
        self.reports.setdefault(name, f"report-{len(self.reports) + 1}")
        return {"item_id": self.reports[name], "operation": operation}


class Pbi(t504.FakePbi):
    """Measure queries and RLS counts as in T-504, report queries and page exports as in T-603."""

    def execute_dax(self, workspace_id, dataset_id, dax, impersonate=None):
        if "EVALUATE ROW(" in dax or "COUNTROWS" in dax:
            return super().execute_dax(workspace_id, dataset_id, dax, impersonate)
        self.queries.append((dax, impersonate))
        return [{"[Net Revenue]": 1.0}]

    def export_report_page(self, workspace_id, report_id, page_name, timeout_s=300):
        return t603.PNG + page_name.encode()


def report_answer() -> str:
    ref, visual = t601.ref, t601.visual
    return t601.answer(
        [{
            "name": "Overview", "purpose": "Revenue", "filters": [],
            "visuals": [
                visual("a", "lineChart", "Monthly Revenue by Month", ["Q-R2"], category=[ref(table="Date", hierarchy="Calendar")],
                       values=[ref(measure=MON)], drill={"hierarchy": "Calendar", "table": "Date"}),
                visual("b", "clusteredBarChart", "Net Revenue by Product", ["Q-R1", "Q-R3"], category=[ref(table="Product", column="Product Name")],
                       values=[ref(measure=NR)]),
            ],
        }],
        interactions=[{"from_visual": "b", "to_visual": "a", "type": "filter"}],
    )


def tree(folder: Path) -> dict[str, bytes]:
    return scope_guard.tree(folder)


def main() -> None:
    os.environ["SANDBOX_WORKSPACE_ID"] = SANDBOX
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        roles_file = tmp / "roles.yaml"
        roles_file.write_text(
            f"roles:\n  business_owner: [{USER}]\n  bi_engineer: [{USER}]\n  project_reviewer: [{USER}]\n", encoding="utf-8"
        )
        original_roles = roles_module.CONFIG_PATH
        roles_module.CONFIG_PATH = roles_file
        try:
            run_checks(tmp)
        finally:
            roles_module.CONFIG_PATH = original_roles
            app.dependency_overrides.clear()
    print("All checks passed.")


def run_checks(tmp: Path) -> None:
    runs_root = tmp / "runs"
    store = FileRunStore(runs_root)
    items, pbi, sql = Items(), Pbi(), t504.FakeSql()
    recon = t504.ReconLlm({NR: t504.RECON_ANSWER, MON: t504.MON_RECON_ANSWER})
    dax_llm = t503.FakeLlm()
    report_llm = t601.FakeLlm(report_answer())
    app.dependency_overrides.update(
        {
            get_store: lambda: store,
            get_runs_root: lambda: runs_root,
            get_dax_llm: lambda: dax_llm,
            get_recon_llm: lambda: recon,
            get_items_client: lambda: items,
            get_powerbi_client: lambda: pbi,
            get_sql_connector_factory: lambda: (lambda scope: sql),
            get_report_llm: lambda: report_llm,
            get_report_services: lambda: ReportServices(llm=report_llm, items=lambda: items, pbi=lambda: pbi),
            get_source_llm: lambda: None,
            get_feasibility_llm: lambda: None,
            authenticate: lambda: AuthContext(principal=Principal(oid="o-1", upn=USER, name="T"), token="x"),
        }
    )
    client = TestClient(app, raise_server_exceptions=False)

    def envelope(r, status, code):
        return r.status_code == status and r.json()["error"]["code"] == code

    def reset_fakes():
        dax_llm.answers = {NR: [t503.NR_ANSWER], MON: [t503.MON_ANSWER]}
        dax_llm.calls.clear()
        pbi.values = {NR: 1000.0, MON: 1000.0}
        sql.total = sql.mon_total = 1000.0

    # --- a run approved all the way ------------------------------------------------------------------------------
    def approve(rid, artifact, role=None, action="ACCEPT", expected=None, **extra):
        review = {"SemanticModel": "model", "Report": "report"}.get(artifact)
        if expected is None and review:
            expected = client.get(f"/runs/{rid}/{review}/review").json()[f"{review}_hash"]
        if expected is None:
            expected = store.get(rid).artifacts[artifact].hash
        body = {"artifact": artifact, "action": action, "expected_hash": expected, **extra}
        if role:
            body["role"] = role
        return client.post(f"/runs/{rid}/approvals", json=body)

    def new_run(state=S.REPORT_APPROVED, with_report=True):
        run = store.create("Show net revenue by product")
        rid = run.run_id
        run_dir = runs_root / rid
        run_dir.mkdir(parents=True, exist_ok=True)
        spec = t503.spec_with_measures(run_id=rid)
        plan = t502.build_plan(spec)
        save_yaml(spec, run_dir / "ModelSpec.yaml")
        save_yaml(plan, run_dir / "ExecutionPlan.yaml")
        save_yaml(spec, run_dir / "plans" / "ModelSpec.v1.yaml")
        save_yaml(plan, run_dir / "plans" / "ExecutionPlan.v1.yaml")
        write_json(run_dir / "MetadataInventory.json", t502.build_inventory(spec).to_dict())
        contract = bc.BusinessContract(
            run_id=rid, business_objective="Revenue", entities=["Sales"], dimensions=["Product"],
            kpis=[t503.KPI_NR, t503.KPI_MON], status=bc.ContractStatus.APPROVED,
        )
        save_contract_version(store, runs_root, run, contract, status="approved")
        run = store.get(rid)
        run.scope = RunScope(workspace_id=t502.WS, lakehouse_id=t502.LH, lakehouse_name="SalesLakehouse", sql_endpoint=t502.ENDPOINT)
        for name, model in (("ModelSpec", spec), ("ExecutionPlan", plan)):
            run.artifacts[name] = ArtifactRef(path=f"{name}.yaml", version=1, hash=canonical_hash(to_data(model)), status="approved", updated_at=utc_now())
        for name, status in (("MetadataInventory", "approved"), ("SourceResolution", "draft"), ("Feasibility", "draft")):
            run.artifacts[name] = ArtifactRef(path=f"{name}.json", version=1, hash=f"sha256:{name}", status=status, updated_at=utc_now())
        run.state = S.PLAN_APPROVED
        store.save(run)
        assert client.post(f"/runs/{rid}/model/generate").status_code == 200
        reset_fakes()
        assert client.post(f"/runs/{rid}/model/measures").status_code == 200
        if with_report:
            assert client.post(f"/runs/{rid}/model/validate").json()["report"]["passed"]
            assert approve(rid, "SemanticModel", "bi_engineer").status_code == 200
            assert approve(rid, "SemanticModel", "business_owner").status_code == 200
            assert store.get(rid).state is S.MODEL_APPROVED
            assert client.post(f"/runs/{rid}/report/plan").status_code == 200
            assert client.post(f"/runs/{rid}/report/generate").status_code == 200
            assert client.post(f"/runs/{rid}/report/validate").json()["report"]["passed"]
            assert approve(rid, "Report", "project_reviewer").status_code == 200
            assert approve(rid, "Report", "business_owner").status_code == 200
            assert store.get(rid).state is S.REPORT_APPROVED
        return rid

    def impact_of(rid):
        return client.get(f"/runs/{rid}/impact").json()

    def stale_of(rid):
        return {e["artifact"]: e for e in impact_of(rid)["stale"]}

    def kpi_edit(rid, name, **changes):
        contract = client.get(f"/runs/{rid}").json()
        return client.put(f"/runs/{rid}/contract/kpis/{name}", json={**changes, "expected_hash": contract["artifacts"]["BusinessContract"]["hash"]})

    # --- 1. returning a run ---------------------------------------------------------------------------------------
    rid = new_run()
    run = store.get(rid)
    check("setup: a run approved all the way (REPORT_APPROVED) with a model, a report and their approvals", run.state is S.REPORT_APPROVED
          and {"TMDLModel", "PbirReport", "ReportPlan", "ModelSpec", "BusinessContract"} <= set(run.artifacts))
    before = {n: (a.version, a.hash) for n, a in run.artifacts.items()}
    model_v = tree(runs_root / rid / "model" / f"v{run.artifacts['TMDLModel'].version}")
    report_v = tree(runs_root / rid / "report" / f"v{run.artifacts['PbirReport'].version}")

    check("impact without a return: nothing to compare with", impact_of(rid) == {"changed": [], "stale": [], "open_return": None})
    check("return: unknown target is 422", client.post(f"/runs/{rid}/return", json={"to": "NOWHERE", "reason": "x"}).status_code == 422)
    check("return: an empty reason is 422", client.post(f"/runs/{rid}/return", json={"to": "MODEL", "reason": "  "}).status_code == 422)
    check("return: unknown run is 404", envelope(client.post("/runs/nope/return", json={"to": "MODEL", "reason": "x"}), 404, "RUN_NOT_FOUND"))
    run.state = S.DEPLOYING
    store.save(run)
    r = client.post(f"/runs/{rid}/return", json={"to": "REQUIREMENT_ANALYSIS", "reason": "Exclude tax"})
    check("return during DEPLOYING: 409 WRONG_STATE", envelope(r, 409, "WRONG_STATE") and store.get(rid).state is S.DEPLOYING and not returns.list_records(runs_root, rid))
    run = store.get(rid)
    run.state = S.CREATED
    store.save(run)
    check("return from CREATED (already in requirement analysis): 409 WRONG_STATE", envelope(client.post(f"/runs/{rid}/return", json={"to": "PLANNING", "reason": "x"}), 409, "WRONG_STATE"))
    run = store.get(rid)
    run.state = S.REPORT_APPROVED
    store.save(run)

    r = client.post(f"/runs/{rid}/return", json={"to": "REQUIREMENT_ANALYSIS", "reason": "Exclude tax"})
    run = store.get(rid)
    contract_ref = run.artifacts["BusinessContract"]
    check("return to requirement analysis: state CREATED, the contract unlocked as a new draft version, the reason in the history",
          r.status_code == 200 and run.state is S.CREATED and contract_ref.status == "draft" and contract_ref.version == before["BusinessContract"][0] + 1
          and "Exclude tax" in run.history[-1].reason and r.json()["contract"]["status"] == "draft")
    check("returning deletes nothing: the approved contract version and every artifact version stay",
          (runs_root / rid / "contracts" / "BusinessContract.v1.yaml").is_file() and bc.load(runs_root / rid / "contracts" / "BusinessContract.v1.yaml").status is bc.ContractStatus.APPROVED
          and {n: (a.version, a.hash) for n, a in run.artifacts.items() if n != "BusinessContract"} == {n: v for n, v in before.items() if n != "BusinessContract"})
    record = returns.open_return(runs_root, rid)
    check("the return is logged with a baseline of every artifact", record is not None and record.to == "REQUIREMENT_ANALYSIS" and record.from_state == "REPORT_APPROVED"
          and record.reason == "Exclude tax" and record.baseline["BusinessContract"]["version"] == 1 and set(record.baseline) == set(before)
          and record.unlocked == ["BusinessContract"] and record.user["upn"] == USER)
    check("impact before the contract is approved again: nothing is stale yet", impact_of(rid)["stale"] == [] and impact_of(rid)["open_return"]["to"] == "REQUIREMENT_ANALYSIS")

    # --- 2. change the KPI, approve again, read the impact -----------------------------------------------------------
    r = kpi_edit(rid, NR, formula_intent="Sum of line totals excluding cancelled orders and tax")
    check("the unlocked contract can be edited", r.status_code == 200)
    r = approve(rid, "BusinessContract")
    check("G1 accepted again: REQUIREMENT_APPROVED", r.status_code == 200 and store.get(rid).state is S.REQUIREMENT_APPROVED)
    data = impact_of(rid)
    stale = {e["artifact"]: e for e in data["stale"]}
    check("impact: the changed object is the KPI", data["changed"] == [{"artifact": "BusinessContract", "objects": [f"kpi:{NR}"]}])
    check("impact: the plan layer is stale for that KPI",
          all(stale[a]["objects"] == [f"kpi:{NR}"] for a in ("SourceResolution", "Feasibility", "ExecutionPlan")))
    check("impact: the ModelSpec lists the measure and the measure that uses it in its DAX, not the unrelated ones",
          set(stale["ModelSpec"]["objects"]) == {f"measure:{NR}", f"measure:{MON}"} and stale["ModelSpec"]["removals"] == [])
    check("impact: the TMDL model lists the measures and the table file they live in, no other file",
          stale["TMDLModel"]["objects"] == [f"measure:{MON}", f"measure:{NR}", "file:definition/tables/Sales.tmdl"])
    check("impact: the visuals that use those measures (page and visual) are stale in the report plan and the report",
          stale["ReportPlan"]["objects"] == ["visual:Overview/v1", "visual:Overview/v2"] and stale["PbirReport"]["objects"] == ["visual:p1/v1", "visual:p1/v2"])
    check("impact: the artifacts are marked stale with their earlier status remembered; the inventory isn't touched",
          {n for n, a in store.get(rid).artifacts.items() if a.status == "stale"} == {"SourceResolution", "Feasibility", "ExecutionPlan", "ModelSpec", "TMDLModel", "ReportPlan", "PbirReport"}
          and store.get(rid).artifacts["MetadataInventory"].status == "approved"
          and json.loads((runs_root / rid / "Impact.json").read_text(encoding="utf-8"))["prior_status"]["TMDLModel"] == "approved")

    # --- 3. blocked ----------------------------------------------------------------------------------------------------
    try:
        store.transition(rid, S.DEPLOYING, "deploy")
        refused = None
    except StaleArtifacts as exc:
        refused = exc
    check("a deployment is refused because of the stale artifacts, whatever the state", refused is not None and "TMDLModel" in refused.names and store.get(rid).state is S.REQUIREMENT_APPROVED)
    run = store.get(rid)
    run.state = S.REPORT_APPROVED
    store.save(run)
    try:
        store.transition(rid, S.DEPLOYING, "deploy")
        refused = None
    except StaleArtifacts:
        refused = True
    except IllegalTransition:
        refused = False
    check("also from REPORT_APPROVED, where the transition itself is legal", refused is True)
    run.state = S.REQUIREMENT_APPROVED
    store.save(run)
    check("assert_fresh names the stale artifacts and ignores missing ones",
          _raises(lambda: assert_fresh(store.get(rid).artifacts, ("TMDLModel", "Nope"), "Generating")) == ["TMDLModel"])
    run = store.get(rid)
    run.state = S.MODEL_APPROVED
    store.save(run)
    r = client.post(f"/runs/{rid}/report/generate")
    check("generating the report in the right state but with a stale plan or model: 409 STALE_ARTIFACTS naming them", envelope(r, 409, "STALE_ARTIFACTS") and "TMDLModel" in r.json()["error"]["details"]["stale"])
    r = client.post(f"/runs/{rid}/report/plan")
    check("planning the report from a stale model: 409 STALE_ARTIFACTS", envelope(r, 409, "STALE_ARTIFACTS"))
    run = store.get(rid)
    run.state = S.REQUIREMENT_APPROVED
    store.save(run)

    # --- 4. regenerate: the requirement layer ---------------------------------------------------------------------------
    r = client.post(f"/runs/{rid}/regenerate-stale")
    body = r.json()
    check("regenerate at REQUIREMENT_APPROVED goes through G2; an inventory that fails it stops the call with the reason",
          r.status_code == 200 and body["run"]["state"] == "REQUIREMENT_APPROVED" and body["regenerated"] == []
          and body["validation"] and body["validation"][0]["gate"] == "G2" and body["needs_review"])

    # the plan layer is run by hand here (it needs the LLM stages and a harvested inventory: see MANUAL_TESTING.md);
    # what is checked is its object-level merge
    run_dir = runs_root / rid
    old_spec = load_yaml(ModelSpec, run_dir / "ModelSpec.yaml")
    new_spec = old_spec.model_copy(deep=True)
    new_spec.measures = [m.model_copy(update={"dax": None, "description": "Approved: Sum of line totals excluding cancelled orders and tax"}) if m.name == NR else m.model_copy(update={"dax": None}) for m in new_spec.measures]
    new_spec.tables = [t for t in new_spec.tables if t.name != "Product"]  # a different plan would not be kept
    notes: list[str] = []
    merged, merged_plan = regenerate_stale._merge_measures({NR.casefold()}, notes)(old_spec, t502.build_plan(old_spec), new_spec, t502.build_plan(new_spec))
    check("plan merge: only the changed KPI's measure comes from the new plan; the other measure, tables and relationships stay as approved",
          [(m.name, m.dax is None) for m in merged.measures] == [(NR, True), (MON, False)] and {t.name for t in merged.tables} == {t.name for t in old_spec.tables}
          and merged.relationships == old_spec.relationships and not notes)
    changed_table = new_spec.model_copy(deep=True)
    changed_table.measures[0].table = "Elsewhere"
    whole, _ = regenerate_stale._merge_measures({NR.casefold()}, notes)(old_spec, t502.build_plan(old_spec), changed_table, t502.build_plan(changed_table))
    check("plan merge: a changed measure that needs a table the approved model doesn't have uses the whole new plan, and says so",
          whole is changed_table and notes and "whole plan" in notes[0])

    old_res = t_resolution(["Sales"], {NR: "a", MON: "b"})
    new_res = t_resolution(["Sales"], {NR: "NEW", MON: "NEW"})
    entities, kpis = _restricted(old_res, new_res.entities, new_res.kpi_fields, {f"kpi:{NR}"})
    check("source resolution: only the restricted KPI's fields are taken from the new answer", [(k.kpi, k.fields[0].column) for k in kpis] == [(NR, "NEW"), (MON, "b")])

    merged.version, merged_plan.version = old_spec.version, 1
    run = store.get(rid)
    run.state = S.DATA_PROFILED
    store.save(run)
    stale_plan_hash = store.get(rid).artifacts["ExecutionPlan"].hash
    r = client.patch(f"/runs/{rid}/plan", json={"expected_hash": stale_plan_hash, "ops": [{"op": "remove_measure", "name": MON}]})
    check("editing a stale plan is refused: 409 STALE_ARTIFACTS", envelope(r, 409, "STALE_ARTIFACTS") and "ExecutionPlan" in r.json()["error"]["details"]["stale"])
    write_new_versions(store, runs_root, store.get(rid), merged, merged_plan)
    run = store.get(rid)
    for name in ("SourceResolution", "Feasibility", "ModelSpec", "ExecutionPlan"):
        run.artifacts[name].status = "approved"  # as G3 does
    store.save(run)
    impact.mark_fresh(store, runs_root, rid, ["SourceResolution", "Feasibility"])
    run = store.get(rid)
    run.state = S.PLAN_APPROVED
    run.artifacts["ModelSpec"].status = run.artifacts["ExecutionPlan"].status = "approved"
    store.save(run)
    stale = stale_of(rid)
    check("after the plan layer: only the model and the report are stale", set(stale) == {"TMDLModel", "ReportPlan", "PbirReport"} and store.get(rid).artifacts["ModelSpec"].status == "approved")

    # --- 5. regenerate: the model layer --------------------------------------------------------------------------------
    tmdl_before = store.get(rid).artifacts["TMDLModel"]
    old_tree = tree(run_dir / tmdl_before.path)
    reset_fakes()
    dax_llm.answers[NR] = [t503.answer(NR, t503.NR_DAX + " + 0", [t503.col("Sales", "LineTotal"), t503.col("Sales Order", "Status")])]
    r = client.post(f"/runs/{rid}/regenerate-stale")
    body = r.json()
    run = store.get(rid)
    new_tree = tree(run_dir / run.artifacts["TMDLModel"].path)
    changed = sorted(n for n in old_tree.keys() | new_tree.keys() if old_tree.get(n) != new_tree.get(n))
    check("regenerate (model): 200, MODEL_GENERATING, the model regenerated and validated again (G4 passed)",
          r.status_code == 200 and body["run"]["state"] == "MODEL_GENERATING" and [x["artifact"] for x in body["regenerated"]] == ["TMDLModel"]
          and body["validation"] == [{"artifact": "SemanticModel", "gate": "G4", "passed": True, "failed_checks": []}] and body["needs_review"] == [])
    check("regenerate (model): only the stale objects changed: a diff of the model version folders shows the Sales table file only",
          changed == ["definition/tables/Sales.tmdl"] and run.artifacts["TMDLModel"].version > tmdl_before.version)
    check("regenerate (model): the new DAX is in the table file and every other file is byte-identical",
          b"+ 0" in new_tree["definition/tables/Sales.tmdl"] and all(old_tree[n] == new_tree[n] for n in old_tree if n != "definition/tables/Sales.tmdl"))
    check("the model is no longer stale, and its earlier approvals are not counted: both roles are pending again",
          "TMDLModel" not in stale_of(rid) and {s["role"]: s["status"] for s in client.get(f"/runs/{rid}/model/review").json()["approvals_status"]} == {"bi_engineer": "pending", "business_owner": "pending"})
    check("regenerating again with the model awaiting approval: 409 WRONG_STATE", envelope(client.post(f"/runs/{rid}/regenerate-stale"), 409, "WRONG_STATE"))
    check("approving the model needs both roles again", approve(rid, "SemanticModel", "bi_engineer").status_code == 200 and store.get(rid).state is S.MODEL_GENERATING
          and approve(rid, "SemanticModel", "business_owner").status_code == 200 and store.get(rid).state is S.MODEL_APPROVED)

    # --- 6. regenerate: the report layer ---------------------------------------------------------------------------------
    report_before = store.get(rid).artifacts["PbirReport"]
    old_report = tree(run_dir / report_before.path)
    r = client.post(f"/runs/{rid}/regenerate-stale")
    body = r.json()
    run = store.get(rid)
    new_report = tree(run_dir / run.artifacts["PbirReport"].path)
    diff = sorted(n for n in old_report.keys() | new_report.keys() if old_report.get(n) != new_report.get(n))
    check("regenerate (report): 200, REPORT_GENERATING, the plan re-pointed at the new model, the report published and validated (G5 passed)",
          r.status_code == 200 and body["run"]["state"] == "REPORT_GENERATING" and [x["artifact"] for x in body["regenerated"]] == ["ReportPlan", "PbirReport"]
          and body["validation"] == [{"artifact": "Report", "gate": "G5", "passed": True, "failed_checks": []}] and body["needs_review"] == [])
    check("regenerate (report): only files of the stale visuals (or the model binding) can differ: the diff of the report folders is inside them",
          set(diff) <= {"definition.pbir", "definition/pages/p1/visuals/v1/visual.json", "definition/pages/p1/visuals/v2/visual.json"} and run.artifacts["PbirReport"].version > report_before.version)
    plan_ref = run.artifacts["ReportPlan"]
    check("the report plan is on the new model and the report is not stale", plan_ref.based_on["SemanticModel"] == run.artifacts["TMDLModel"].hash and "PbirReport" not in stale_of(rid))
    review = client.get(f"/runs/{rid}/report/review").json()
    check("the report needs both approvals again, though its files may be identical", {s["role"]: s["status"] for s in review["approvals_status"]} == {"project_reviewer": "pending", "business_owner": "pending"})
    check("the return is still open until the report is approved", returns.open_return(runs_root, rid) is not None)
    assert approve(rid, "Report", "project_reviewer").status_code == 200
    r = approve(rid, "Report", "business_owner")
    check("both approve: REPORT_APPROVED, nothing stale, the return resolved", r.status_code == 200 and store.get(rid).state is S.REPORT_APPROVED
          and returns.open_return(runs_root, rid) is None and impact_of(rid) == {"changed": [], "stale": [], "open_return": None}
          and not [n for n, a in store.get(rid).artifacts.items() if a.status == "stale"])
    check("the history shows every step and the earlier versions are all still there",
          (runs_root / rid / "model" / "v2").is_dir() and (runs_root / rid / "report" / "v1").is_dir() and (runs_root / rid / "contracts" / "BusinessContract.v1.yaml").is_file()
          and (runs_root / rid / "plans" / "ModelSpec.v1.yaml").is_file() and len(returns.list_records(runs_root, rid)) == 2)

    # --- 7. a change with no object-level effect -------------------------------------------------------------------------
    rid2 = new_run()
    assert client.post(f"/runs/{rid2}/return", json={"to": "REQUIREMENT_ANALYSIS", "reason": "Reword"}).status_code == 200
    r = kpi_edit(rid2, NR, rationale="Reworded for the board")
    check("a rationale edit works", r.status_code == 200)
    assert approve(rid2, "BusinessContract").status_code == 200
    check("no object-level effect (a KPI's rationale): changed [] and stale []", impact_of(rid2)["changed"] == [] and impact_of(rid2)["stale"] == []
          and not [n for n, a in store.get(rid2).artifacts.items() if a.status == "stale"])
    original = regenerate_stale.check_g2
    regenerate_stale.check_g2 = lambda s_, root, run_id: GateResult(gate="G2", artifact="MetadataInventory", artifact_hash="x", passed=True, checked_at=utc_now(), checks=[GateCheck(id="G2.1", description="d", status="pass")])
    try:
        r = client.post(f"/runs/{rid2}/regenerate-stale")
    finally:
        regenerate_stale.check_g2 = original
    run = store.get(rid2)
    check("nothing is regenerated, and the approved stages are walked back up through legal transitions to where the run was, closing the return",
          r.status_code == 200 and r.json()["regenerated"] == [] and run.state is S.REPORT_APPROVED and returns.open_return(runs_root, rid2) is None
          and [h.to.value for h in run.history[-7:]] == ["REQUIREMENT_APPROVED", "DATA_PROFILED", "PLAN_APPROVED", "MODEL_GENERATING", "MODEL_APPROVED", "REPORT_GENERATING", "REPORT_APPROVED"])

    # --- 8. a deleted KPI ------------------------------------------------------------------------------------------------------
    rid3 = new_run()
    assert client.post(f"/runs/{rid3}/return", json={"to": "REQUIREMENT_ANALYSIS", "reason": "Drop the monthly KPI"}).status_code == 200
    run = store.get(rid3)
    contract = bc.load(runs_root / rid3 / "BusinessContract.yaml")
    contract = contract.model_copy(update={"kpis": [k for k in contract.kpis if k.name != MON]})
    save_contract_version(store, runs_root, run, contract)
    assert approve(rid3, "BusinessContract").status_code == 200
    stale = stale_of(rid3)
    first = returns.open_return(runs_root, rid3)
    check("a deleted KPI: its measure and visuals are marked for removal and shown, not dropped",
          stale["ModelSpec"]["removals"] == [f"measure:{MON}"] and stale["TMDLModel"]["removals"] == [f"measure:{MON}"]
          and stale["ReportPlan"]["removals"] == ["visual:Overview/v1"] and stale["PbirReport"]["removals"] == ["visual:p1/v1"]
          and f"measure:{MON}" in stale["ModelSpec"]["objects"] and (runs_root / rid3 / "ModelSpec.yaml").is_file()
          and any(m.name == MON for m in load_yaml(ModelSpec, runs_root / rid3 / "ModelSpec.yaml").measures))

    r = client.post(f"/runs/{rid3}/return", json={"to": "REQUIREMENT_ANALYSIS", "reason": "Again"})
    second = returns.open_return(runs_root, rid3)
    check("a second return keeps the first baseline and the stage the run was returned from; the contract is a draft again so nothing is stale for now",
          r.status_code == 200 and second.return_id != first.return_id and second.baseline == first.baseline and second.from_state == "REPORT_APPROVED"
          and impact_of(rid3)["stale"] == [] and store.get(rid3).state is S.CREATED)
    assert approve(rid3, "BusinessContract").status_code == 200
    check("approving again marks the same artifacts stale again", {"TMDLModel", "ReportPlan"} <= set(stale_of(rid3)))

    # --- 9. returning to planning and to the model -----------------------------------------------------------------------
    rid4 = new_run()
    r = client.post(f"/runs/{rid4}/return", json={"to": "PLANNING", "reason": "Different storage mode"})
    run = store.get(rid4)
    check("return to planning: DATA_PROFILED, the plan and the model spec unlocked (draft), the later artifacts kept as they are",
          r.status_code == 200 and run.state is S.DATA_PROFILED and run.artifacts["ExecutionPlan"].status == "draft" and run.artifacts["ModelSpec"].status == "draft"
          and run.artifacts["TMDLModel"].status == "approved")
    spec = load_yaml(ModelSpec, runs_root / rid4 / "ModelSpec.yaml")
    spec.relationships = [r_ for r_ in spec.relationships if r_.name != "Sales_Product"]
    plan = t502.build_plan(spec)
    plan.version = 1
    write_new_versions(store, runs_root, store.get(rid4), spec, plan)
    run = store.get(rid4)
    run.artifacts["ModelSpec"].status = run.artifacts["ExecutionPlan"].status = "approved"
    run.state = S.PLAN_APPROVED
    store.save(run)
    impact.on_accept(store, runs_root, rid4)
    stale = stale_of(rid4)
    data = impact_of(rid4)
    check("the changed object is the removed relationship; the model file of the relationship and the visual that uses the Product table are stale",
          data["changed"] == [{"artifact": "ModelSpec", "objects": ["relationship:Sales_Product"]}]
          and set(stale["TMDLModel"]["objects"]) == {"file:definition/relationships.tmdl", "file:definition/tables/Product.tmdl", "file:definition/tables/Sales.tmdl"}
          and stale["ReportPlan"]["objects"] == ["visual:Overview/v2"] and "ExecutionPlan" not in stale)

    rid5 = new_run()
    r = client.post(f"/runs/{rid5}/return", json={"to": "MODEL", "reason": "Add a bridge"})
    run = store.get(rid5)
    check("return to the model: PLAN_APPROVED, the model unlocked (draft), the plan stays approved",
          r.status_code == 200 and run.state is S.PLAN_APPROVED and run.artifacts["TMDLModel"].status == "draft" and run.artifacts["ExecutionPlan"].status == "approved")
    check("a return to the model from PLAN_APPROVED is refused (nothing downstream to reopen): 409",
          envelope(client.post(f"/runs/{rid5}/return", json={"to": "MODEL", "reason": "again"}), 409, "WRONG_STATE"))

    # the model approvals given before a return don't count, even for identical files
    assert client.post(f"/runs/{rid5}/model/generate").status_code == 200
    reset_fakes()
    assert client.post(f"/runs/{rid5}/model/measures").status_code == 200
    assert client.post(f"/runs/{rid5}/model/validate").json()["report"]["passed"]
    review = client.get(f"/runs/{rid5}/model/review").json()
    check("a model regenerated to the same content after a return still needs both approvals", {s["role"]: s["status"] for s in review["approvals_status"]} == {"bi_engineer": "pending", "business_owner": "pending"})

    # --- 10. the scope guard ----------------------------------------------------------------------------------------------------
    a, b = tmp / "guard-a", tmp / "guard-b"
    for folder, extra in ((a, b"one"), (b, b"two")):
        (folder / "definition" / "tables").mkdir(parents=True)
        (folder / "definition" / "tables" / "Sales.tmdl").write_bytes(extra)
        (folder / "definition" / "tables" / "Product.tmdl").write_bytes(b"same" if folder is a else b"changed")
        (folder / "renders").mkdir()
        (folder / "renders" / "p1.png").write_bytes(extra)
    try:
        scope_guard.check("model", a, b, {"definition/tables/Sales.tmdl"})
        out = None
    except scope_guard.ScopeViolation as exc:
        out = exc
    check("scope guard: a change outside the allowed files is refused and named; renders are ignored", out is not None and out.files == ["definition/tables/Product.tmdl"])
    check("scope guard: a folder prefix covers its files; None means no restriction",
          scope_guard.outside(["definition/roles/R.tmdl", "x"], {"definition/roles/"}) == ["x"] and scope_guard.outside(["x"], None) == [])



def _raises(fn):
    try:
        fn()
    except StaleArtifacts as exc:
        return exc.names
    return None


def t_resolution(entities, columns: dict):
    from app.contracts.source_resolution import EntityResolution, FieldMapping, KpiFields, SourceResolution

    return SourceResolution(
        run_id="r", version=1,
        entities=[EntityResolution(entity=e) for e in entities],
        kpi_fields=[KpiFields(kpi=k, fields=[FieldMapping(need="n", table="Sales", column=c, role="measure")]) for k, c in columns.items()],
    )


if __name__ == "__main__":
    main()
