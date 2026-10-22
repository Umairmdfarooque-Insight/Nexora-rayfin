"""The report generation stage (T-602): ReportPlan.json -> a PBIR folder -> the sandbox workspace.

A plain function, deterministic, no LLM. The files are generated and checked in a staging folder,
the folder is published from there (create, or update if the report exists), and only after Fabric
accepts it does it become `runs/<id>/report/v<n>/`, get registered and move the run from
MODEL_APPROVED to REPORT_GENERATING. A failure at any step leaves no new version and no state
change. A run already in REPORT_GENERATING can generate and publish again (a new version).
"""

import json
import shutil
from pathlib import Path

from app.compiler.critical_terms import ConfigError
from app.contracts.model_plan import ModelSpec, load_yaml, to_data as spec_data
from app.contracts.report_plan import ReportPlan
from app.deploy.fabric_items import SEMANTIC_MODEL, FabricItemsClient, get_sandbox_workspace_id
from app.fsutil import replace_file
from app.report.pbir_generator import PbirGenerationError, check_pbir, folder_digest, generate_pbir
from app.report.plan_validation import failures, validate_report_plan
from app.store.base import RunStore
from app.workflow.clarification import NotFound, StaleVersion
from app.workflow.compile_stage import WrongState
from app.workflow.harvest_stage import canonical_hash, write_json
from app.workflow.model_stage import TMDL_ARTIFACT
from app.workflow.plan_stage import MODEL_ARTIFACT, MODEL_FILE
from app.workflow.report_plan_stage import ARTIFACT as PLAN_ARTIFACT
from app.workflow.report_plan_stage import CURRENT as PLAN_FILE
from app.workflow.report_plan_stage import SEMANTIC_MODEL as BASED_ON_MODEL
from app.workflow import scope_guard
from app.workflow.run import ArtifactRef, Run, utc_now
from app.workflow.stale import assert_fresh
from app.workflow.states import RunState

ARTIFACT = "PbirReport"
REPORT_DIR = "report"
PUBLISH_FILE = "ReportPublish.json"
GENERATE_STATES = (RunState.MODEL_APPROVED, RunState.REPORT_GENERATING)


class ReportPlanInvalid(ValueError):
    """The saved plan no longer passes its checks, or has nothing to generate; nothing was written."""


class ModelNotDeployed(NotFound):
    """The approved model isn't in the sandbox workspace yet."""


def _next_version(report_root: Path) -> int:
    found = [int(p.name[1:]) for p in report_root.glob("v*") if p.is_dir() and p.name[1:].isdigit()]
    return max(found, default=0) + 1


def report_name(run_id: str) -> str:
    return f"Nexora_{run_id[:8]}_Report"  # the model is Nexora_<id>; another name since both live in one workspace


def generate_report(
    store: RunStore,
    runs_root: Path,
    run_id: str,
    items_client: FabricItemsClient,
    actor: str = "system",
    expect_only: set[str] | None = None,
) -> tuple[int, list[str], dict, Run]:
    """`POST /runs/{id}/report/generate`. Returns the version, its files (relative to
    `report/v<n>/`), the publish result and the run.

    `expect_only` (T-701, targeted regeneration) is the set of files (or folders, ending in `/`) that
    may differ from the report version in place; any other difference raises ScopeViolation before
    anything is published."""
    run = store.get(run_id)
    if run.state not in GENERATE_STATES:
        raise WrongState("Generating the report", run.state, GENERATE_STATES)
    assert_fresh(run.artifacts, (PLAN_ARTIFACT, TMDL_ARTIFACT), "Generating the report")
    run_dir = Path(runs_root) / run_id
    plan_ref = run.artifacts.get(PLAN_ARTIFACT)
    if plan_ref is None or not (run_dir / PLAN_FILE).is_file():
        raise NotFound("No report plan yet: POST /runs/{id}/report/plan first")
    model_ref = run.artifacts.get(MODEL_ARTIFACT)
    tmdl_ref = run.artifacts.get(TMDL_ARTIFACT)
    if model_ref is None or tmdl_ref is None or not (run_dir / MODEL_FILE).is_file():
        raise NotFound("The run has no approved model yet")
    spec = load_yaml(ModelSpec, run_dir / MODEL_FILE)
    spec_hash = canonical_hash(spec_data(spec))
    if spec_hash != model_ref.hash:
        raise StaleVersion(model_ref.hash, spec_hash, "model spec")

    data = json.loads((run_dir / PLAN_FILE).read_text(encoding="utf-8"))
    plan, result = validate_report_plan(data, spec)  # catches a hand edit, before anything is published
    if plan is None or not result.passed:
        raise ReportPlanInvalid("The report plan is not valid, so no report was generated: " + "; ".join(failures(result)[:10]))
    if plan.unanswered_questions:
        raise ReportPlanInvalid(
            "The plan has unanswered questions (" + ", ".join(u.question_id for u in plan.unanswered_questions)
            + "): return to the model or drop the question before generating the report"
        )
    if plan.based_on.get(BASED_ON_MODEL) != tmdl_ref.hash:
        raise StaleVersion(plan.based_on.get(BASED_ON_MODEL, ""), tmdl_ref.hash, "report plan (the model changed: plan the report again)")

    workspace_id = get_sandbox_workspace_id()
    if not workspace_id:
        raise ConfigError("SANDBOX_WORKSPACE_ID is not set. Reports are only published to the sandbox workspace.")
    model_name = f"Nexora_{run_id[:8]}"
    model = items_client.get_item_by_name(workspace_id, model_name, SEMANTIC_MODEL)  # the current id, so a redeployment rebinds
    if model is None:
        raise ModelNotDeployed(f"The semantic model {model_name!r} isn't in the sandbox workspace: validate the model first (POST /runs/{{id}}/model/validate)")

    report_root = run_dir / REPORT_DIR
    version = _next_version(report_root)
    staging = report_root / f".v{version}.tmp"
    if staging.exists():
        shutil.rmtree(staging)
    try:
        try:
            paths = generate_pbir(plan, model["id"], staging, spec)
        except PbirGenerationError as exc:
            raise ReportPlanInvalid(f"The report couldn't be generated: {exc}") from None
        problems = check_pbir(staging)
        if problems:
            raise ReportPlanInvalid("The generated report fails the PBIR checks: " + "; ".join(problems[:10]))
        files = [p.relative_to(staging).as_posix() for p in paths]
        previous = run_dir / run.artifacts[ARTIFACT].path if ARTIFACT in run.artifacts else None
        scope_guard.check("report", previous, staging, expect_only)
        digest = folder_digest(staging)
        published = items_client.upsert_report(workspace_id, report_name(run_id), staging)
        replace_file(staging, report_root / f"v{version}")
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    write_json(
        run_dir / PUBLISH_FILE,
        {
            "version": version,
            "workspace_id": workspace_id,
            "report_name": report_name(run_id),
            "report_item_id": published["item_id"],
            "operation": published["operation"],
            "semantic_model_item_id": model["id"],
            "published_at": utc_now(),
        },
    )
    run = store.get(run_id)
    run.artifacts[ARTIFACT] = ArtifactRef(
        path=f"{REPORT_DIR}/v{version}",
        version=version,
        hash=digest,
        status="draft",
        based_on={PLAN_ARTIFACT: canonical_hash(data), BASED_ON_MODEL: tmdl_ref.hash},
        updated_at=utc_now(),
    )
    store.save(run)
    if run.state is RunState.MODEL_APPROVED:
        run = store.transition(run_id, RunState.REPORT_GENERATING, f"Report v{version} generated and published", actor)
    return version, files, published, run
