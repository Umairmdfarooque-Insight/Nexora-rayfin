"""The model generation stage (T-502): turn the approved ModelSpec into a versioned TMDL project.

A plain function, deterministic, no LLM. `write_model_version` checks and writes the files of the
next `runs/<id>/model/v<n>/` folder and registers the `TMDLModel` artifact; `generate_model` adds the
state rule and the move `PLAN_APPROVED -> MODEL_GENERATING` (or `MODEL_FAILED -> MODEL_GENERATING`
when repair calls it). Existing version folders are never modified.
"""

import hashlib
import re
import shutil
from pathlib import Path

from app.contracts.model_plan import ExecutionPlan, ModelSpec, load_yaml, to_data
from app.fsutil import replace_file
from app.model.tmdl_generator import generate_tmdl
from app.store.base import RunStore
from app.workflow.clarification import NotFound, StaleVersion
from app.workflow.compile_stage import WrongState
from app.workflow.gates import GateResult
from app.workflow.harvest_stage import ScopeMissing, canonical_hash
from app.workflow import scope_guard
from app.workflow.plan_stage import MODEL_ARTIFACT, MODEL_FILE, PLAN_ARTIFACT, PLAN_FILE, _validation
from app.workflow.run import ArtifactRef, Run, utc_now
from app.workflow.stale import assert_fresh
from app.workflow.states import RunState

TMDL_ARTIFACT = "TMDLModel"
MODEL_DIR = "model"
GENERATE_STATES = (RunState.PLAN_APPROVED,)
REPAIR_STATES = (RunState.PLAN_APPROVED, RunState.MODEL_FAILED)
VERSION_DIR = re.compile(r"v(\d+)")


class ModelSpecInvalid(Exception):
    """The ModelSpec fails plan validation; nothing was written."""

    def __init__(self, result: GateResult):
        self.result = result
        self.failed = [c for c in result.checks if c.status == "fail"]
        details = "; ".join(f"{c.id}: {p}" for c in self.failed for p in c.evidence.get("paths", [c.description]))
        super().__init__(f"The ModelSpec is not valid, so no model was generated: {details}")


def _next_version(model_root: Path) -> int:
    found = [int(m.group(1)) for p in model_root.glob("v*") if p.is_dir() and (m := VERSION_DIR.fullmatch(p.name))]
    return max(found, default=0) + 1


def folder_hash(folder: Path) -> str:
    """One hash over every file's relative path and bytes."""
    digest = hashlib.sha256()
    for path in sorted(p for p in Path(folder).rglob("*") if p.is_file()):
        digest.update(path.relative_to(folder).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return "sha256:" + digest.hexdigest()


def write_model_version(
    store: RunStore, runs_root: Path, run_id: str, expect_only: set[str] | None = None
) -> tuple[int, list[str]]:
    """Generate the next TMDL version from the approved ModelSpec. No state check, no state change.

    `expect_only` (T-701, targeted regeneration) is the set of files (or folders, ending in `/`) that
    may differ from the TMDL version in place; any other difference raises ScopeViolation before the
    version is kept."""
    run = store.get(run_id)
    model_ref = run.artifacts.get(MODEL_ARTIFACT)
    if model_ref is None or model_ref.status != "approved":
        raise NotFound("The run has no approved ModelSpec yet: approve the plan first")
    scope = run.scope
    if scope is None:
        raise ScopeMissing()
    if scope.sql_endpoint is None:
        raise ScopeMissing("The run's Lakehouse has no SQL endpoint, so the model has nothing to read from.")

    run_dir = Path(runs_root) / run_id
    if not (run_dir / MODEL_FILE).is_file() or not (run_dir / PLAN_FILE).is_file():
        raise NotFound("No plan has been generated for this run yet")
    spec = load_yaml(ModelSpec, run_dir / MODEL_FILE)
    plan = load_yaml(ExecutionPlan, run_dir / PLAN_FILE)
    spec_hash = canonical_hash(to_data(spec))
    if spec_hash != model_ref.hash:
        raise StaleVersion(model_ref.hash, spec_hash, "model spec")
    result = _validation(runs_root, run_id, spec, plan)
    if not result.passed:
        raise ModelSpecInvalid(result)

    model_root = run_dir / MODEL_DIR
    version = _next_version(model_root)
    final = model_root / f"v{version}"
    staging = model_root / f".v{version}.tmp"
    if staging.exists():
        shutil.rmtree(staging)
    try:
        files = generate_tmdl(spec, staging, scope.sql_endpoint)  # raises before anything is kept
        previous = run_dir / run.artifacts[TMDL_ARTIFACT].path if TMDL_ARTIFACT in run.artifacts else None
        scope_guard.check("model", previous, staging, expect_only)
        digest = folder_hash(staging)
        replace_file(staging, final)
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    run = store.get(run_id)
    run.artifacts[TMDL_ARTIFACT] = ArtifactRef(
        path=f"{MODEL_DIR}/v{version}",
        version=version,
        hash=digest,
        status="draft",
        based_on={MODEL_ARTIFACT: spec_hash},
        updated_at=utc_now(),
    )
    store.save(run)
    return version, [p.relative_to(staging).as_posix() for p in files]


def generate_model(
    store: RunStore,
    runs_root: Path,
    run_id: str,
    *,
    from_repair: bool = False,
    actor: str = "system",
    expect_only: set[str] | None = None,
) -> tuple[int, list[str], Run]:
    """`POST /runs/{id}/model/generate`: PLAN_APPROVED (or MODEL_FAILED, for repair) to MODEL_GENERATING."""
    run = store.get(run_id)
    allowed = REPAIR_STATES if from_repair else GENERATE_STATES
    if run.state not in allowed:
        raise WrongState("Generating the model", run.state, allowed)
    assert_fresh(run.artifacts, (MODEL_ARTIFACT, PLAN_ARTIFACT), "Generating the model")
    version, files = write_model_version(store, runs_root, run_id, expect_only)
    run = store.transition(run_id, RunState.MODEL_GENERATING, f"Model v{version} generated", actor)
    return version, files, run
