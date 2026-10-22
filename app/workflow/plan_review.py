"""Review of the plan (T-405): Gate G3, edits and the approval of the ExecutionPlan.

Edits are a small set of operations that create new versions of both files. Accept needs a
bi_engineer, a current hash and a passing G3, and moves the run to PLAN_APPROVED. Reject records
the comment and, if the plan was approved, reopens it (back to DATA_PROFILED). Only the store
changes run state.
"""

import uuid
from pathlib import Path
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, StringConstraints

from app.contracts.feasibility_report import FeasibilityReport
from app.contracts.model_plan import ExecutionPlan, ModelSpec, load_yaml, to_data
from app.store.base import RunStore
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
)
from app.workflow.clarification import NotFound
from app.workflow.compile_stage import WrongState, load_current_contract
from app.workflow.conformed import load_conformed
from app.workflow.gates import GateResult, save_gate_result
from app.workflow.harvest_stage import canonical_hash, load_inventory
from app.workflow.inventory_review import approved_exception_ids
from app.workflow.plan_gate import run_g3
from app.workflow.plan_stage import MODEL_ARTIFACT, MODEL_FILE, PLAN_ARTIFACT, PLAN_FILE, write_new_versions
from app.workflow.roles import load_roles
from app.workflow.run import Run, utc_now
from app.workflow.stale import assert_fresh
from app.workflow.states import RunState

PLAN_ROLE = "bi_engineer"
EDIT_STATES = (RunState.DATA_PROFILED,)
REJECT_STATES = (RunState.DATA_PROFILED, RunState.PLAN_APPROVED)
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class RemoveRelationship(BaseModel):
    op: Literal["remove_relationship"]
    name: Text


class SetStorageMode(BaseModel):
    op: Literal["set_storage_mode"]
    value: Literal["DirectLake", "Import"]


class OverrideRelationship(BaseModel):
    op: Literal["override_relationship"]
    name: Text
    rationale: Text


class RemoveMeasure(BaseModel):
    op: Literal["remove_measure"]
    name: Text


PlanOp = Annotated[
    Union[RemoveRelationship, SetStorageMode, OverrideRelationship, RemoveMeasure], Field(discriminator="op")
]
VALID_OPS = ("remove_relationship", "set_storage_mode", "override_relationship", "remove_measure")


class PlanEditInvalid(Exception):
    """An operation names something that isn't in the plan."""


def _artifact(run: Run):
    ref = run.artifacts.get(PLAN_ARTIFACT)
    if ref is None or MODEL_ARTIFACT not in run.artifacts:
        raise NotFound("No plan has been generated for this run yet: POST /runs/{id}/plan first")
    return ref


def _load(runs_root: Path, run_id: str) -> tuple[ModelSpec, ExecutionPlan]:
    run_dir = Path(runs_root) / run_id
    if not (run_dir / MODEL_FILE).is_file() or not (run_dir / PLAN_FILE).is_file():
        raise NotFound("No plan has been generated for this run yet: POST /runs/{id}/plan first")
    return load_yaml(ModelSpec, run_dir / MODEL_FILE), load_yaml(ExecutionPlan, run_dir / PLAN_FILE)


def evaluate_g3(store: RunStore, runs_root: Path, run_id: str) -> GateResult:
    model, plan = _load(runs_root, run_id)
    feasibility = Path(runs_root) / run_id / "Feasibility.json"
    return run_g3(
        model,
        plan,
        load_inventory(runs_root, run_id),
        FeasibilityReport.model_validate_json(feasibility.read_text(encoding="utf-8")) if feasibility.is_file() else None,
        load_current_contract(runs_root, run_id),
        load_conformed(),
        set(approved_exception_ids(store, runs_root, run_id)),
        canonical_hash(to_data(plan)),
    )


def check_g3(store: RunStore, runs_root: Path, run_id: str) -> GateResult:
    """Run Gate G3 on the current plan and save the result."""
    store.get(run_id)
    result = evaluate_g3(store, runs_root, run_id)
    save_gate_result(runs_root, run_id, result)
    return result


def _held(user: dict[str, str]) -> str | None:
    roles = load_roles().roles_of(user["upn"])
    return PLAN_ROLE if PLAN_ROLE in roles else (roles[0] if roles else None)


def _apply(model: ModelSpec, op) -> None:
    if isinstance(op, SetStorageMode):
        model.storage_mode = op.value
    elif isinstance(op, RemoveRelationship):
        kept = [r for r in model.relationships if r.name != op.name]
        if len(kept) == len(model.relationships):
            raise PlanEditInvalid(f"No relationship named {op.name!r} in the model")
        model.relationships = kept
    elif isinstance(op, OverrideRelationship):
        found = next((r for r in model.relationships if r.name == op.name), None)
        if found is None:
            raise PlanEditInvalid(f"No relationship named {op.name!r} in the model")
        found.override_rationale = op.rationale
    elif isinstance(op, RemoveMeasure):
        kept_measures = [m for m in model.measures if m.name != op.name]
        if len(kept_measures) == len(model.measures):
            raise PlanEditInvalid(f"No measure named {op.name!r} in the model")
        model.measures = kept_measures


def _summary(op) -> str:
    if isinstance(op, SetStorageMode):
        return f"set_storage_mode {op.value}"
    if isinstance(op, OverrideRelationship):
        return f"override_relationship {op.name}: {op.rationale}"
    return f"{op.op} {op.name}"


def edit_plan(
    store: RunStore, runs_root: Path, run_id: str, expected_hash: str, ops: list, user: dict[str, str]
) -> tuple[ModelSpec, ExecutionPlan, GateResult]:
    """Apply `ops` in order as one edit: new versions of both files, then Gate G3. Nothing is
    changed if any operation is invalid."""
    run = store.get(run_id)
    if run.state not in EDIT_STATES:
        raise WrongState("Editing the plan", run.state, EDIT_STATES)
    assert_fresh(run.artifacts, (PLAN_ARTIFACT, MODEL_ARTIFACT), "Editing the plan")
    ref = _artifact(run)
    if ref.hash != expected_hash:
        raise StaleVersion(expected_hash, ref.hash, "plan")
    model, plan = _load(runs_root, run_id)
    for op in ops:
        _apply(model, op)
    write_new_versions(store, runs_root, run, model, plan)
    ref = run.artifacts[PLAN_ARTIFACT]
    _append(
        runs_root,
        ApprovalRecord(
            approval_id=str(uuid.uuid4()),
            run_id=run_id,
            artifact=PLAN_ARTIFACT,
            artifact_version=ref.version,
            artifact_hash=ref.hash,
            action=Action.EDIT,
            role=_held(user),
            user={"oid": user["oid"], "upn": user["upn"]},
            at=utc_now(),
            comment="; ".join(_summary(op) for op in ops),
        ),
    )
    return model, plan, check_g3(store, runs_root, run_id)


def decide_plan(
    store: RunStore,
    runs_root: Path,
    run_id: str,
    action: Action,
    user: dict[str, str],
    expected_hash: str,
    comment: str | None = None,
) -> Outcome:
    """Accept or reject the ExecutionPlan. Only a bi_engineer may."""
    if action not in (Action.ACCEPT, Action.REJECT):
        raise UnsupportedAction(PLAN_ARTIFACT, action)
    if PLAN_ROLE not in load_roles().roles_of(user["upn"]):
        raise Forbidden(PLAN_ROLE, action, f"Only a {PLAN_ROLE} can {action.value.lower()} the plan")
    run = store.get(run_id)
    ref = _artifact(run)
    allowed = (RunState.DATA_PROFILED,) if action is Action.ACCEPT else REJECT_STATES
    if run.state is RunState.PLAN_APPROVED and action is Action.ACCEPT:
        raise AlreadyApproved("The plan is already approved")
    if run.state not in allowed:
        raise WrongState(f"{action.value.title()} of the plan", run.state, allowed)
    if ref.hash != expected_hash:
        raise StaleVersion(expected_hash, ref.hash, "plan")

    gate = None
    gate_ref = None
    actor = user["upn"]
    if action is Action.ACCEPT:
        assert_fresh(run.artifacts, (PLAN_ARTIFACT, MODEL_ARTIFACT, "SourceResolution", "Feasibility"), "Approving the plan")
        gate = evaluate_g3(store, runs_root, run_id)
        gate_ref = save_gate_result(runs_root, run_id, gate)
        if not gate.passed:
            raise GateFailed(gate)
        for name in (PLAN_ARTIFACT, MODEL_ARTIFACT):
            run.artifacts[name].status = "approved"
            run.artifacts[name].updated_at = utc_now()
        store.save(run)
        store.transition(run_id, RunState.PLAN_APPROVED, "Execution plan approved", actor)
    elif run.state is RunState.PLAN_APPROVED:  # reject reopens an approved plan
        for name in (PLAN_ARTIFACT, MODEL_ARTIFACT):
            run.artifacts[name].status = "draft"
            run.artifacts[name].updated_at = utc_now()
        store.save(run)
        store.transition(run_id, RunState.DATA_PROFILED, f"Execution plan rejected: {(comment or '')[:200]}", actor)

    record = ApprovalRecord(
        approval_id=str(uuid.uuid4()),
        run_id=run_id,
        artifact=PLAN_ARTIFACT,
        artifact_version=ref.version,
        artifact_hash=ref.hash,
        action=action,
        role=PLAN_ROLE,
        user={"oid": user["oid"], "upn": actor},
        at=utc_now(),
        comment=comment,
        gate_result_ref=gate_ref,
    )
    _append(runs_root, record)
    return Outcome(approval=record, run=store.get(run_id), gate_result=gate)
