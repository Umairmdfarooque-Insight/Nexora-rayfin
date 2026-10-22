"""The model validation stage (T-504, Gate G4): deploy the current TMDL version to the sandbox,
refresh it, run the G4 checks and record them in ValidationReport.json.

A plain function, deterministic apart from the reconciliation SQL (see `model/recon_sql.py`). If
any check fails the run moves MODEL_GENERATING -> MODEL_FAILED; if all pass it stays in
MODEL_GENERATING, awaiting the review of T-507. Source data is only read, and the only writes go to
the sandbox model. A refresh or deployment that Fabric rejects is recorded as a failed G4.DAX check
and the checks that need the deployed model are marked skipped; anything else that goes wrong
(access denied, throttling, timeouts) is an upstream error and changes nothing.
"""

from pathlib import Path
from typing import Any

from app.compiler.critical_terms import ConfigError
from app.connectors.powerbi_api import PowerBiClient, RefreshFailed
from app.contracts.model_plan import ModelSpec, load_yaml
from app.contracts.validation_report import ValidationRun, load_report, save_report
from app.deploy.fabric_items import FabricItemsClient, FabricOperationFailed, get_sandbox_workspace_id
from app.model.recon_sql import LlmCall, ReconSql, propose_recon_sql
from app.model.tmdl_reader import read_model
from app.model.bridge_checks import check_bridges
from app.model.validation_config import load_tolerance
from app.model.validators import (
    Context,
    check,
    check_dax,
    check_reconciliation,
    check_references,
    check_relationships,
    check_rls,
    skipped,
)
from app.store.base import RunStore
from app.workflow.clarification import NotFound
from app.workflow.compile_stage import WrongState, load_current_contract
from app.workflow.harvest_stage import ScopeMissing, canonical_hash, load_inventory
from app.workflow.llm_cache import CachedLlm, read_answers, write_answers
from app.workflow.model_stage import TMDL_ARTIFACT, folder_hash
from app.workflow.plan_stage import MODEL_FILE
from app.workflow.run import ArtifactRef, Run, utc_now
from app.workflow.states import RunState

REPORT_ARTIFACT = "ValidationReport"
REPORT_FILE = "ValidationReport.json"
CACHE = "recon_suggestions.json"
VALIDATE_STATES = (RunState.MODEL_GENERATING,)


class Environment:
    """Everything one validation run needs, loaded once so a repair loop can validate repeatedly."""

    def __init__(self, store: RunStore, runs_root: Path, run_id: str):
        run = store.get(run_id)
        self.run_dir = Path(runs_root) / run_id
        self.run_id = run_id
        if run.scope is None:
            raise ScopeMissing()
        if run.scope.sql_endpoint is None:
            raise ScopeMissing("The run's Lakehouse has no SQL endpoint, so the model can't be reconciled.")
        self.scope = run.scope
        workspace_id = get_sandbox_workspace_id()
        if not workspace_id:
            raise ConfigError("SANDBOX_WORKSPACE_ID is not set. Models are only validated in the sandbox workspace.")
        self.workspace_id = workspace_id
        self.tolerance = load_tolerance()  # CONFIG_ERROR before anything is deployed
        contract = load_current_contract(runs_root, run_id)
        if contract is None:
            raise NotFound("The run has no business contract yet")
        self.contract = contract
        self.inventory = load_inventory(runs_root, run_id)
        self.spec = load_yaml(ModelSpec, self.run_dir / MODEL_FILE)


def run_validation(
    env: Environment,
    folder: Path,
    version: int,
    expected_hash: str | None,
    *,
    spec: ModelSpec | None = None,
    items_client: FabricItemsClient,
    pbi: PowerBiClient,
    sql_connector_factory: Any,
    recon_llm: LlmCall,
    user_upn: str | None = None,
    repair_attempt: int | None = None,
    baseline: dict[str, float | None] | None = None,
) -> ValidationRun:
    """Deploy `folder` to the sandbox, refresh it, run the G4 checks and append the result to
    ValidationReport.json. It changes no run state and registers no artifact: its callers do
    (the validation endpoint, and the repair service after its graph)."""
    spec = spec or env.spec
    digest = folder_hash(folder)
    started = utc_now()
    tmdl = read_model(folder)

    cache = read_answers(env.run_dir, CACHE)
    accepted: dict[str, str] = {}
    memo: dict[str, ReconSql] = {}

    def recon_for(kpi) -> ReconSql:
        if kpi.name not in memo:
            cached = CachedLlm(recon_llm, cache)
            memo[kpi.name] = propose_recon_sql(kpi, spec, env.inventory, cached)
            accepted[cached.last_key] = cached.last_raw
        return memo[kpi.name]

    ctx = Context(
        spec=spec, contract=env.contract, tmdl=tmdl, tolerance=env.tolerance, workspace_id=env.workspace_id,
        dataset_id="", pbi=pbi, sql_connector=sql_connector_factory(env.scope), recon_for=recon_for, user_upn=user_upn,
        baseline=baseline or {},
    )

    checks = []
    if expected_hash is not None and digest != expected_hash:
        checks.append(check("G4.REF", "model", "warning", f"The TMDL files differ from generated version {version}: the files as they are now were validated", "FILES_CHANGED"))
    checks += check_references(ctx)

    failure = None
    deployed: dict | None = None
    try:
        deployed = items_client.upsert_semantic_model(env.workspace_id, f"Nexora_{env.run_id[:8]}", folder)
        ctx.dataset_id = deployed["item_id"]
        pbi.refresh(env.workspace_id, ctx.dataset_id)
    except RefreshFailed as exc:
        failure = ("REFRESH_FAILED", f"The refresh failed: {exc}")
    except FabricOperationFailed as exc:
        failure = ("DEPLOY_FAILED", f"Fabric rejected the model: {exc}")

    if failure:
        code, message = failure
        checks.append(check("G4.DAX", "model", "fail", message, code, {"workspace_id": env.workspace_id, "item": deployed}))
        for id_ in ("G4.REL", "G4.RECON", "G4.RLS"):
            checks.append(skipped(id_, "model", message))
    else:
        checks += check_dax(ctx)
        with ctx.sql_connector.connect() as connection:
            checks += check_relationships(ctx, connection)
            checks += check_reconciliation(ctx, connection)
            checks += check_bridges(ctx, connection)
        checks += check_rls(ctx)

    report = load_report(env.run_dir / REPORT_FILE, env.run_id)
    entry = ValidationRun(
        artifact_version=version,
        artifact_hash=digest,
        attempt=len(report.runs_of("SemanticModel")) + 1,
        started_at=started,
        passed=not any(c.status == "fail" for c in checks),
        repair_attempt=repair_attempt,
        checks=checks,
    )
    report.runs.append(entry)
    save_report(report, env.run_dir / REPORT_FILE)
    if accepted:
        write_answers(env.run_dir, CACHE, {**cache, **accepted})
    return entry


def register_report(store: RunStore, runs_root: Path, run_id: str, entry: ValidationRun) -> None:
    """Register the report artifact for the latest validation run (after the run was saved)."""
    from app.observability import telemetry  # T-704: every validation run is a pass or a fail

    telemetry.record_validation(entry.artifact, entry.passed, run_id)
    run_dir = Path(runs_root) / run_id
    data = load_report(run_dir / REPORT_FILE, run_id).model_dump(mode="json")
    run = store.get(run_id)
    run.artifacts[REPORT_ARTIFACT] = ArtifactRef(
        path=REPORT_FILE,
        version=entry.attempt,
        hash=canonical_hash(data),
        status="passed" if entry.passed else "failed",
        based_on={(TMDL_ARTIFACT if entry.artifact == "SemanticModel" else "PbirReport"): entry.artifact_hash},
        updated_at=utc_now(),
    )
    store.save(run)


def validate_model(
    store: RunStore,
    runs_root: Path,
    run_id: str,
    *,
    items_client: FabricItemsClient,
    pbi: PowerBiClient,
    sql_connector_factory: Any,
    recon_llm: LlmCall,
    user_upn: str | None = None,
    actor: str = "system",
) -> tuple[ValidationRun, Run]:
    """`POST /runs/{id}/model/validate`: returns this validation run's entry and the run."""
    run = store.get(run_id)
    if run.state not in VALIDATE_STATES:
        raise WrongState("Validating the model", run.state, VALIDATE_STATES)
    tmdl_ref = run.artifacts.get(TMDL_ARTIFACT)
    run_dir = Path(runs_root) / run_id
    if tmdl_ref is None or not (run_dir / tmdl_ref.path).is_dir():
        raise NotFound("The model hasn't been generated yet: POST /runs/{id}/model/generate first")
    env = Environment(store, runs_root, run_id)
    entry = run_validation(
        env, run_dir / tmdl_ref.path, tmdl_ref.version, tmdl_ref.hash,
        items_client=items_client, pbi=pbi, sql_connector_factory=sql_connector_factory,
        recon_llm=recon_llm, user_upn=user_upn,
    )
    register_report(store, runs_root, run_id, entry)
    run = store.get(run_id)
    if not entry.passed:
        failed = sum(1 for c in entry.checks if c.status == "fail")
        run = store.transition(run_id, RunState.MODEL_FAILED, f"Model validation failed: {failed} check(s)", actor)
    return entry, run
