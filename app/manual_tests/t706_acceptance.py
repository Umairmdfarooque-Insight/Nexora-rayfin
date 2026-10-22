"""T-706: the offline rehearsal of the vertical-slice acceptance.

Run: python -m app.manual_tests.t706_acceptance            (the integrated run, then the scenario suites)
     python -m app.manual_tests.t706_acceptance --run-only (the integrated run only)

Part A takes ONE Sales prompt through the integrated API, as the Rayfin screens would: the ambiguity of
"revenue" is detected and resolved, the contract is approved, a model is generated with an intentional
failure that is repaired narrowly, a report is generated with an intentional binding failure that is
repaired narrowly, the reviewers approve, the release is approved, the artifacts are promoted and smoke
tested, and the run is traced from a visual back to the prompt. A completed run is
shown to be final. Part B runs the offline suite behind each of the other acceptance scenarios and prints
the table of .claude/tasks/T-706-evidence.md.

Everything outside the code under test is a fake (Fabric, Power BI, the SQL endpoint, the model), and the
data stages (harvest, profile, relationships, sources, feasibility, plan) are seeded from the fixtures of
T-502/T-503, because their own suites cover them. The real thing - Rayfin, a Lakehouse, a capacity, a
second workspace - is the live part of the acceptance, in MANUAL_TESTING.md. This script proves the
integration of the code; it does not replace that run.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.deps import (
    get_dax_llm,
    get_fabric_client,
    get_items_client,
    get_llm,
    get_powerbi_client,
    get_promotion_items_client,
    get_recon_llm,
    get_report_llm,
    get_report_services,
    get_runs_root,
    get_sql_connector_factory,
    get_store,
    get_workspace_lister,
)
from app.api.main import app
from app.api.security import AuthContext, Principal, authenticate
from app.contracts import business_contract as bc
from app.contracts.metadata_inventory import ColumnProfile, ProfileEvidence, TableProfile
from app.contracts.model_plan import Measure, save_yaml, to_data
from app.contracts.source_resolution import FieldMapping, KpiFields, SourceResolution
from app.manual_tests import t202_offline_checks as t202
from app.manual_tests import t204_offline_checks as t204
from app.manual_tests import t502_offline_checks as t502
from app.manual_tests import t503_offline_checks as t503
from app.manual_tests import t504_offline_checks as t504
from app.manual_tests import t601_offline_checks as t601
from app.manual_tests import t701_offline_checks as t701
from app.manual_tests import t702_offline_checks as t702
from app.repair.report_adapter import read_tree

from app.store.file_store import FileRunStore
from app.workflow import promotion, roles as roles_module
from app.workflow.harvest_stage import canonical_hash, write_json
from app.workflow.report_review import ReportServices
from app.workflow.run import ArtifactRef, RunScope, utc_now
from app.workflow.states import RunState

check = t502.check
S = RunState
USER = t702.USER
MEASURE = "Net Revenue"
TABLE = "saleslt.SalesLT_SalesOrderDetail"
DAX = "SUM(Sales[LineTotal])"
PROMPT = "Show revenue by month for 2024"


def changed(before: dict, after: dict) -> list[str]:
    return sorted(n for n in before.keys() | after.keys() if before.get(n) != after.get(n))


def part_a() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        roles = tmp / "roles.yaml"
        roles.write_text(
            f"roles:\n  business_owner: [{USER}]\n  bi_engineer: [{USER}]\n  project_reviewer: [{USER}]\n  release_approver: [{USER}]\n  security_reviewer: [{USER}]\n  data_engineer: [{USER}]\n",
            encoding="utf-8",
        )
        original_roles = roles_module.CONFIG_PATH
        roles_module.CONFIG_PATH = roles
        try:
            run_part_a(tmp)
        finally:
            roles_module.CONFIG_PATH = original_roles
            app.dependency_overrides.clear()


def run_part_a(tmp: Path) -> None:
    runs_root = tmp / "runs"
    store = FileRunStore(runs_root)
    items, pbi, sql = t701.Items(), t701.Pbi(), t504.FakeSql()
    target_items, target_pbi, workspaces = t702.TargetItems(), t702.TargetPbi(), t702.Workspaces()
    compile_llm = t202.FakeLlm()
    dax_llm = t503.FakeLlm()
    recon = None
    report_llm = t601.FakeLlm(t601.answer(
        [{
            "name": "Overview", "purpose": "Revenue", "filters": [],
            "visuals": [
                t601.visual("a", "lineChart", "Net Revenue by Month", ["Q-R1"], category=[t601.ref(table="Date", hierarchy="Calendar")],
                            values=[t601.ref(measure=MEASURE)], drill={"hierarchy": "Calendar", "table": "Date"}),
                t601.visual("b", "clusteredBarChart", "Net Revenue by Product", ["Q-R2"], category=[t601.ref(table="Product", column="Product Name")],
                            values=[t601.ref(measure=MEASURE)]),
            ],
        }],
    ))
    holder = {"recon": None}
    app.dependency_overrides.update(
        {
            get_store: lambda: store,
            get_runs_root: lambda: runs_root,
            get_llm: lambda: compile_llm,
            get_dax_llm: lambda: dax_llm,
            get_recon_llm: lambda: holder["recon"],
            get_items_client: lambda: items,
            get_powerbi_client: lambda: t702.Router(pbi, target_pbi),
            get_promotion_items_client: lambda: target_items,
            get_fabric_client: lambda: workspaces,
            get_workspace_lister: lambda: (lambda: []),
            get_sql_connector_factory: lambda: (lambda scope: sql),
            get_report_llm: lambda: report_llm,
            get_report_services: lambda: ReportServices(llm=report_llm, items=lambda: items, pbi=lambda: pbi),
            authenticate: lambda: AuthContext(principal=Principal(oid="o-1", upn=USER, name="Tester"), token="x"),
        }
    )
    client = TestClient(app, raise_server_exceptions=False)
    ticked: list[str] = []

    def tick(item: str, condition: bool) -> None:
        check(item, condition)
        ticked.append(item)

    def envelope(r, status, code):
        return r.status_code == status and r.json()["error"]["code"] == code

    def approve(rid, artifact, role=None, action="ACCEPT", comment=None):
        review = {"SemanticModel": "model", "Report": "report"}.get(artifact)
        if review:
            expected = client.get(f"/runs/{rid}/{review}/review").json()[f"{review}_hash"]
        elif artifact == "Release":
            expected = promotion.release_hash(store.get(rid))
        else:
            expected = store.get(rid).artifacts[artifact].hash
        body = {"artifact": artifact, "action": action, "expected_hash": expected}
        if role:
            body["role"] = role
        if comment:
            body["comment"] = comment
        return client.post(f"/runs/{rid}/approvals", json=body)

    # --- 1. the prompt and the ambiguity ------------------------------------------------------------------------------
    r = client.post("/runs", json={"prompt": PROMPT, "subject_area": "Sales"})
    rid = r.json()["run_id"]
    run_dir = runs_root / rid
    r = client.post(f"/runs/{rid}/compile")
    open_ids = {q["id"] for q in r.json()["run"]["open_questions"]}
    tick("A user submits a Sales business prompt through the API (the call Rayfin makes)", r.status_code == 200 and store.get(rid).prompt == PROMPT)
    tick("A material KPI ambiguity (Revenue, with no definition) is detected: a blocking question, state NEEDS_CLARIFICATION",
         "RULE-REVENUE" in open_ids and store.get(rid).state is S.NEEDS_CLARIFICATION)
    blocked = approve(rid, "BusinessContract")
    check("generation can't go ahead: approving the contract is refused (409 GATE_FAILED)", envelope(blocked, 409, "GATE_FAILED"))
    r = client.post(f"/runs/{rid}/questions/RULE-REVENUE/answer", json={"answer": t204.DEFINITION})
    tick("The ambiguity is resolved through clarification: the question is answered, the state is back to CREATED", r.status_code == 200 and store.get(rid).state is S.CREATED)
    r = approve(rid, "BusinessContract", comment="KPI meaning, grain and exclusions are right")
    tick("The requirement contract is approved and locked for the run (REQUIREMENT_APPROVED)", r.status_code == 200 and store.get(rid).state is S.REQUIREMENT_APPROVED
         and bc.load(run_dir / "BusinessContract.yaml").status is bc.ContractStatus.APPROVED)
    contract = bc.load(run_dir / "BusinessContract.yaml")
    kpi = contract.kpis[0]
    locked = client.put(f"/runs/{rid}/contract/kpis/{kpi.name.replace(' ', '%20')}", json={"currency": "EUR", "expected_hash": store.get(rid).artifacts["BusinessContract"].hash})
    check("an approved contract can't be edited (409 CONTRACT_LOCKED)", envelope(locked, 409, "CONTRACT_LOCKED"))

    # --- 2. the data stages (seeded) ---------------------------------------------------------------------------------------
    spec = t503.spec_with_measures(run_id=rid)
    spec.measures = [Measure(name=MEASURE, table="Sales", kpi=kpi.name, formula_intent=kpi.formula_intent, description=f"Approved: {kpi.formula_intent}")]
    plan = t502.build_plan(spec)
    save_yaml(spec, run_dir / "ModelSpec.yaml")
    save_yaml(plan, run_dir / "ExecutionPlan.yaml")
    save_yaml(spec, run_dir / "plans" / "ModelSpec.v1.yaml")
    inventory = t502.build_inventory(spec)
    for t in inventory.tables:
        if f"{t.schema_name}.{t.table_name}" == TABLE:
            t.profile = TableProfile(
                status="ok", row_count=1200, evidence=ProfileEvidence(queries=["SELECT COUNT_BIG(*) FROM [saleslt].[SalesLT_SalesOrderDetail]"], profiled_at="2026-10-01T00:00:00Z"),
                columns={"LineTotal": ColumnProfile(null_count=3, distinct_count=900, min=0.5, max=9000.0)},
            )
    write_json(run_dir / "MetadataInventory.json", inventory.to_dict())
    write_json(run_dir / "SourceResolution.json", SourceResolution(
        run_id=rid, version=1, kpi_fields=[KpiFields(kpi=kpi.name, fields=[FieldMapping(need="order line amount", table=TABLE, column="LineTotal", role="measure")])],
    ).to_dict())
    run = store.get(rid)
    run.scope = RunScope(workspace_id=t502.WS, lakehouse_id=t502.LH, lakehouse_name="SalesLakehouse", sql_endpoint=t502.ENDPOINT)
    for name, model in (("ModelSpec", spec), ("ExecutionPlan", plan)):
        run.artifacts[name] = ArtifactRef(path=f"{name}.yaml", version=1, hash=canonical_hash(to_data(model)), status="approved", updated_at=utc_now())
    for name, status in (("MetadataInventory", "approved"), ("SourceResolution", "draft")):
        run.artifacts[name] = ArtifactRef(path=f"{name}.json", version=1, hash=f"sha256:{name}", status=status, updated_at=utc_now())
    run.state = S.PLAN_APPROVED
    store.save(run)
    ticked.append("The selected Lakehouse metadata and relationship evidence are harvested (seeded here; proven by T-301 to T-304 and live)")
    ticked.append("The model plan is approved, with a fact grain and evidence-backed joins (seeded here; proven by T-404, T-405 and live)")

    # --- 3. the model, with an intentional failure -----------------------------------------------------------------------
    holder["recon"] = t504.ReconLlm({kpi.name: t504.MON_RECON_ANSWER})
    pbi.values = {MEASURE: 1000.0}
    sql.total = sql.mon_total = 1000.0
    dax_llm.answers = {MEASURE: [t503.answer(MEASURE, DAX, [t503.col("Sales", "LineTotal")])]}
    assert client.post(f"/runs/{rid}/model/generate").status_code == 200
    r = client.post(f"/runs/{rid}/model/measures")
    tick("TMDL and DAX artifacts are generated in a sandbox (a model version with the validated measure DAX)", r.status_code == 200 and r.json()["measures"][0]["status"] == "accepted")
    folder = run_dir / store.get(rid).artifacts["TMDLModel"].path
    table = folder / "definition" / "tables" / "Sales.tmdl"
    table.write_bytes(table.read_bytes().replace(b"Sales[LineTotal]", b"Sales[LineTotalX]"))  # the intentional model failure
    before = read_tree(folder)
    r = client.post(f"/runs/{rid}/model/validate")
    check("the intentional failure is caught: Gate G4 fails and the run is MODEL_FAILED", not r.json()["report"]["passed"] and store.get(rid).state is S.MODEL_FAILED)
    r = client.post(f"/runs/{rid}/model/repair")
    run = store.get(rid)
    after = read_tree(run_dir / run.artifacts["TMDLModel"].path)
    tick("An intentional model failure is repaired by targeted regeneration, and the model is revalidated and passes (only the Sales table file changed)",
         r.json()["outcome"] == "passed" and run.state is S.MODEL_GENERATING and changed(before, after) == ["definition/tables/Sales.tmdl"])
    check("both roles must approve the model", approve(rid, "SemanticModel", "bi_engineer").status_code == 200 and store.get(rid).state is S.MODEL_GENERATING
          and approve(rid, "SemanticModel", "business_owner").status_code == 200 and store.get(rid).state is S.MODEL_APPROVED)

    # --- 4. the report, with an intentional failure -----------------------------------------------------------------------
    assert client.post(f"/runs/{rid}/report/plan").status_code == 200
    r = client.post(f"/runs/{rid}/report/generate")
    tick("A PBIR report is generated against the approved semantic model and published to the sandbox", r.status_code == 200 and store.get(rid).state is S.REPORT_GENERATING)
    visual = run_dir / "report" / "v1" / "definition" / "pages" / "p1" / "visuals" / "v2" / "visual.json"
    visual.write_bytes(visual.read_bytes().replace(f'"{MEASURE}"'.encode(), b'"Ghost"'))  # the intentional report-binding failure
    before = read_tree(run_dir / "report" / "v1")
    r = client.post(f"/runs/{rid}/report/validate")
    check("the intentional binding failure is caught: Gate G5 fails (G5.BIND) and the run is REPORT_FAILED", not r.json()["report"]["passed"] and store.get(rid).state is S.REPORT_FAILED)
    r = client.post(f"/runs/{rid}/report/repair")
    run = store.get(rid)
    after = read_tree(run_dir / run.artifacts["PbirReport"].path)
    tick("An intentional report-binding issue is repaired without rebuilding unaffected artifacts (only that visual's file changed)",
         r.json()["outcome"] == "passed" and run.state is S.REPORT_GENERATING and changed(before, after) == ["definition/pages/p1/visuals/v2/visual.json"])
    r1 = approve(rid, "Report", "project_reviewer")
    r2 = approve(rid, "Report", "business_owner")
    tick("Business and technical reviewers complete their defined approvals (model: BI engineer and business owner; report: project reviewer and business owner)",
         r1.status_code == 200 and r2.status_code == 200 and store.get(rid).state is S.REPORT_APPROVED)

    # --- 5. promotion ------------------------------------------------------------------------------------------------------------
    r = client.post(f"/runs/{rid}/deploy", json={"target_workspace_id": t702.TARGET})
    check("deploying without the final release approval is refused (G6.4)", envelope(r, 409, "GATE_FAILED") and not target_items.store)
    assert approve(rid, "Release").status_code == 200
    r = client.post(f"/runs/{rid}/deploy", json={"target_workspace_id": t702.TARGET})
    dep = store.get(rid).deployment
    tick("Approved artifacts are published to the target workspace and the smoke tests pass (state COMPLETED, items recorded with their approved hashes)",
         r.status_code == 202 and store.get(rid).state is S.COMPLETED and dep["outcome"] == "completed" and all(t["status"] == "pass" for t in dep["smoke_tests"])
         and len(dep["items"]) == 2 and len(target_items.store) == 2)

    # --- 6. traceability ---------------------------------------------------------------------------------------------------------
    audit = client.get(f"/runs/{rid}/audit").json()
    trace = client.get(f"/runs/{rid}/trace?visual_id=v1").json()
    tick("The run can be traced from the final report to the prompt, the approved contract, the source evidence, the model version and the validation results",
         audit["chain_valid"] and audit["total"] > 20 and {"approval", "gate", "validation", "deployment"} <= {e["type"] for e in audit["events"]}
         and trace["prompt"] == PROMPT and trace["measure"]["name"] == MEASURE and trace["measure"]["model_version"] == store.get(rid).artifacts["TMDLModel"].version
         and trace["kpi"]["approved_by"] == USER and trace["kpi"]["definition"]["formula_intent"] == kpi.formula_intent
         and trace["sources"][0]["profile_evidence"]["distinct_count"] == 900 and trace["gaps"] == [])

    # --- 7. a completed run is final -------------------------------------------------------------------------------------------
    r = client.post(f"/runs/{rid}/return", json={"to": "REQUIREMENT_ANALYSIS", "reason": "Exclude tax as well"})
    check("a COMPLETED run can not be returned (409 WRONG_STATE): the upstream KPI change is exercised on an approved run, by t701", envelope(r, 409, "WRONG_STATE"))

    print("\nDefinition of Done, as proven by this rehearsal:")
    for item in ticked:
        print("  [x] " + item)


SCENARIOS = [
    ("Simple Sales KPI", "T-706", "this script, part A (the integrated run)", None),
    ("Ambiguous Revenue", "T-205", "t202, t203, t204 and part A", ["t202", "t203", "t204"]),
    ("Invalid relationship", "T-504", "t504, t505", ["t504", "t505"]),
    ("Many-to-many bridge", "T-506", "t506", ["t506"]),
    ("Unsupported bridge", "T-403", "t403", ["t403"]),
    ("Report binding failure", "T-603", "t603 and part A", ["t603"]),
    ("Upstream KPI change", "T-701", "t701 and part A", ["t701"]),
]


def part_b() -> bool:
    print("\nScenario suites (each is the offline proof behind a scenario):")
    results: dict[str, str] = {}
    for _, _, _, suites in SCENARIOS:
        for suite in suites or []:
            if suite in results:
                continue
            done = subprocess.run([sys.executable, "-m", f"app.manual_tests.{suite}_offline_checks"], capture_output=True, text=True, timeout=900)
            lines = [x for x in done.stdout.splitlines() if x.strip() and '"level"' not in x]
            results[suite] = "PASS" if done.returncode == 0 and lines and lines[-1].strip() == "All checks passed." else "FAIL"
    ok = True
    print(f"\n{'Scenario':<26} {'First proven in':<16} {'Offline result':<15} Proof")
    for name, first, proof, suites in SCENARIOS:
        result = "PASS" if suites is None else ("PASS" if all(results[s] == "PASS" for s in suites) else "FAIL")
        ok &= result == "PASS"
        print(f"{name:<26} {first:<16} {result:<15} {proof}")
    return ok


def main() -> int:
    os.environ["SANDBOX_WORKSPACE_ID"] = t701.SANDBOX
    part_a()
    if "--run-only" in sys.argv:
        print("\nAll checks passed.")
        return 0
    if not part_b():
        print("\nA scenario suite failed.")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
