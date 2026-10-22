"""The plan stage service (T-404): check the run, build ModelSpec.yaml and ExecutionPlan.yaml from
the approved inputs, validate them and register both artifacts.

A plain function, not a graph: generation is deterministic and has no LLM. The run state never
changes (T-405's Gate G3 moves it to PLAN_APPROVED).
"""

from collections.abc import Callable
from pathlib import Path

import yaml
from pydantic import ValidationError

from app.config import get_optional_setting
from app.contracts.feasibility_report import FeasibilityReport
from app.contracts.model_plan import ExecutionPlan, ModelSpec, load_yaml, save_yaml, to_data
from app.discovery.model_planner import PlanInputs, build_plan
from app.discovery.plan_validation import validate_plan
from app.store.base import RunStore
from app.workflow.bridge_stage import load_proposals
from app.workflow.clarification import NotFound
from app.workflow.compile_stage import ARTIFACT as CONTRACT_ARTIFACT
from app.workflow.compile_stage import WrongState, load_current_contract
from app.workflow.conformed import load_conformed
from app.workflow.feasibility_stage import ARTIFACT as FEASIBILITY_ARTIFACT
from app.workflow.feasibility_stage import CURRENT as FEASIBILITY_FILE
from app.workflow.feasibility_stage import load_resolution
from app.workflow.gates import GateResult
from app.workflow.harvest_stage import ARTIFACT as INVENTORY_ARTIFACT
from app.workflow.harvest_stage import canonical_hash, load_inventory
from app.workflow.inventory_review import approved_exception_ids
from app.workflow.run import ArtifactRef, Run, utc_now
from app.workflow.source_stage import ARTIFACT as RESOLUTION_ARTIFACT
from app.workflow.stale import assert_fresh
from app.workflow.states import RunState

PlanMerge = Callable[[ModelSpec, ExecutionPlan, ModelSpec, ExecutionPlan], tuple[ModelSpec, ExecutionPlan]]
MODEL_ARTIFACT = "ModelSpec"
PLAN_ARTIFACT = "ExecutionPlan"
MODEL_FILE = "ModelSpec.yaml"
PLAN_FILE = "ExecutionPlan.yaml"
PLAN_STATES = (RunState.DATA_PROFILED,)


def _load_feasibility(runs_root: Path, run_id: str) -> FeasibilityReport:
    path = Path(runs_root) / run_id / FEASIBILITY_FILE
    if not path.is_file():
        raise NotFound("Feasibility hasn't been checked for this run yet: POST /runs/{id}/plan/feasibility first")
    return FeasibilityReport.model_validate_json(path.read_text(encoding="utf-8"))


def _validation(runs_root: Path, run_id: str, model: ModelSpec, plan: ExecutionPlan) -> GateResult:
    run_dir = Path(runs_root) / run_id
    feasibility = run_dir / FEASIBILITY_FILE
    return validate_plan(
        model,
        plan,
        inventory=load_inventory(runs_root, run_id),
        feasibility=FeasibilityReport.model_validate_json(feasibility.read_text(encoding="utf-8")) if feasibility.is_file() else None,
        contract=load_current_contract(runs_root, run_id),
        artifact_hash=canonical_hash(to_data(model)),
    )


def _load_old(cls, path: Path):
    """The saved file, or None if it is missing or can't be read (it is then regenerated)."""
    if not path.is_file():
        return None
    try:
        return load_yaml(cls, path)
    except (ValidationError, ValueError, yaml.YAMLError):
        return None


def _register(store: RunStore, run: Run, name: str, filename: str, model, based_on: dict[str, str]) -> None:
    run.artifacts[name] = ArtifactRef(
        path=filename,
        version=model.version,
        hash=canonical_hash(to_data(model)),
        status="draft",
        based_on=based_on,
        updated_at=utc_now(),
    )


def generate_plan(
    store: RunStore, runs_root: Path, run_id: str, merge: PlanMerge | None = None
) -> tuple[ExecutionPlan, ModelSpec, GateResult]:
    """Generate the ModelSpec and ExecutionPlan of a run whose KPIs have been checked.

    `merge(old_model, old_plan, new_model, new_plan)` (T-701, targeted regeneration) may replace the
    generated pair, for example by keeping every object of the saved files except the changed ones. It
    is called only when saved files exist and can be read.
    """
    run = store.get(run_id)
    if run.state not in PLAN_STATES:
        raise WrongState("Planning the model", run.state, PLAN_STATES)
    assert_fresh(run.artifacts, (RESOLUTION_ARTIFACT, FEASIBILITY_ARTIFACT), "Planning the model")
    inventory = load_inventory(runs_root, run_id)  # 404 before a harvest
    contract = load_current_contract(runs_root, run_id)
    if contract is None:
        raise NotFound("The run has no business contract yet")
    resolution = load_resolution(runs_root, run_id)
    feasibility = _load_feasibility(runs_root, run_id)
    try:
        bridges = load_proposals(runs_root, run_id)
    except NotFound:
        bridges = None
    conformed = load_conformed()  # CONFIG_ERROR before anything is written
    exceptions = approved_exception_ids(store, runs_root, run_id)

    based_on = {RESOLUTION_ARTIFACT: canonical_hash(resolution.to_dict())}
    for name in (CONTRACT_ARTIFACT, INVENTORY_ARTIFACT):
        if name in run.artifacts:
            based_on[name] = run.artifacts[name].hash
    workspace = get_optional_setting("FABRIC_SANDBOX_WORKSPACE_ID") or (run.scope.workspace_id if run.scope else None)

    model, plan = build_plan(
        PlanInputs(run_id, contract, inventory, resolution, feasibility, bridges, conformed, exceptions, based_on, workspace)
    )

    run_dir = Path(runs_root) / run_id
    if merge is not None:
        old_model = _load_old(ModelSpec, run_dir / MODEL_FILE)
        old_plan = _load_old(ExecutionPlan, run_dir / PLAN_FILE)
        if old_model is not None and old_plan is not None:
            model, plan = merge(old_model, old_plan, model, plan)
    changed = False
    for filename, new, cls in ((MODEL_FILE, model, ModelSpec), (PLAN_FILE, plan, ExecutionPlan)):
        if new is plan:
            plan.based_on = {MODEL_ARTIFACT: canonical_hash(to_data(model))}
        path = run_dir / filename
        old = _load_old(cls, path)
        artifact = run.artifacts.get(MODEL_ARTIFACT if new is model else PLAN_ARTIFACT)
        if old is not None:
            new.version = old.version
            if to_data(new) == to_data(old):
                continue
            new.version = old.version + 1
        elif artifact is not None:  # the file is missing or was hand-edited into something unreadable
            new.version = artifact.version + 1
        changed = True
        save_yaml(new, path)
        save_yaml(new, run_dir / "plans" / filename.replace(".yaml", f".v{new.version}.yaml"))
    if changed:
        _register(store, run, MODEL_ARTIFACT, MODEL_FILE, model, based_on)
        _register(store, run, PLAN_ARTIFACT, PLAN_FILE, plan, plan.based_on)
        store.save(run)
    return plan, model, _validation(runs_root, run_id, model, plan)


def write_new_versions(store: RunStore, runs_root: Path, run: Run, model: ModelSpec, plan: ExecutionPlan) -> None:
    """Save `model` and `plan` as the next versions of both (after an edit): the plan is rebased
    on the new model's hash, both files and their version copies are written, and both artifacts
    go back to `draft`. Earlier versions are kept in `plans/`."""
    model.version += 1
    plan.version += 1
    plan.based_on = {MODEL_ARTIFACT: canonical_hash(to_data(model))}
    run_dir = Path(runs_root) / run.run_id
    for filename, new in ((MODEL_FILE, model), (PLAN_FILE, plan)):
        save_yaml(new, run_dir / filename)
        save_yaml(new, run_dir / "plans" / filename.replace(".yaml", f".v{new.version}.yaml"))
    _register(store, run, MODEL_ARTIFACT, MODEL_FILE, model, model.based_on)
    _register(store, run, PLAN_ARTIFACT, PLAN_FILE, plan, plan.based_on)
    store.save(run)


def read_plan(store: RunStore, runs_root: Path, run_id: str) -> tuple[ExecutionPlan, ModelSpec, GateResult]:
    """The saved files, validated again (so a hand edit shows up)."""
    store.get(run_id)
    run_dir = Path(runs_root) / run_id
    if not (run_dir / MODEL_FILE).is_file() or not (run_dir / PLAN_FILE).is_file():
        raise NotFound("No plan has been generated for this run yet: POST /runs/{id}/plan first")
    model = load_yaml(ModelSpec, run_dir / MODEL_FILE)
    plan = load_yaml(ExecutionPlan, run_dir / PLAN_FILE)
    return plan, model, _validation(runs_root, run_id, model, plan)
