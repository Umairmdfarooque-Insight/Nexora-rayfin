"""T-702 offline checks: the release approval, Gate G6, the hash check, the lock, the backup, the smoke
tests, completion and rollback of a promotion deployment, and the new Fabric client calls.

Run: python -m app.manual_tests.t702_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
Fabric, Power BI and the models are fakes (extending the fixtures of t701), the roles are a temporary
file and the runs are built by hand in a temporary folder, so no sign-in or network is needed. The live
steps are in MANUAL_TESTING.md.
"""

import base64
import json
import os
import tempfile
import time
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
from app.connectors.fabric_api import FabricForbidden
from app.contracts import business_contract as bc
from app.contracts.model_plan import save_yaml, to_data
from app.deploy.fabric_items import REPORT, SEMANTIC_MODEL, FabricItemsClient, TargetNotAllowed
from app.manual_tests import t502_offline_checks as t502
from app.manual_tests import t503_offline_checks as t503
from app.manual_tests import t504_offline_checks as t504
from app.manual_tests import t601_offline_checks as t601
from app.manual_tests import t701_offline_checks as t701
from app.store import locks
from app.store.base import IllegalTransition, StaleArtifacts
from app.store.file_store import FileRunStore
from app.workflow import promotion, roles as roles_module
from app.workflow.compile_stage import save_contract_version
from app.workflow.harvest_stage import canonical_hash, write_json
from app.workflow.report_review import ReportServices
from app.workflow.run import ArtifactRef, RunScope, utc_now
from app.workflow.states import RunState

check = t502.check
S = RunState
USER = "both@contoso.com"
TARGET = "55555555-5555-4555-8555-555555555555"
OTHER = "66666666-6666-4666-8666-666666666666"
NR, MON = t503.NR, t503.MON


class TargetItems:
    """The target workspace as the promotion service sees it: items by (workspace, type, name)."""

    def __init__(self):
        self.store: dict[tuple, dict] = {}
        self.calls: list[tuple] = []
        self.fail_report = False
        self.fail_restore = False
        self.n = 0

    def seed(self, workspace, item_type, name, files: dict[str, bytes]):
        self.n += 1
        self.store[(workspace, item_type, name.casefold())] = {"id": f"old-{self.n}", "type": item_type, "files": dict(files)}

    @staticmethod
    def _read(folder: Path, prefix_ok) -> dict[str, bytes]:
        folder = Path(folder)
        return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file() and prefix_ok(p.relative_to(folder))}

    def get_item_by_name(self, workspace, name, item_type):
        item = self.store.get((workspace, item_type, name.casefold()))
        return {"id": item["id"], "displayName": name, "type": item_type} if item else None

    def _upsert(self, workspace, item_type, name, files):
        self.calls.append(("upsert", workspace, item_type, name))
        key = (workspace, item_type, name.casefold())
        operation = "updated" if key in self.store else "created"
        self.n += 1
        item_id = self.store[key]["id"] if key in self.store else f"new-{self.n}"
        self.store[key] = {"id": item_id, "type": item_type, "files": files}
        return {"item_id": item_id, "operation": operation}

    def upsert_semantic_model(self, workspace, name, tmdl_dir):
        return self._upsert(workspace, SEMANTIC_MODEL, name, self._read(tmdl_dir, lambda r: r.parts[0] == "definition" or r.name == "definition.pbism"))

    def upsert_report(self, workspace, name, pbir_dir):
        if self.fail_report:
            self.calls.append(("upsert", workspace, REPORT, name))
            raise RuntimeError("Fabric rejected the report")
        return self._upsert(workspace, REPORT, name, self._read(pbir_dir, lambda r: r.parts[0] == "definition" or r.name == "definition.pbir"))

    def _find(self, workspace, item_id):
        return next((k for k, v in self.store.items() if k[0] == workspace and v["id"] == item_id), None)

    def get_definition(self, workspace, item_id, collection):
        key = self._find(workspace, item_id)
        return [{"path": p, "payload": base64.b64encode(b).decode("ascii"), "payloadType": "InlineBase64"} for p, b in self.store[key]["files"].items()]

    def update_definition(self, workspace, item_id, collection, parts, fmt=None):
        self.calls.append(("restore", workspace, item_id))
        if self.fail_restore:
            raise RuntimeError("Fabric refused the restore")
        self.store[self._find(workspace, item_id)]["files"] = {p["path"]: base64.b64decode(p["payload"]) for p in parts}

    def delete_item(self, workspace, item_id):
        self.calls.append(("delete", workspace, item_id))
        del self.store[self._find(workspace, item_id)]

    def names(self, workspace):
        return sorted(k[2] for k in self.store if k[0] == workspace)


class TargetPbi:
    def __init__(self):
        self.fail_refresh = False
        self.fail_measure: str | None = None
        self.blank: str | None = None
        self.calls: list[str] = []

    def refresh(self, workspace_id, dataset_id, timeout_s=600):
        self.calls.append("refresh")
        if self.fail_refresh:
            raise RuntimeError("Direct Lake: no access to the source Lakehouse")

    def execute_dax(self, workspace_id, dataset_id, dax, impersonate=None):
        self.calls.append(dax)
        if self.fail_measure and f"[{self.fail_measure}]" in dax:
            raise RuntimeError(f"Measure {self.fail_measure} failed")
        if self.blank and f"[{self.blank}]" in dax:
            return [{"[v]": None}]
        return [{"[v]": 1.0}]


class Router:
    """The Power BI client of the API: the target workspace goes to the target fake, the sandbox to the other."""

    def __init__(self, sandbox, target):
        self.sandbox, self.target = sandbox, target

    def _for(self, workspace_id):
        return self.target if workspace_id == TARGET else self.sandbox

    def refresh(self, workspace_id, *a, **kw):
        return self._for(workspace_id).refresh(workspace_id, *a, **kw)

    def execute_dax(self, workspace_id, *a, **kw):
        return self._for(workspace_id).execute_dax(workspace_id, *a, **kw)

    def export_report_page(self, workspace_id, *a, **kw):
        return self.sandbox.export_report_page(workspace_id, *a, **kw)


class Workspaces:
    def __init__(self):
        self.ids = [TARGET]
        self.error: Exception | None = None

    def list_workspaces(self):
        if self.error:
            raise self.error
        return [{"id": i, "display_name": "w", "type": "Workspace"} for i in self.ids]


def main() -> None:
    os.environ["SANDBOX_WORKSPACE_ID"] = t701.SANDBOX
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        roles_file = tmp / "roles.yaml"
        with_release = f"roles:\n  business_owner: [{USER}]\n  bi_engineer: [{USER}]\n  project_reviewer: [{USER}]\n  release_approver: [{USER}]\n"
        roles_file.write_text(with_release, encoding="utf-8")
        original = roles_module.CONFIG_PATH
        roles_module.CONFIG_PATH = roles_file
        try:
            run_checks(tmp, roles_file, with_release)
        finally:
            roles_module.CONFIG_PATH = original
            app.dependency_overrides.clear()
    print("All checks passed.")


def run_checks(tmp: Path, roles_file: Path, with_release: str) -> None:
    runs_root = tmp / "runs"
    store = FileRunStore(runs_root)
    items, pbi, sql = t701.Items(), t701.Pbi(), t504.FakeSql()
    recon = t504.ReconLlm({NR: t504.RECON_ANSWER, MON: t504.MON_RECON_ANSWER})
    dax_llm = t503.FakeLlm()
    report_llm = t601.FakeLlm(t701.report_answer())
    target_items, target_pbi, workspaces = TargetItems(), TargetPbi(), Workspaces()
    app.dependency_overrides.update(
        {
            get_store: lambda: store,
            get_runs_root: lambda: runs_root,
            get_dax_llm: lambda: dax_llm,
            get_recon_llm: lambda: recon,
            get_items_client: lambda: items,
            get_powerbi_client: lambda: Router(pbi, target_pbi),
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

    def reset():
        dax_llm.answers = {NR: [t503.NR_ANSWER], MON: [t503.MON_ANSWER]}
        dax_llm.calls.clear()
        pbi.values = {NR: 1000.0, MON: 1000.0}
        sql.total = sql.mon_total = 1000.0

    def approve(rid, artifact, role=None, action="ACCEPT", expected=None):
        review = {"SemanticModel": "model", "Report": "report"}.get(artifact)
        if expected is None and review:
            expected = client.get(f"/runs/{rid}/{review}/review").json()[f"{review}_hash"]
        body = {"artifact": artifact, "action": action, "expected_hash": expected or store.get(rid).artifacts[artifact].hash}
        if role:
            body["role"] = role
        return client.post(f"/runs/{rid}/approvals", json=body)

    def new_run():
        run = store.create("Show net revenue by product")
        rid = run.run_id
        run_dir = runs_root / rid
        run_dir.mkdir(parents=True, exist_ok=True)
        spec = t503.spec_with_measures(run_id=rid)
        plan = t502.build_plan(spec)
        save_yaml(spec, run_dir / "ModelSpec.yaml")
        save_yaml(plan, run_dir / "ExecutionPlan.yaml")
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
        run.artifacts["MetadataInventory"] = ArtifactRef(path="MetadataInventory.json", version=1, hash="sha256:inv", status="approved", updated_at=utc_now())
        run.state = S.PLAN_APPROVED
        store.save(run)
        assert client.post(f"/runs/{rid}/model/generate").status_code == 200
        reset()
        assert client.post(f"/runs/{rid}/model/measures").status_code == 200
        assert client.post(f"/runs/{rid}/model/validate").json()["report"]["passed"]
        assert approve(rid, "SemanticModel", "bi_engineer").status_code == 200 and approve(rid, "SemanticModel", "business_owner").status_code == 200
        assert client.post(f"/runs/{rid}/report/plan").status_code == 200
        assert client.post(f"/runs/{rid}/report/generate").status_code == 200
        assert client.post(f"/runs/{rid}/report/validate").json()["report"]["passed"]
        assert approve(rid, "Report", "project_reviewer").status_code == 200 and approve(rid, "Report", "business_owner").status_code == 200
        assert store.get(rid).state is S.REPORT_APPROVED
        return rid

    def release(rid, **kw):
        return client.post(f"/runs/{rid}/approvals", json={"artifact": "Release", "action": kw.get("action", "ACCEPT"), "expected_hash": kw.get("expected") or promotion.release_hash(store.get(rid))})

    def deploy(rid, target=TARGET):
        return client.post(f"/runs/{rid}/deploy", json={"target_workspace_id": target})

    def failed_checks(r):
        return sorted(r.json()["error"]["details"]["failed_checks"])

    def hashes(rid):
        run = store.get(rid)
        return run.artifacts["TMDLModel"].hash, run.artifacts["PbirReport"].hash

    # --- 1. the release approval --------------------------------------------------------------------------------
    rid = new_run()
    check("setup: a run approved all the way (REPORT_APPROVED)", store.get(rid).state is S.REPORT_APPROVED)
    check("approving a release: only ACCEPT exists (422)", release(rid, action="REJECT").status_code in (422,))
    r = release(rid, expected="sha256:old")
    check("approving a release with an old hash: 409 STALE_VERSION", envelope(r, 409, "STALE_VERSION"))
    roles_file.write_text(with_release.replace(f"  release_approver: [{USER}]\n", ""), encoding="utf-8")
    check("approving a release without the release_approver role: 403 FORBIDDEN", envelope(release(rid), 403, "FORBIDDEN") and r.status_code == 409)
    roles_file.write_text(with_release, encoding="utf-8")

    # --- 2. Gate G6 --------------------------------------------------------------------------------------------------------
    r = deploy(rid)
    check("deploy without a release approval: 409 GATE_FAILED with G6.4 only, and nothing is deployed", envelope(r, 409, "GATE_FAILED") and failed_checks(r) == ["G6.4"]
          and not target_items.store and store.get(rid).state is S.REPORT_APPROVED)
    r = deploy(rid, t701.SANDBOX)
    check("deploy to the sandbox: G6.1 fails", envelope(r, 409, "GATE_FAILED") and "G6.1" in failed_checks(r) and "sandbox" in json.dumps(r.json()))
    r = deploy(rid, OTHER)
    check("deploy to a workspace the user can't access: G6.1 fails and nothing is deployed", envelope(r, 409, "GATE_FAILED") and "G6.1" in failed_checks(r) and not target_items.store)
    workspaces.error = FabricForbidden("denied")
    check("a workspace list that can't be read is 'not authorized', never a pass", "G6.1" in failed_checks(deploy(rid)))
    workspaces.error = None
    check("an invalid workspace id is 422", deploy(rid, "not-a-guid").status_code == 422)

    assert release(rid).status_code == 200
    check("the release approval is recorded: role, hash and user", [(a["artifact"], a["role"], a["user"]["upn"]) for a in client.get(f"/runs/{rid}/approvals").json()][-1] == ("Release", "release_approver", USER))
    check("approving the same release again: 409 ALREADY_APPROVED", envelope(release(rid), 409, "ALREADY_APPROVED"))
    run = store.get(rid)
    run.state = S.CREATED
    store.save(run)
    check("deploy from another state: 409 WRONG_STATE", envelope(deploy(rid), 409, "WRONG_STATE"))
    run.state = S.REPORT_APPROVED
    store.save(run)

    # --- 3. the hash check -----------------------------------------------------------------------------------------------------
    model_folder = runs_root / rid / store.get(rid).artifacts["TMDLModel"].path
    table = model_folder / "definition" / "tables" / "Sales.tmdl"
    original = table.read_bytes()
    table.write_bytes(original + b"\t// edited by hand\n")
    r = deploy(rid)
    check("a TMDL file edited after approval: 409 HASH_MISMATCH before anything is deployed, no lock left",
          envelope(r, 409, "HASH_MISMATCH") and r.json()["error"]["details"]["artifact"] == "TMDLModel" and not target_items.store
          and store.get(rid).state is S.REPORT_APPROVED and not list((runs_root / ".locks").glob("*")))
    table.write_bytes(original)

    # --- 4. the lock (T-702's Fabric rework: app.store.locks, a dbo.nexora_locks row or this file lock) ------------------------
    lock = locks.acquire(runs_root, TARGET, "another-run-id", "another-deployment-id", "another-run")
    check("another deployment to the same target is running: 409 TARGET_BUSY", envelope(deploy(rid), 409, "TARGET_BUSY") and store.get(rid).state is S.REPORT_APPROVED)
    old = time.time() - locks.LOCK_MAX_AGE - 60
    os.utime(lock.path, (old, old))
    locks.release(lock)
    lock = locks.acquire(runs_root, TARGET, "crashed-run-id", "crashed-deployment-id", "crashed")
    os.utime(lock.path, (old, old))
    again = locks.acquire(runs_root, TARGET, "next-run-id", "next-deployment-id", "next")
    check("a lock left by a crash (older than the limit) is replaced", again == lock and "next" in again.path.read_text(encoding="utf-8"))
    locks.release(again)

    # --- 5. stale artifacts --------------------------------------------------------------------------------------------------------
    run = store.get(rid)
    run.artifacts["TMDLModel"].status = "stale"
    store.save(run)
    r = deploy(rid)
    check("stale artifacts: G6.3 fails (and G6.2, since the model is no longer approved)", envelope(r, 409, "GATE_FAILED") and "G6.3" in failed_checks(r))
    check("approving a release with stale artifacts: 409 STALE_ARTIFACTS", envelope(release(rid), 409, "STALE_ARTIFACTS"))
    run = store.get(rid)
    run.artifacts["TMDLModel"].status = "approved"
    store.save(run)

    # --- 6. promote ---------------------------------------------------------------------------------------------------------------------
    model_hash, report_hash = hashes(rid)
    sandbox_files = {p: b for p, b in t701.tree(runs_root / rid / store.get(rid).artifacts["PbirReport"].path).items()}
    r = deploy(rid)
    run = store.get(rid)
    dep = run.deployment
    check("deploy: 202 with a deployment id, and the run is COMPLETED", r.status_code == 202 and r.json()["deployment_id"] == dep["deployment_id"] and run.state is S.COMPLETED)
    check("the model and the report are in the target workspace", target_items.names(TARGET) == sorted([f"nexora_{rid[:8]}".casefold(), f"nexora_{rid[:8]}_report".casefold()]))
    check("RunManifest.deployment: the target, the items with the approved hashes and operations, the backup path, the outcome",
          dep["target_workspace_id"] == TARGET and dep["outcome"] == "completed" and dep["finished_at"]
          and [(i["type"], i["hash"], i["operation"]) for i in dep["items"]] == [("SemanticModel", model_hash, "created"), ("Report", report_hash, "created")]
          and dep["backup_path"].startswith("deploy/backup-") and (runs_root / rid / dep["backup_path"] / "backup.json").is_file())
    check("every smoke test passed: refresh, each measure, the report definition",
          [t["id"] for t in dep["smoke_tests"]] == ["SMOKE.REFRESH", f"SMOKE.MEASURE:{MON}", f"SMOKE.MEASURE:{NR}", "SMOKE.REPORT"] and all(t["status"] == "pass" for t in dep["smoke_tests"]))
    report_in_target = target_items.store[(TARGET, REPORT, f"nexora_{rid[:8]}_report")]["files"]["definition.pbir"].decode("utf-8")
    model_id = target_items.store[(TARGET, SEMANTIC_MODEL, f"nexora_{rid[:8]}")]["id"]
    check("the report in the target is bound to the target's model, and the approved report folder is untouched",
          f"semanticmodelid={model_id}" in report_in_target and t701.tree(runs_root / rid / store.get(rid).artifacts["PbirReport"].path) == sandbox_files
          and hashes(rid) == (model_hash, report_hash))
    check("the lock is released, Gate G6 was saved and the run detail shows the deployment",
          not list((runs_root / ".locks").glob("*")) and list((runs_root / rid / "gates").glob("G6-*.json"))
          and client.get(f"/runs/{rid}").json()["deployment"]["outcome"] == "completed")

    # --- 7. rollback of a new deployment ------------------------------------------------------------------------------------------------
    rid2 = new_run()
    assert release(rid2).status_code == 200
    target_items.store.clear()
    target_pbi.fail_refresh = True
    r = deploy(rid2)
    run = store.get(rid2)
    check("a failing smoke test: the run is ROLLED_BACK and the items the deployment created are deleted (the target is as it was)",
          r.status_code == 202 and run.state is S.ROLLED_BACK and run.deployment["outcome"] == "rolled_back" and not target_items.store
          and "no access to the source Lakehouse" in run.deployment["error"] and [t["status"] for t in run.deployment["smoke_tests"]][0] == "fail"
          and {a["action"] for a in run.deployment["rollback"]} == {"deleted"})
    check("the lock is released after a rollback", not list((runs_root / ".locks").glob("*")))
    target_pbi.fail_refresh = False
    r = deploy(rid2)
    check("a rolled-back run can be deployed again once the cause is fixed", r.status_code == 202 and store.get(rid2).state is S.COMPLETED and len(target_items.store) == 2)

    # --- 8. rollback to the previous version ----------------------------------------------------------------------------------------------------
    rid3 = new_run()
    assert release(rid3).status_code == 200
    target_items.store.clear()
    old_model = {"definition/model.tmdl": b"previous model", "definition.pbism": b"{}"}
    old_report = {"definition.pbir": b'{"datasetReference": {"byConnection": {"connectionString": "semanticmodelid=old"}}}', "definition/report.json": b"{}"}
    target_items.seed(TARGET, SEMANTIC_MODEL, f"Nexora_{rid3[:8]}", old_model)
    target_items.seed(TARGET, REPORT, f"Nexora_{rid3[:8]}_Report", old_report)
    target_items.fail_report = True
    r = deploy(rid3)
    run = store.get(rid3)
    backup = runs_root / rid3 / run.deployment["backup_path"]
    check("the model deploys but the report fails: both are restored to their previous definitions and the run is ROLLED_BACK",
          run.state is S.ROLLED_BACK and run.deployment["outcome"] == "rolled_back"
          and target_items.store[(TARGET, SEMANTIC_MODEL, f"nexora_{rid3[:8]}")]["files"] == old_model
          and target_items.store[(TARGET, REPORT, f"nexora_{rid3[:8]}_report")]["files"] == old_report
          and {a["action"] for a in run.deployment["rollback"]} == {"restored"})
    check("the backup of the target's definitions is in the run folder", (backup / SEMANTIC_MODEL / "definition" / "model.tmdl").read_bytes() == b"previous model"
          and json.loads((backup / "backup.json").read_text(encoding="utf-8"))["items"][0]["existed"] is True)
    target_items.fail_report = False
    r = deploy(rid3)
    check("redeploying over existing items updates them", store.get(rid3).state is S.COMPLETED and [i["operation"] for i in store.get(rid3).deployment["items"]] == ["updated", "updated"])

    # --- 9. the rollback fails -------------------------------------------------------------------------------------------------------------------------
    rid4 = new_run()
    assert release(rid4).status_code == 200
    target_items.store.clear()
    target_items.seed(TARGET, SEMANTIC_MODEL, f"Nexora_{rid4[:8]}", old_model)
    target_items.fail_report = True
    target_items.fail_restore = True
    r = deploy(rid4)
    run = store.get(rid4)
    check("a rollback that fails: outcome rollback_failed, NEEDS_INPUT with a critical message for manual intervention",
          run.state is S.NEEDS_INPUT and run.deployment["outcome"] == "rollback_failed" and run.history[-1].reason.startswith("CRITICAL")
          and "manual intervention" in run.history[-1].reason and any(a["status"] == "failed" for a in run.deployment["rollback"]))
    check("the lock is released even then", not list((runs_root / ".locks").glob("*")))
    target_items.fail_report = target_items.fail_restore = False

    # --- 10. smoke test details ------------------------------------------------------------------------------------------------------------------------
    rid5 = new_run()
    assert release(rid5).status_code == 200
    target_items.store.clear()
    target_pbi.fail_measure = NR
    deploy(rid5)
    run = store.get(rid5)
    check("a measure whose query fails fails its smoke test and rolls back", run.state is S.ROLLED_BACK
          and [t["id"] for t in run.deployment["smoke_tests"] if t["status"] == "fail"] == [f"SMOKE.MEASURE:{NR}"] and not target_items.store)
    target_pbi.fail_measure, target_pbi.blank = None, MON
    deploy(rid5)
    run = store.get(rid5)
    check("a blank measure is a warning, not a failure", run.state is S.COMPLETED and any(t["status"] == "warning" and t["id"] == f"SMOKE.MEASURE:{MON}" for t in run.deployment["smoke_tests"]))
    target_pbi.blank = None

    # --- 11. a completed run is final; the stale guard -------------------------------------------------------------------------------------------------
    check("deploying a COMPLETED run: 409 WRONG_STATE", envelope(deploy(rid5), 409, "WRONG_STATE"))
    run = store.get(rid)
    run.artifacts["TMDLModel"].status = "stale"
    store.save(run)
    try:
        store.transition(rid, S.DEPLOYING, "x")
        blocked = False
    except (StaleArtifacts, IllegalTransition):
        blocked = True
    check("nothing leaves COMPLETED for DEPLOYING", blocked)

    # --- 12. the Fabric client calls ------------------------------------------------------------------------------------------------------------------------
    sent: list = []

    class Resp:
        def __init__(self, status=200, body=None, headers=None):
            self.status_code, self._body, self.headers = status, body if body is not None else {}, headers or {}

        def json(self):
            return self._body

    queue: list = []

    def http(method, url, headers=None, json=None, params=None, timeout=None):
        sent.append((method, url, json))
        return queue.pop(0)

    parts = [{"path": "definition.pbir", "payload": "e30=", "payloadType": "InlineBase64"}]
    client_ = FabricItemsClient(token_provider=lambda: "t", http=http, sleep=lambda s: None, allow_any_workspace=True)
    queue[:] = [Resp(200, {"definition": {"parts": parts}})]
    check("getDefinition: a report is asked for in PBIR format and its parts returned", client_.get_definition("w", "i", "reports") == parts
          and sent[0][0] == "POST" and sent[0][1].endswith("/workspaces/w/reports/i/getDefinition?format=PBIR"))
    sent.clear()
    queue[:] = [Resp(202, {}, {"Location": "https://api.fabric.microsoft.com/v1/operations/op-1"}), Resp(200, {"status": "Succeeded"}), Resp(200, {"definition": {"parts": parts}})]
    check("getDefinition: a long-running answer is polled and its /result read", client_.get_definition("w", "i", "semanticModels") == parts and sent[-1][1].endswith("/operations/op-1/result"))
    queue[:] = [Resp(200, {})]
    try:
        client_.get_definition("w", "i", "semanticModels")
        empty = False
    except Exception:
        empty = True
    check("getDefinition: an empty definition is an error", empty)
    sent.clear()
    queue[:] = [Resp(200, {})]
    client_.update_definition("w", "i", "semanticModels", parts)
    check("updateDefinition posts the parts", sent[0][1].endswith("/semanticModels/i/updateDefinition") and sent[0][2] == {"definition": {"parts": parts}})
    queue[:] = [Resp(200, {})]
    client_.delete_item("w", "i")
    check("delete_item calls DELETE on the item", sent[-1][:2] == ("DELETE", "https://api.fabric.microsoft.com/v1/workspaces/w/items/i"))
    guarded = FabricItemsClient(token_provider=lambda: "t", http=http)
    try:
        guarded.delete_item("not-the-sandbox", "i")
        refused = False
    except TargetNotAllowed:
        refused = True
    check("the new calls stay behind the sandbox guard unless the promotion service built the client", refused)
    check("only the promotion route builds the unguarded client", "allow_any_workspace=True" in Path("app/api/deps.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
