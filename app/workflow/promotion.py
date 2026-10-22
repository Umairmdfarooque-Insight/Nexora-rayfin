"""Promotion deployment (T-702, FR-019, Gate G6): the approved model and report to a higher workspace.

`decide_release` records the final release approval (role `release_approver`). `deploy` checks Gate G6
(an authorized target that isn't the sandbox, G1-G5 passed on the current hashes, nothing stale, a release
approval on the current hashes), checks the files on disk against the approved hashes, takes the target's
lock and moves the run to DEPLOYING; `run_deployment` then backs up what the target already has, deploys
the model and the report, runs the smoke tests and moves the run to COMPLETED. Any failure restores the
backup (or deletes what the deployment created) and moves the run to ROLLED_BACK; a rollback that fails is
`rollback_failed` and NEEDS_INPUT. This service is the only caller of the Fabric items client allowed past
the sandbox guard (`allow_any_workspace=True`).
"""

import base64
import json
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.contracts.model_plan import ModelSpec, load_yaml
from app.contracts.validation_report import load_report
from app.deploy.fabric_items import (
    REPORT,
    SEMANTIC_MODEL,
    FabricItemsClient,
    get_sandbox_workspace_id,
)
from app.model.validators import _dax_name
from app.report.pbir_generator import folder_digest
from app.store import locks
from app.store.base import RunStore
from app.workflow import returns
from app.workflow.approval import (
    Action,
    AlreadyApproved,
    ApprovalRecord,
    Forbidden,
    GateFailed,
    Outcome,
    StaleVersion,
    UnsupportedAction,
    _append,
    list_approvals,
)
from app.workflow.compile_stage import WrongState
from app.workflow.gates import GateCheck, GateResult, save_gate_result
from app.workflow.harvest_stage import canonical_hash, write_json
from app.workflow.model_stage import TMDL_ARTIFACT, folder_hash
from app.workflow.plan_stage import MODEL_ARTIFACT, MODEL_FILE
from app.workflow.report_plan_stage import ARTIFACT as REPORT_PLAN
from app.workflow.report_stage import ARTIFACT as PBIR, report_name
from app.workflow.roles import load_roles
from app.workflow.run import Run, utc_now
from app.workflow.stale import StaleArtifacts, stale_names
from app.workflow.states import RunState

S = RunState
RELEASE_ARTIFACT = "Release"
RELEASE_ROLE = "release_approver"
DEPLOY_STATES = (S.REPORT_APPROVED, S.ROLLED_BACK)
MODEL_ID = re.compile(r"semanticmodelid=[^\"\s;]+")


class HashMismatch(Exception):
    """A file on disk differs from the approved artifact hash; nothing was deployed."""

    def __init__(self, artifact: str, expected: str, actual: str):
        self.artifact, self.expected, self.actual = artifact, expected, actual
        super().__init__(f"The files of {artifact} on disk don't match the approved hash, so nothing was deployed")


TargetBusy = locks.TargetBusy  # re-exported: callers caught this from here before the T-702 Fabric rework


class SmokeFailed(Exception):
    def __init__(self, failed: list[dict]):
        self.failed = failed
        super().__init__("Smoke tests failed: " + ", ".join(f"{t['id']} ({t.get('message', '')})" for t in failed))


# --- the release approval -------------------------------------------------------------------------------


def release_hash(run: Run) -> str | None:
    """What a release approval covers: the approved model and report, by hash."""
    model, report = run.artifacts.get(TMDL_ARTIFACT), run.artifacts.get(PBIR)
    if model is None or report is None:
        return None
    return canonical_hash({TMDL_ARTIFACT: model.hash, PBIR: report.hash})


def release_approved(store: RunStore, runs_root: Path, run: Run) -> ApprovalRecord | None:
    """The release approval on the current hashes, given after the last return, or None."""
    current = release_hash(run)
    records = list_approvals(store, runs_root, run.run_id)
    after = returns.reopened_after(runs_root, run.run_id) or 0
    for index in range(len(records) - 1, after - 1, -1):
        r = records[index]
        if r.artifact == RELEASE_ARTIFACT and r.action is Action.ACCEPT and r.artifact_hash == current:
            return r
    return None


def decide_release(
    store: RunStore, runs_root: Path, run_id: str, action: Action, user: dict[str, str], expected_hash: str, comment: str | None
) -> Outcome:
    """Record the final release approval. Raises UnsupportedAction, Forbidden, WrongState, StaleArtifacts,
    StaleVersion or AlreadyApproved; then nothing is recorded."""
    if action is not Action.ACCEPT:
        raise UnsupportedAction(RELEASE_ARTIFACT, action)
    if not load_roles().has(user["upn"], RELEASE_ROLE):
        raise Forbidden(RELEASE_ROLE, action, f"Only a {RELEASE_ROLE} can approve a release")
    run = store.get(run_id)
    if run.state is not S.REPORT_APPROVED:
        raise WrongState("Approving the release", run.state, (S.REPORT_APPROVED,))
    stale = stale_names(run.artifacts)
    if stale:
        raise StaleArtifacts("Approving the release", stale)
    current = release_hash(run)
    if current is None or current != expected_hash:
        raise StaleVersion(expected_hash, current or "", "release")
    if release_approved(store, runs_root, run):
        raise AlreadyApproved("This release is already approved")
    record = ApprovalRecord(
        approval_id=str(uuid.uuid4()), run_id=run_id, artifact=RELEASE_ARTIFACT, artifact_version=run.artifacts[PBIR].version,
        artifact_hash=current, action=action, role=RELEASE_ROLE, user={"oid": user["oid"], "upn": user["upn"]}, at=utc_now(), comment=comment,
    )
    _append(runs_root, record)
    return Outcome(approval=record, run=run)


# --- Gate G6 ----------------------------------------------------------------------------------------------


def _g6_2(run: Run, runs_root: Path) -> tuple[bool, dict]:
    """Gates G1-G5 on the current hashes, from the approved artifacts and the latest validations."""
    arts = run.artifacts
    failed: list[str] = []

    def need(gate: str, *names: str) -> None:
        for n in names:
            if n not in arts or arts[n].status != "approved":
                failed.append(f"{gate}: {n} is not approved")

    need("G1", "BusinessContract")
    need("G2", "MetadataInventory")
    need("G3", "ExecutionPlan", MODEL_ARTIFACT)
    need("G4", TMDL_ARTIFACT)
    need("G5", PBIR, REPORT_PLAN)
    report = load_report(Path(runs_root) / run.run_id / "ValidationReport.json", run.run_id)
    for gate, artifact, ref_name in (("G4", "SemanticModel", TMDL_ARTIFACT), ("G5", "Report", PBIR)):
        latest = report.latest(artifact)
        ref = arts.get(ref_name)
        if latest is None or not latest.passed or ref is None or latest.artifact_hash != ref.hash:
            failed.append(f"{gate}: no passing validation of the current {ref_name}")
    return not failed, {"failed": failed}


def run_g6(
    store: RunStore, runs_root: Path, run: Run, target_workspace_id: str, workspaces: Callable[[], list[dict]]
) -> GateResult:
    """Gate G6 (spec §12) for a deployment of `run` to `target_workspace_id`."""
    sandbox = get_sandbox_workspace_id()
    target = target_workspace_id.strip().casefold()
    is_sandbox = bool(sandbox) and target == sandbox.strip().casefold()
    authorized, reason = False, None
    if not is_sandbox:
        try:
            authorized = any(str(w.get("id", "")).casefold() == target for w in workspaces())
            reason = None if authorized else "the workspace isn't one the signed-in user can access"
        except Exception as exc:  # a refusal or an outage is "not authorized", never a pass
            reason = f"the workspace list couldn't be read ({type(exc).__name__})"
    ok2, ev2 = _g6_2(run, runs_root)
    stale = stale_names(run.artifacts)
    release = release_approved(store, runs_root, run)
    checks = [
        GateCheck(
            id="G6.1", description="The target workspace is authorized for the user and isn't the sandbox",
            status="pass" if authorized and not is_sandbox else "fail",
            evidence={"target": target_workspace_id, **({"reason": "the target is the sandbox workspace"} if is_sandbox else {"reason": reason} if reason else {})},
        ),
        GateCheck(id="G6.2", description="Gates G1-G5 have passed on the current hashes", status="pass" if ok2 else "fail", evidence=ev2),
        GateCheck(id="G6.3", description="No stale artifacts", status="pass" if not stale else "fail", evidence={"stale": stale}),
        GateCheck(
            id="G6.4", description="Final release approval recorded (release_approver)", status="pass" if release else "fail",
            evidence={"approval_id": release.approval_id, "by": release.user["upn"]} if release else {"reason": "no release approval on the current hashes"},
        ),
    ]
    return GateResult(
        gate="G6", artifact="Release", artifact_hash=release_hash(run) or "", passed=all(c.status == "pass" for c in checks),
        checked_at=utc_now(), checks=checks,
    )


# --- starting a deployment ---------------------------------------------------------------------------------


@dataclass
class Services:
    """What a deployment needs, built by the API from the signed-in user."""

    items: Any  # FabricItemsClient(allow_any_workspace=True)
    pbi: Any  # PowerBiClient
    workspaces: Callable[[], list[dict]]  # the workspaces the user can access


@dataclass
class Started:
    deployment_id: str
    target: str
    lock: locks.LockHandle


def _check_hashes(run: Run, runs_root: Path) -> None:
    run_dir = Path(runs_root) / run.run_id
    model, report = run.artifacts[TMDL_ARTIFACT], run.artifacts[PBIR]
    for name, ref, digest in ((TMDL_ARTIFACT, model, folder_hash), (PBIR, report, folder_digest)):
        folder = run_dir / ref.path
        actual = digest(folder) if folder.is_dir() else ""
        if actual != ref.hash:
            raise HashMismatch(name, ref.hash, actual)


def _set(store: RunStore, run_id: str, **changes: Any) -> None:
    run = store.get(run_id)
    run.deployment = {**(run.deployment or {}), **changes}
    store.save(run)


def start_deployment(
    store: RunStore, runs_root: Path, run_id: str, target_workspace_id: str, services: Services, actor: str
) -> Started:
    """The checks before anything is deployed, then DEPLOYING. Raises WrongState, GateFailed, HashMismatch or
    TargetBusy; then nothing has changed."""
    run = store.get(run_id)
    if run.state not in DEPLOY_STATES:
        raise WrongState("Deploying", run.state, DEPLOY_STATES)
    gate = run_g6(store, runs_root, run, target_workspace_id, services.workspaces)
    save_gate_result(runs_root, run_id, gate)
    if not gate.passed:
        raise GateFailed(gate)
    _check_hashes(run, runs_root)
    deployment_id = str(uuid.uuid4())
    lock = locks.acquire(runs_root, target_workspace_id, run_id, deployment_id, actor)
    try:
        run = store.get(run_id)
        run.deployment = {
            "deployment_id": deployment_id, "target_workspace_id": target_workspace_id, "started_at": utc_now(), "finished_at": None,
            "items": [], "backup_path": None, "smoke_tests": [], "outcome": None,
        }
        store.save(run)
        store.transition(run_id, S.DEPLOYING, f"Deploying to workspace {target_workspace_id}", actor)
    except Exception:
        locks.release(lock)
        raise
    return Started(deployment_id, target_workspace_id, lock)


# --- running it ----------------------------------------------------------------------------------------------


def _rebound_report(folder: Path, model_item_id: str, out: Path) -> Path:
    """A copy of the approved report folder bound to the target's model (definition.pbir is the only file
    that names the model; the approved hash stays that of the folder as approved)."""
    shutil.copytree(folder, out, ignore=shutil.ignore_patterns("renders"))
    pbir = out / "definition.pbir"
    text = pbir.read_text(encoding="utf-8")
    pbir.write_bytes(MODEL_ID.sub(f"semanticmodelid={model_item_id}", text).encode("utf-8"))
    return out


def _backup(items: Any, target: str, planned: list[dict], backup_dir: Path) -> None:
    """Save the definition of each planned item that already exists in the target (getDefinition)."""
    manifest = []
    for p in planned:
        existing = items.get_item_by_name(target, p["name"], p["type"])
        entry = {"type": p["type"], "name": p["name"], "existed": existing is not None, "item_id": existing["id"] if existing else None}
        if existing:
            parts = items.get_definition(target, existing["id"], p["collection"])
            for part in parts:
                path = backup_dir / p["type"] / part["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(base64.b64decode(part["payload"]))
            entry["parts"] = p["parts"] = [x["path"] for x in parts]
        manifest.append(entry)
        p["existed"], p["item_id"] = entry["existed"], entry["item_id"]
    write_json(backup_dir / "backup.json", {"target_workspace_id": target, "items": manifest})


def _parts_from_backup(backup_dir: Path, item_type: str, paths: list[str]) -> list[dict]:
    return [
        {"path": p, "payload": base64.b64encode((backup_dir / item_type / p).read_bytes()).decode("ascii"), "payloadType": "InlineBase64"}
        for p in paths
    ]


def smoke_tests(items: Any, pbi: Any, target: str, model_id: str, report_id: str, spec: ModelSpec) -> list[dict]:
    """The model refreshes, `EVALUATE ROW` on each measure returns a value, the report definition can be read."""
    results: list[dict] = []

    def run_test(id_: str, fn: Callable[[], str | None]) -> None:
        try:
            note = fn()
            results.append({"id": id_, "status": "warning" if note else "pass", **({"message": note} if note else {})})
        except Exception as exc:
            results.append({"id": id_, "status": "fail", "message": str(exc)[:500]})

    run_test("SMOKE.REFRESH", lambda: pbi.refresh(target, model_id))
    for m in sorted(spec.measures, key=lambda m: m.name):
        if not m.dax:
            continue

        def measure(name: str = m.name) -> str | None:
            rows = pbi.execute_dax(target, model_id, f'EVALUATE ROW("v", {_dax_name(name)})')
            if not rows:
                raise ValueError("the query returned no row")
            return "the measure is blank" if next(iter(rows[0].values()), None) is None else None

        run_test(f"SMOKE.MEASURE:{m.name}", measure)

    def report() -> None:
        parts = items.get_definition(target, report_id, "reports")
        if not any(p.get("path") == "definition.pbir" for p in parts):
            raise ValueError("the report definition has no definition.pbir")

    run_test("SMOKE.REPORT", report)
    return results


def _rollback(items: Any, target: str, planned: list[dict], backup_dir: Path) -> tuple[list[dict], bool]:
    """Restore what existed (from the backup) and delete what the deployment created. Returns each
    action's result and whether all of them worked."""
    done, ok = [], True
    for p in reversed(planned):
        if not p.get("attempted"):
            continue
        try:
            if p["existed"]:
                parts = _parts_from_backup(backup_dir, p["type"], p["parts"])
                items.update_definition(target, p["item_id"], p["collection"], parts, "PBIR" if p["type"] == REPORT else None)
                done.append({"item": p["name"], "type": p["type"], "action": "restored", "status": "done"})
            else:
                found = items.get_item_by_name(target, p["name"], p["type"])  # also catches one that was half created
                if found:
                    items.delete_item(target, found["id"])
                done.append({"item": p["name"], "type": p["type"], "action": "deleted" if found else "nothing to delete", "status": "done"})
        except Exception as exc:
            ok = False
            done.append({"item": p["name"], "type": p["type"], "action": "restore" if p["existed"] else "delete", "status": "failed", "message": str(exc)[:500]})
    return done, ok


def run_deployment(
    store: RunStore, runs_root: Path, run_id: str, started: Started, services: Services, actor: str = "system"
) -> None:
    """Back up, deploy, smoke-test and settle the run. Never raises: the outcome is in the run."""
    from app.observability.telemetry import stage_span  # T-704: the background job is a stage of its own

    with stage_span("deploy.run", run_id=run_id) as span:
        _run_deployment(store, runs_root, run_id, started, services, actor, span)


def _run_deployment(
    store: RunStore, runs_root: Path, run_id: str, started: Started, services: Services, actor: str, span: Any
) -> None:
    target = started.target
    run_dir = Path(runs_root) / run_id
    planned: list[dict] = []
    backup_dir = run_dir / "deploy" / f"backup-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    try:
        run = store.get(run_id)
        model_ref, report_ref = run.artifacts[TMDL_ARTIFACT], run.artifacts[PBIR]
        spec = load_yaml(ModelSpec, run_dir / MODEL_FILE)
        planned = [
            {"type": SEMANTIC_MODEL, "collection": "semanticModels", "name": f"Nexora_{run_id[:8]}"},
            {"type": REPORT, "collection": "reports", "name": report_name(run_id)},
        ]
        _backup(services.items, target, planned, backup_dir)
        _set(store, run_id, backup_path=backup_dir.relative_to(run_dir).as_posix() + "/")

        items: list[dict] = []
        model_plan, report_plan = planned
        model_plan["attempted"] = True
        deployed = services.items.upsert_semantic_model(target, model_plan["name"], run_dir / model_ref.path)
        items.append({"type": SEMANTIC_MODEL, "item_id": deployed["item_id"], "hash": model_ref.hash, "operation": deployed["operation"]})
        _set(store, run_id, items=list(items))
        report_plan["attempted"] = True
        with tempfile.TemporaryDirectory() as tmp:
            rebound = _rebound_report(run_dir / report_ref.path, deployed["item_id"], Path(tmp) / "report")
            published = services.items.upsert_report(target, report_plan["name"], rebound)
        items.append({"type": REPORT, "item_id": published["item_id"], "hash": report_ref.hash, "operation": published["operation"]})
        _set(store, run_id, items=list(items))

        tests = smoke_tests(services.items, services.pbi, target, deployed["item_id"], published["item_id"], spec)
        _set(store, run_id, smoke_tests=tests)
        failed = [t for t in tests if t["status"] == "fail"]
        if failed:
            raise SmokeFailed(failed)
        _set(store, run_id, finished_at=utc_now(), outcome="completed")
        store.transition(run_id, S.COMPLETED, "Deployment and smoke tests passed", actor)
    except Exception as exc:
        reason = str(exc)[:300] or type(exc).__name__
        try:
            actions, ok = _rollback(services.items, target, planned, backup_dir)
        except Exception as rollback_error:  # the rollback itself blew up
            actions, ok = [{"action": "rollback", "status": "failed", "message": str(rollback_error)[:300]}], False
        span.set_outcome("rolled_back" if ok else "rollback_failed")
        _set(store, run_id, finished_at=utc_now(), outcome="rolled_back" if ok else "rollback_failed", error=reason, rollback=actions)
        if ok:
            store.transition(run_id, S.ROLLED_BACK, f"Deployment failed and was rolled back: {reason}", actor)
        else:
            left = ", ".join(a.get("item", "?") for a in actions if a["status"] == "failed")
            store.transition(
                run_id, S.NEEDS_INPUT,
                f"CRITICAL: the deployment failed ({reason}) and the rollback failed for {left}: manual intervention is needed in workspace {target}", actor,
            )
    finally:
        locks.release(started.lock)
        from app.workflow import audit  # avoids a circular import

        audit.record(store, runs_root, run_id)


# --- resuming after a cancelled or crashed job (T-702 FR-3) -------------------------------------------------

_COLLECTIONS = {SEMANTIC_MODEL: "semanticModels", REPORT: "reports"}


def resume_rollback(store: RunStore, runs_root: Path, run_id: str, services: Services, actor: str) -> None:
    """A deployment job that was cancelled or crashed leaves the run in `DEPLOYING` with nothing further
    written. The next `POST /runs/{id}/deploy` while the state is still `DEPLOYING` runs only the rollback,
    from the backup the crashed job already took, and releases the lock; nothing is redeployed. Raises
    `WrongState` if the run isn't `DEPLOYING`. Otherwise never raises: the outcome is in the run, like
    `run_deployment`."""
    from app.observability.telemetry import stage_span

    run = store.get(run_id)
    if run.state is not S.DEPLOYING:
        raise WrongState("Resuming a deployment", run.state, (S.DEPLOYING,))
    d = run.deployment or {}
    target = d.get("target_workspace_id")
    backup_rel = d.get("backup_path")
    run_dir = Path(runs_root) / run_id
    if not target or not backup_rel:
        # The crash happened before anything was backed up: there is nothing to restore, only a state
        # to leave DEPLOYING from.
        _set(store, run_id, finished_at=utc_now(), outcome="rolled_back", error="Resumed with no backup recorded: nothing to restore")
        store.transition(run_id, S.ROLLED_BACK, "Resumed after a cancelled or crashed deployment: no backup was taken yet, nothing to restore", actor)
        if target:
            locks.release_by_workspace(runs_root, target)
        return
    backup_dir = run_dir / backup_rel
    try:
        manifest = json.loads((backup_dir / "backup.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {"items": []}
    deployed_types = {i["type"] for i in d.get("items", [])}
    planned: list[dict] = []
    for entry in manifest.get("items", []):
        p = dict(entry)
        p["collection"] = _COLLECTIONS.get(p["type"], p["type"])
        p["attempted"] = p["type"] in deployed_types
        planned.append(p)
    with stage_span("deploy.run", run_id=run_id) as span:
        try:
            actions, ok = _rollback(services.items, target, planned, backup_dir)
        except Exception as exc:  # the rollback itself blew up
            actions, ok = [{"action": "rollback", "status": "failed", "message": str(exc)[:300]}], False
        span.set_outcome("rolled_back" if ok else "rollback_failed")
        _set(store, run_id, finished_at=utc_now(), outcome="rolled_back" if ok else "rollback_failed", rollback=actions)
        if ok:
            store.transition(run_id, S.ROLLED_BACK, "Resumed after a cancelled or crashed deployment: rolled back", actor)
        else:
            left = ", ".join(a.get("item", "?") for a in actions if a["status"] == "failed")
            store.transition(
                run_id, S.NEEDS_INPUT,
                f"CRITICAL: resuming the deployment's rollback failed for {left}: manual intervention is needed in workspace {target}", actor,
            )
    locks.release_by_workspace(runs_root, target)
    from app.workflow import audit  # avoids a circular import

    audit.record(store, runs_root, run_id)
