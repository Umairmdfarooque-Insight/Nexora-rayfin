"""The report validation stage (T-603, Gate G5): check the generated report against the approved
model and record the result in ValidationReport.json.

A plain function. `run_report_validation` reads the current PBIR folder, runs G5.SCHEMA and G5.BIND
on the files, then (only if those pass) publishes the folder to the sandbox, runs G5.QUERY against
the deployed model and G5.RENDER on the published report. It appends a `Report` entry to
ValidationReport.json and changes no run state: its callers do. `validate_report` moves the run:
any failed check gives REPORT_FAILED, and a model defect (an object that is in the ModelSpec but
whose query fails) goes on to MODEL_GENERATING with the reason, after a failed Gate G4 entry is
recorded for the model, so the model can't be approved as it is. A report defect stays in
REPORT_FAILED for the repair service. If everything passes the run stays in REPORT_GENERATING.
"""

from pathlib import Path

from app.compiler.critical_terms import ConfigError
from app.connectors.powerbi_api import PowerBiClient
from app.contracts.model_plan import ModelSpec, load_yaml
from app.contracts.validation_report import ValidationCheck, ValidationRun, load_report, save_report
from app.deploy.fabric_items import SEMANTIC_MODEL, FabricItemsClient, FabricOperationFailed, get_sandbox_workspace_id
from app.model.validators import check, skipped
from app.report.pbir_generator import folder_digest
from app.report.pbir_reader import PageRead, read_report
from app.report.validators import check_binding, check_query, check_render, check_schema
from app.store.base import RunStore
from app.workflow.clarification import NotFound
from app.workflow.compile_stage import WrongState
from app.workflow.harvest_stage import write_json
from app.workflow.model_stage import TMDL_ARTIFACT
from app.workflow.plan_stage import MODEL_FILE
from app.workflow.report_stage import ARTIFACT, PUBLISH_FILE, REPORT_DIR, ModelNotDeployed, report_name
from app.workflow.run import Run, utc_now
from app.workflow.states import RunState
from app.workflow.validate_stage import REPORT_FILE, register_report

VALIDATE_STATES = (RunState.REPORT_GENERATING,)
REPORT = "Report"  # the `artifact` of this stage's entries in ValidationReport.json


class ReportEnvironment:
    """Everything one report validation needs, loaded once so a repair loop can validate repeatedly."""

    def __init__(self, store: RunStore, runs_root: Path, run_id: str):
        self.run_id = run_id
        self.run_dir = Path(runs_root) / run_id
        workspace_id = get_sandbox_workspace_id()
        if not workspace_id:
            raise ConfigError("SANDBOX_WORKSPACE_ID is not set. Reports are only validated in the sandbox workspace.")
        self.workspace_id = workspace_id
        if not (self.run_dir / MODEL_FILE).is_file():
            raise NotFound("The run has no approved model yet")
        self.spec = load_yaml(ModelSpec, self.run_dir / MODEL_FILE)

    @property
    def model_name(self) -> str:
        return f"Nexora_{self.run_id[:8]}"


def _publish_file(env: ReportEnvironment, version: int, published: dict, model_id: str) -> None:
    write_json(
        env.run_dir / PUBLISH_FILE,
        {
            "version": version,
            "workspace_id": env.workspace_id,
            "report_name": report_name(env.run_id),
            "report_item_id": published["item_id"],
            "operation": published["operation"],
            "semantic_model_item_id": model_id,
            "published_at": utc_now(),
        },
    )


def run_report_validation(
    env: ReportEnvironment,
    folder: Path,
    version: int,
    expected_hash: str | None,
    *,
    items_client: FabricItemsClient,
    pbi: PowerBiClient,
    repair_attempt: int | None = None,
) -> ValidationRun:
    """Run the G5 checks on `folder` and append the result to ValidationReport.json. Changes no run
    state and registers no artifact."""
    folder = Path(folder)
    started = utc_now()
    digest = folder_digest(folder)
    checks: list[ValidationCheck] = []
    if expected_hash is not None and digest != expected_hash:
        checks.append(check("G5.SCHEMA", "report", "warning", f"The PBIR files differ from generated version {version}: the files as they are now were validated", "FILES_CHANGED", {"defect": None}))
    checks += check_schema(folder)
    pages: list[PageRead] = read_report(folder)
    checks += check_binding(pages, env.spec)
    local_failed = any(c.status == "fail" for c in checks)

    model = items_client.get_item_by_name(env.workspace_id, env.model_name, SEMANTIC_MODEL)  # the current id: a redeployed model rebinds
    if model is None:
        raise ModelNotDeployed(f"The semantic model {env.model_name!r} isn't in the sandbox workspace: validate the model first (POST /runs/{{id}}/model/validate)")
    unbound = {c.object for c in checks if c.id == "G5.BIND" and c.status == "fail"}
    checks += check_query(pages, env.spec, pbi, env.workspace_id, model["id"], unbound)

    if local_failed:
        checks += [skipped("G5.RENDER", f"page:{p.name}", "the report has failing schema or binding checks, so it wasn't published") for p in pages]
    else:
        try:
            published = items_client.upsert_report(env.workspace_id, report_name(env.run_id), folder)
        except FabricOperationFailed as exc:
            checks.append(check("G5.RENDER", "report", "fail", f"Fabric rejected the report: {exc}", "PUBLISH_FAILED", {"defect": "report", "details": exc.details}))
            checks += [skipped("G5.RENDER", f"page:{p.name}", "the report was not published") for p in pages]
        else:
            _publish_file(env, version, published, model["id"])
            checks += check_render(pages, folder, pbi, env.workspace_id, published["item_id"])

    report = load_report(env.run_dir / REPORT_FILE, env.run_id)
    entry = ValidationRun(
        artifact=REPORT,
        artifact_version=version,
        artifact_hash=digest,
        attempt=len(report.runs_of(REPORT)) + 1,
        started_at=started,
        passed=not any(c.status == "fail" for c in checks),
        repair_attempt=repair_attempt,
        checks=checks,
    )
    report.runs.append(entry)
    save_report(report, env.run_dir / REPORT_FILE)
    return entry


def model_defects(entry: ValidationRun) -> list[ValidationCheck]:
    return [c for c in entry.checks if c.status == "fail" and c.evidence.get("defect") == "model"]


def route_model_defects(store: RunStore, runs_root: Path, run_id: str, entry: ValidationRun, actor: str = "system") -> Run:
    """The report's queries found a model defect. Record a failed Gate G4 entry for the current
    model, so it can't be approved as it is, and move the run REPORT_FAILED -> MODEL_GENERATING with
    the reason. The report isn't patched. The run must be in REPORT_FAILED."""
    run = store.get(run_id)
    defects = model_defects(entry)
    tmdl_ref = run.artifacts.get(TMDL_ARTIFACT)
    run_dir = Path(runs_root) / run_id
    reasons = [c.message for c in defects]
    if tmdl_ref is not None:
        failing = [
            check(
                "G4.DAX", "model", "fail",
                f"A report query failed against the deployed model: {c.message}", "REPORT_QUERY_FAILED",
                {"routed_from": "G5.QUERY", "visual": c.object, "query": c.evidence.get("query"), "response": c.evidence.get("response")},
            )
            for c in defects
        ]
        report = load_report(run_dir / REPORT_FILE, run_id)
        model_entry = ValidationRun(
            artifact="SemanticModel", artifact_version=tmdl_ref.version, artifact_hash=tmdl_ref.hash,
            attempt=len(report.runs_of("SemanticModel")) + 1, started_at=utc_now(), passed=False, checks=failing,
        )
        report.runs.append(model_entry)
        save_report(report, run_dir / REPORT_FILE)
        register_report(store, runs_root, run_id, model_entry)
    reason = "Model defect found by report validation: " + "; ".join(reasons)
    return store.transition(run_id, RunState.MODEL_GENERATING, reason[:400], actor)


def validate_report(
    store: RunStore,
    runs_root: Path,
    run_id: str,
    *,
    items_client: FabricItemsClient,
    pbi: PowerBiClient,
    actor: str = "system",
) -> tuple[ValidationRun, Run]:
    """`POST /runs/{id}/report/validate`: returns this validation run's entry and the run."""
    run = store.get(run_id)
    if run.state not in VALIDATE_STATES:
        raise WrongState("Validating the report", run.state, VALIDATE_STATES)
    ref = run.artifacts.get(ARTIFACT)
    run_dir = Path(runs_root) / run_id
    if ref is None or not (run_dir / ref.path).is_dir():
        raise NotFound("The report hasn't been generated yet: POST /runs/{id}/report/generate first")
    env = ReportEnvironment(store, runs_root, run_id)
    entry = run_report_validation(env, run_dir / ref.path, ref.version, ref.hash, items_client=items_client, pbi=pbi)
    return entry, settle(store, runs_root, run_id, entry, actor)


def settle(store: RunStore, runs_root: Path, run_id: str, entry: ValidationRun, actor: str = "system") -> Run:
    """Register the validation entry and move the run by its result: a pass keeps REPORT_GENERATING,
    a failure moves to REPORT_FAILED, and a model defect goes on to MODEL_GENERATING."""
    register_report(store, runs_root, run_id, entry)
    run = store.get(run_id)
    if not entry.passed:
        failed = sum(1 for c in entry.checks if c.status == "fail")
        run = store.transition(run_id, RunState.REPORT_FAILED, f"Report validation failed: {failed} check(s)", actor)
        if model_defects(entry):
            run = route_model_defects(store, runs_root, run_id, entry, actor)
    return run
