"""T-703 offline checks: the tamper-evident event log, the audit timeline and the trace of a visual.

Run: python -m app.manual_tests.t703_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
Fabric, Power BI and the models are fakes (the fixtures of t701 and t702); a run is taken to COMPLETED in
a temporary folder, so no sign-in or network is needed. The live steps are in MANUAL_TESTING.md.
"""

import json
import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.deps import (
    get_dax_llm,
    get_fabric_client,
    get_items_client,
    get_powerbi_client,
    get_promotion_items_client,
    get_recon_llm,
    get_report_llm,
    get_report_services,
    get_runs_root,
    get_sql_connector_factory,
    get_store,
)
from app.api.main import app
from app.api.security import AuthContext, Principal, authenticate
from app.contracts import business_contract as bc
from app.contracts.metadata_inventory import ColumnProfile, ProfileEvidence, TableProfile
from app.contracts.model_plan import save_yaml, to_data
from app.contracts.source_resolution import FieldMapping, KpiFields, SourceResolution
from app.manual_tests import t502_offline_checks as t502
from app.manual_tests import t503_offline_checks as t503
from app.manual_tests import t504_offline_checks as t504
from app.manual_tests import t601_offline_checks as t601
from app.manual_tests import t701_offline_checks as t701
from app.manual_tests import t702_offline_checks as t702
from app.store.artifacts import LocalArtifactStore
from app.store.file_store import FileRunStore
from app.workflow import audit, promotion, roles as roles_module
from app.workflow.compile_stage import save_contract_version
from app.workflow.harvest_stage import canonical_hash, write_json
from app.workflow.report_review import ReportServices
from app.workflow.run import ArtifactRef, RunScope, utc_now
from app.workflow.states import RunState

check = t502.check
S = RunState
USER = t702.USER
NR, MON = t503.NR, t503.MON
TABLE = "saleslt.SalesLT_SalesOrderDetail"


def main() -> None:
    os.environ["SANDBOX_WORKSPACE_ID"] = t701.SANDBOX
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        roles_file = tmp / "roles.yaml"
        roles_file.write_text(
            f"roles:\n  business_owner: [{USER}]\n  bi_engineer: [{USER}]\n  project_reviewer: [{USER}]\n  release_approver: [{USER}]\n", encoding="utf-8"
        )
        original = roles_module.CONFIG_PATH
        roles_module.CONFIG_PATH = roles_file
        try:
            run_checks(tmp)
        finally:
            roles_module.CONFIG_PATH = original
            app.dependency_overrides.clear()
    print("All checks passed.")


def run_checks(tmp: Path) -> None:
    runs_root = tmp / "runs"
    store = FileRunStore(runs_root)
    # T-703's Fabric rework: events.jsonl goes through app.store.factory.get_artifact_store(), which
    # (ARTIFACT_STORE=local) always points at PROJECT_ROOT/runs; this check needs it to point at the
    # temporary runs_root instead, the same way `get_runs_root` is overridden below for everything else.
    original_get_artifact_store = audit.get_artifact_store
    audit.get_artifact_store = lambda: LocalArtifactStore(runs_root)
    items, pbi, sql = t701.Items(), t701.Pbi(), t504.FakeSql()
    recon = t504.ReconLlm({NR: t504.RECON_ANSWER, MON: t504.MON_RECON_ANSWER})
    dax_llm = t503.FakeLlm()
    report_llm = t601.FakeLlm(t701.report_answer())
    target_items, target_pbi, workspaces = t702.TargetItems(), t702.TargetPbi(), t702.Workspaces()
    app.dependency_overrides.update(
        {
            get_store: lambda: store,
            get_runs_root: lambda: runs_root,
            get_dax_llm: lambda: dax_llm,
            get_recon_llm: lambda: recon,
            get_items_client: lambda: items,
            get_powerbi_client: lambda: t702.Router(pbi, target_pbi),
            get_promotion_items_client: lambda: target_items,
            get_fabric_client: lambda: workspaces,
            get_sql_connector_factory: lambda: (lambda scope: sql),
            get_report_llm: lambda: report_llm,
            get_report_services: lambda: ReportServices(llm=report_llm, items=lambda: items, pbi=lambda: pbi),
            authenticate: lambda: AuthContext(principal=Principal(oid="o-1", upn=USER, name="T"), token="x"),
        }
    )
    client = TestClient(app, raise_server_exceptions=False)

    def envelope(r, status, code):
        return r.status_code == status and r.json()["error"]["code"] == code

    def approve(rid, artifact, role=None):
        review = {"SemanticModel": "model", "Report": "report"}.get(artifact)
        expected = client.get(f"/runs/{rid}/{review}/review").json()[f"{review}_hash"] if review else (
            promotion.release_hash(store.get(rid)) if artifact == "Release" else store.get(rid).artifacts[artifact].hash)
        body = {"artifact": artifact, "action": "ACCEPT", "expected_hash": expected}
        if role:
            body["role"] = role
        return client.post(f"/runs/{rid}/approvals", json=body)

    # --- a run taken to COMPLETED ------------------------------------------------------------------------------
    prompt = "Show net revenue by product"
    run = store.create(prompt)
    rid = run.run_id
    run_dir = runs_root / rid
    run_dir.mkdir(parents=True, exist_ok=True)
    spec = t503.spec_with_measures(run_id=rid)
    plan = t502.build_plan(spec)
    save_yaml(spec, run_dir / "ModelSpec.yaml")
    save_yaml(plan, run_dir / "ExecutionPlan.yaml")
    inventory = t502.build_inventory(spec)
    for t in inventory.tables:
        if f"{t.schema_name}.{t.table_name}" == TABLE:
            t.profile = TableProfile(
                status="ok", row_count=1200, evidence=ProfileEvidence(queries=["SELECT COUNT_BIG(*) FROM [saleslt].[SalesLT_SalesOrderDetail]"], profiled_at="2026-10-01T00:00:00Z"),
                columns={"LineTotal": ColumnProfile(null_count=3, distinct_count=900, min=0.5, max=9000.0)},
            )
    write_json(run_dir / "MetadataInventory.json", inventory.to_dict())
    resolution = SourceResolution(
        run_id=rid, version=1,
        kpi_fields=[
            KpiFields(kpi=NR, fields=[FieldMapping(need="order line amount", table=TABLE, column="LineTotal", role="measure")]),
            KpiFields(kpi=MON, fields=[FieldMapping(need="order line amount", table=TABLE, column="LineTotal", role="measure")]),
        ],
    )
    write_json(run_dir / "SourceResolution.json", resolution.to_dict())
    contract = bc.BusinessContract(run_id=rid, business_objective="Revenue", entities=["Sales"], dimensions=["Product"],
                                   kpis=[t503.KPI_NR, t503.KPI_MON], status=bc.ContractStatus.APPROVED)
    save_contract_version(store, runs_root, run, contract, status="approved")
    run = store.get(rid)
    run.scope = RunScope(workspace_id=t502.WS, lakehouse_id=t502.LH, lakehouse_name="SalesLakehouse", sql_endpoint=t502.ENDPOINT)
    for name, model in (("ModelSpec", spec), ("ExecutionPlan", plan)):
        run.artifacts[name] = ArtifactRef(path=f"{name}.yaml", version=1, hash=canonical_hash(to_data(model)), status="approved", updated_at=utc_now())
    for name, status in (("MetadataInventory", "approved"), ("SourceResolution", "draft")):
        run.artifacts[name] = ArtifactRef(path=f"{name}.json", version=1, hash=f"sha256:{name}", status=status, updated_at=utc_now())
    run.state = S.PLAN_APPROVED
    store.save(run)
    # the contract's approval record, as POST /approvals writes it
    assert client.post(f"/runs/{rid}/model/generate").status_code == 200
    dax_llm.answers = {NR: [t503.NR_ANSWER], MON: [t503.MON_ANSWER]}
    assert client.post(f"/runs/{rid}/model/measures").status_code == 200
    assert client.post(f"/runs/{rid}/model/validate").json()["report"]["passed"]
    assert approve(rid, "SemanticModel", "bi_engineer").status_code == 200 and approve(rid, "SemanticModel", "business_owner").status_code == 200
    assert client.post(f"/runs/{rid}/report/plan").status_code == 200
    assert client.post(f"/runs/{rid}/report/generate").status_code == 200
    assert client.post(f"/runs/{rid}/report/validate").json()["report"]["passed"]
    assert approve(rid, "Report", "project_reviewer").status_code == 200
    check("approving writes audit events straight away, without anyone opening the audit", (run_dir / "events.jsonl").is_file())
    assert approve(rid, "Report", "business_owner").status_code == 200
    assert approve(rid, "Release").status_code == 200
    assert client.post(f"/runs/{rid}/deploy", json={"target_workspace_id": t702.TARGET}).status_code == 202
    check("setup: the run is COMPLETED", store.get(rid).state is S.COMPLETED)

    # the contract has no approval record in this fixture (it was saved approved by hand): add the one POST /approvals
    # would have written, so the trace has an approver to show
    from app.workflow.approval import Action, ApprovalRecord, _append

    cref = store.get(rid).artifacts["BusinessContract"]
    _append(runs_root, ApprovalRecord(approval_id="contract-approval", run_id=rid, artifact="BusinessContract", artifact_version=cref.version, artifact_hash=cref.hash,
                                      action=Action.ACCEPT, role="business_owner", user={"oid": "o-1", "upn": USER}, at="2026-10-01T09:00:00Z", comment="KPIs are right"))

    # --- the timeline --------------------------------------------------------------------------------------------
    r = client.get(f"/runs/{rid}/audit")
    body = r.json()
    events = body["events"]
    types = {e["type"] for e in events}
    check("audit: 200, the chain is intact, every kind of event is there",
          r.status_code == 200 and body["chain_valid"] is True and body["first_broken_seq"] is None and body["total"] == len(events)
          and {"state", "approval", "artifact", "gate", "validation", "deployment"} <= types)
    check("audit: events are numbered from 1 without gaps and each holds the hash of the one before it",
          [e["seq"] for e in events] == list(range(1, len(events) + 1)) and events[0]["prev_event_hash"] == audit.GENESIS
          and all(events[i]["prev_event_hash"] == audit.event_hash(events[i - 1]) for i in range(1, len(events))))
    approvals = [e for e in events if e["type"] == "approval"]
    release = next(e for e in approvals if e["artifact"] == "Release")
    check("audit: who approved what, when, and the artifact hash (the release, the model by both roles, the report)",
          release["actor"] == USER and release["hash"] == promotion.release_hash(store.get(rid)) and release["at"]
          and sum(1 for e in approvals if e["artifact"] == "SemanticModel") == 2 and sum(1 for e in approvals if e["artifact"] == "Report") == 2
          and "as release_approver" in release["summary"])
    check("audit: the state changes (to DEPLOYING and COMPLETED), gate results, validations and the deployment outcome are in it",
          any("REPORT_APPROVED → DEPLOYING" in e["summary"] for e in events if e["type"] == "state")
          and any(e["summary"].startswith("Gate G6 passed") for e in events if e["type"] == "gate")
          and any("validation passed" in e["summary"] for e in events if e["type"] == "validation")
          and any(e["summary"].startswith("Deployment completed") and "SemanticModel sha256:" in e["summary"] for e in events if e["type"] == "deployment"))
    check("audit: artifact registrations carry the version and hash",
          any(e["artifact"] == "TMDLModel" and e["version"] and e["hash"].startswith("sha256:") for e in events if e["type"] == "artifact"))
    total = body["total"]
    check("a second look adds nothing: events are logged once", client.get(f"/runs/{rid}/audit").json()["total"] == total)
    page = client.get(f"/runs/{rid}/audit?offset=3&limit=5").json()
    check("paging: offset and limit return that slice, with the total", [e["seq"] for e in page["events"]] == [4, 5, 6, 7, 8] and page["total"] == total)
    check("a bad page size is 422", client.get(f"/runs/{rid}/audit?limit=0").status_code == 422)
    check("unknown run: 404", envelope(client.get("/runs/nope/audit"), 404, "RUN_NOT_FOUND"))

    # --- the trace ---------------------------------------------------------------------------------------------------
    r = client.get(f"/runs/{rid}/trace?visual_id=v1")
    t = r.json()
    check("trace v1: the visual, its measure with the DAX and the model version, from the model as deployed",
          r.status_code == 200 and t["visual"]["id"] == "v1" and t["visual"]["page"] == "Overview" and t["visual"]["questions"][0]["id"] == "Q-R2"
          and t["measure"]["name"] == MON and "Net Revenue" in t["measure"]["dax"] and t["measure"]["model_version"] == store.get(rid).artifacts["TMDLModel"].version)
    check("trace v1: the KPI as approved (definition, contract version, who and when)",
          t["kpi"]["definition"]["formula_intent"] == "Net revenue by month" and t["kpi"]["contract_version"] == cref.version
          and t["kpi"]["approved_by"] == USER and t["kpi"]["approved_at"] == "2026-10-01T09:00:00Z")
    check("trace v1: the source columns with their profile evidence, and the prompt",
          t["sources"] == [{"table": TABLE, "column": "LineTotal", "need": "order line amount", "role": "measure", "profile_evidence": t["sources"][0]["profile_evidence"]}]
          and t["sources"][0]["profile_evidence"]["distinct_count"] == 900 and t["sources"][0]["profile_evidence"]["row_count"] == 1200
          and t["sources"][0]["profile_evidence"]["queries"] and t["prompt"] == prompt and t["gaps"] == [])
    t2 = client.get(f"/runs/{rid}/trace?visual_id=v2").json()
    check("trace v2 is the Net Revenue chart", t2["measure"]["name"] == NR and t2["kpi"]["definition"]["name"] == NR and t2["gaps"] == [])
    check("a visual that doesn't exist: 404", envelope(client.get(f"/runs/{rid}/trace?visual_id=v99"), 404, "NOT_FOUND"))
    check("a missing visual_id is 422", client.get(f"/runs/{rid}/trace").status_code == 422)

    # --- gaps are explicit ---------------------------------------------------------------------------------------------------
    contract_file = run_dir / "contracts" / f"BusinessContract.v{cref.version}.yaml"
    saved = contract_file.read_bytes()
    contract_file.unlink()
    model_folder = run_dir / store.get(rid).artifacts["TMDLModel"].path
    moved = model_folder.with_name("moved-away")
    model_folder.rename(moved)
    t = client.get(f"/runs/{rid}/trace?visual_id=v1").json()
    check("a missing contract version and a missing model version are named as gaps, not skipped",
          t["kpi"]["definition"] is None and t["kpi"]["contract_version"] == cref.version
          and any(g.startswith(f"Contract version {cref.version} is missing") for g in t["gaps"])
          and any(g.startswith(f"Model version {store.get(rid).artifacts['TMDLModel'].version} is missing") for g in t["gaps"]) and t["measure"]["dax"])
    moved.rename(model_folder)
    contract_file.write_bytes(saved)
    inventory_file = run_dir / "MetadataInventory.json"
    inv_saved = inventory_file.read_bytes()
    data = json.loads(inv_saved)
    for tbl in data["tables"]:
        tbl["profile"] = None
    inventory_file.write_text(json.dumps(data), encoding="utf-8")
    t = client.get(f"/runs/{rid}/trace?visual_id=v1").json()
    check("a source column with no profile shows the column and a gap", t["sources"][0]["profile_evidence"] is None and any("No profile evidence" in g for g in t["gaps"]))
    inventory_file.write_bytes(inv_saved)

    # --- tamper evidence ---------------------------------------------------------------------------------------------------------------
    log = run_dir / "events.jsonl"
    original = log.read_text(encoding="utf-8")
    lines = original.splitlines()
    edited = json.loads(lines[2])
    edited["actor"] = "someone.else@contoso.com"
    log.write_text("\n".join([*lines[:2], json.dumps(edited), *lines[3:]]) + "\n", encoding="utf-8")
    body = client.get(f"/runs/{rid}/audit").json()
    check("an edited line: chain_valid is false and the first broken seq (the event after the edit) is shown", body["chain_valid"] is False and body["first_broken_seq"] == 4)
    log.write_text("\n".join([*lines[:4], *lines[5:]]) + "\n", encoding="utf-8")
    body = client.get(f"/runs/{rid}/audit").json()
    check("a removed line breaks the chain at the line after it", body["chain_valid"] is False and body["first_broken_seq"] == 5)
    log.write_text("\n".join([*lines[:6], "not json", *lines[7:]]) + "\n", encoding="utf-8")
    body = client.get(f"/runs/{rid}/audit").json()
    check("an unreadable line breaks the chain there and is shown as unreadable",
          body["chain_valid"] is False and body["first_broken_seq"] == 7 and any(e.get("unreadable") for e in body["events"]))
    log.write_text("\n".join([lines[1], lines[0], *lines[2:]]) + "\n", encoding="utf-8")
    check("reordered lines break the chain at the first", client.get(f"/runs/{rid}/audit").json()["first_broken_seq"] == 1)
    log.write_text(original, encoding="utf-8")
    check("the original log verifies again", client.get(f"/runs/{rid}/audit").json()["chain_valid"] is True)

    # --- a long log is paged --------------------------------------------------------------------------------------------------------------
    rid2 = store.create("Another run").run_id
    for _ in range(250):
        run = store.get(rid2)
        run.history.append(run.history[0].model_copy(update={"reason": f"entry {len(run.history)}"}))
        store.save(run)
    body = client.get(f"/runs/{rid2}/audit").json()
    check("a run with many events: a page of 200 by default and the total", len(body["events"]) == 200 and body["total"] == 251 and body["chain_valid"] is True)
    check("the next page continues where the first stopped", [e["seq"] for e in client.get(f"/runs/{rid2}/audit?offset=200").json()["events"]] == list(range(201, 252)))
    check("a run with no report plan has nothing to trace: 404", envelope(client.get(f"/runs/{rid2}/trace?visual_id=v1"), 404, "NOT_FOUND"))

    audit.get_artifact_store = original_get_artifact_store


if __name__ == "__main__":
    main()
