"""Review and approval of the semantic model (T-507, §12 G4 human decision, FR-018, rule 2).

The review combines what the model contains (tables, relationships, each measure's DAX next to
its approved KPI definition and the reconciliation values), the RLS roles and the latest
ValidationReport. People approve within their own scopes, each as a separate action with the role
they act as: `bi_engineer` and `business_owner` always, `security_reviewer` when the model has RLS
roles. An approval is bound to the model's hash (a hash of the deployed TMDL files), so a repair, a
bridge or any other change invalidates it and it must be given again. When every required role has
approved the current hash and Gate G4 passes, the run moves MODEL_GENERATING -> MODEL_APPROVED. Only
the store changes run state. A business owner can't redefine a KPI here: a Reject that says the
definition itself is wrong sends the run to NEEDS_INPUT for requirement review (T-701 completes
that loop), and nothing in the contract changes.
"""

import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.contracts.model_plan import ModelSpec, load_yaml
from app.contracts.validation_report import ValidationRun, load_report
from app.model.tmdl_reader import read_model
from app.store.base import RunStore
from app.workflow.approval import (
    Action,
    AlreadyApproved,
    ApprovalRecord,
    Forbidden,
    GateFailed,
    Outcome,
    UnsupportedAction,
    _append,
    list_approvals,
)
from app.workflow.clarification import NotFound, StaleVersion
from app.workflow.compile_stage import WrongState, load_current_contract
from app.workflow.gates import GateCheck, GateResult, save_gate_result
from app.workflow.model_stage import TMDL_ARTIFACT, folder_hash
from app.workflow.plan_stage import MODEL_ARTIFACT, MODEL_FILE
from app.workflow.roles import load_roles
from app.workflow.returns import reopened_after
from app.workflow.stale import assert_fresh
from app.workflow.run import Run, utc_now
from app.workflow.states import RunState
from app.workflow.validate_stage import REPORT_FILE

ARTIFACT = "SemanticModel"
REVIEW_STATES = (RunState.MODEL_GENERATING,)
ALWAYS = ("bi_engineer", "business_owner")
SECURITY = "security_reviewer"


class InvalidRole(Exception):
    def __init__(self, message: str):
        super().__init__(message)


class RoleStatus(BaseModel):
    role: str
    status: str  # pending | approved | rejected | invalidated
    approval: ApprovalRecord | None = None
    note: str | None = None


class ApprovalItem(ApprovalRecord):
    current: bool  # recorded on the model hash being reviewed
    note: str | None = None


class Reconciliation(BaseModel):
    dax_value: float | None = None
    sql_value: float | None = None
    expected: float | None = None
    status: str = "not_validated"  # pass | fail | warning | not_validated
    message: str | None = None


class MeasureReview(BaseModel):
    name: str
    table: str
    dax: str | None
    description: str | None = None
    kpi_definition: dict[str, Any] | None
    reconciliation: Reconciliation


class ModelReview(BaseModel):
    model_hash: str
    version: int
    state: RunState
    tables: list[dict[str, Any]]
    relationships: list[dict[str, Any]]
    measures: list[MeasureReview]
    roles: list[dict[str, Any]]
    validation_report: ValidationRun | None
    validation_current: bool  # the latest validation is of the model being reviewed
    gate: GateResult
    approvals_required: list[str]
    approvals_status: list[RoleStatus]
    approvals_done: list[ApprovalItem] = Field(default_factory=list)


def required_roles(spec: ModelSpec) -> list[str]:
    return [*ALWAYS, *([SECURITY] if spec.roles else [])]


def _current(store: RunStore, runs_root: Path, run_id: str):
    run = store.get(run_id)
    ref = run.artifacts.get(TMDL_ARTIFACT)
    run_dir = Path(runs_root) / run_id
    if ref is None or not (run_dir / ref.path).is_dir() or not (run_dir / MODEL_FILE).is_file():
        raise NotFound("The model hasn't been generated yet: POST /runs/{id}/model/generate first")
    folder = run_dir / ref.path
    return run, ref, folder, folder_hash(folder), load_yaml(ModelSpec, run_dir / MODEL_FILE)


def g4_gate(report_run: ValidationRun | None, live_hash: str) -> GateResult:
    """Gate G4 as the latest validation of the model being reviewed."""
    if report_run is None:
        checks = [GateCheck(id="G4.VALIDATED", description="The model has been validated: POST /runs/{id}/model/validate", status="fail")]
        return GateResult(gate="G4", artifact=ARTIFACT, artifact_hash=live_hash, passed=False, checked_at=utc_now(), checks=checks)
    checks = [
        GateCheck(
            id=c.id,
            description=f"{c.object}: {c.message}",
            status="warning" if c.status == "skipped" else c.status,
            evidence={"code": c.code, **c.evidence},
        )
        for c in report_run.checks
    ]
    current = report_run.artifact_hash == live_hash
    if not current:
        checks.append(GateCheck(id="G4.CURRENT", description="The latest validation is of another version of the model: validate it again", status="fail"))
    return GateResult(
        gate="G4", artifact=ARTIFACT, artifact_hash=live_hash,
        passed=report_run.passed and current, checked_at=report_run.started_at, checks=checks,
    )


def role_statuses(
    records: list[ApprovalRecord], roles: list[str], live_hash: str, after: int | None = None
) -> list[RoleStatus]:
    """`after` (T-701): the approval records before this position (the run was returned then) don't count."""
    out = []
    for role in roles:
        mine = [
            r for i, r in enumerate(records)
            if r.artifact == ARTIFACT and r.role == role and r.action in (Action.ACCEPT, Action.REJECT)
            and (after is None or i >= after)
        ]
        if not mine:
            out.append(RoleStatus(role=role, status="pending"))
            continue
        last = mine[-1]
        if last.artifact_hash == live_hash:
            out.append(RoleStatus(role=role, status="approved" if last.action is Action.ACCEPT else "rejected", approval=last))
        elif last.action is Action.ACCEPT:
            out.append(RoleStatus(role=role, status="invalidated", approval=last, note="invalidated: model changed"))
        else:
            out.append(RoleStatus(role=role, status="pending", note="the earlier rejection was of another version"))
    return out


def _latest_validation(runs_root: Path, run_id: str) -> ValidationRun | None:
    report = load_report(Path(runs_root) / run_id / REPORT_FILE, run_id)
    return report.latest("SemanticModel")


def _reconciliation(latest: ValidationRun | None, name: str) -> Reconciliation:
    check = next((c for c in (latest.checks if latest else []) if c.id == "G4.RECON" and c.object == f"measure:{name}"), None)
    if check is None:
        return Reconciliation()
    ev = check.evidence
    example = ev.get("expected_example") or {}
    return Reconciliation(
        dax_value=ev.get("dax_value"), sql_value=ev.get("sql_value"), expected=example.get("value"),
        status=check.status, message=check.message,
    )


def review(store: RunStore, runs_root: Path, run_id: str) -> ModelReview:
    """`GET /runs/{id}/model/review`."""
    run, ref, folder, live_hash, spec = _current(store, runs_root, run_id)
    tmdl = read_model(folder)
    contract = load_current_contract(runs_root, run_id)
    kpis = {k.name.casefold(): k for k in (contract.kpis if contract else [])}
    latest = _latest_validation(runs_root, run_id)
    roles = required_roles(spec)
    records = list_approvals(store, runs_root, run_id)
    status = role_statuses(records, roles, live_hash, reopened_after(runs_root, run_id))

    measures = []
    for m in sorted(spec.measures, key=lambda m: m.name):
        table = tmdl.tables.get(m.table)
        kpi = kpis.get(m.kpi.casefold())
        measures.append(
            MeasureReview(
                name=m.name, table=m.table, dax=(table.measures.get(m.name) if table else None) or m.dax,
                description=m.description,
                kpi_definition=(
                    {
                        "name": kpi.name, "formula_intent": kpi.formula_intent, "grain": kpi.grain, "filters": kpi.filters,
                        "time_logic": kpi.time_logic, "exclusions": kpi.exclusions, "currency": kpi.currency,
                    }
                    if kpi else None
                ),
                reconciliation=_reconciliation(latest, m.name),
            )
        )
    done = [
        ApprovalItem(
            **r.model_dump(),
            current=r.artifact_hash == live_hash,
            note=None if r.artifact_hash == live_hash else "invalidated: model changed",
        )
        for r in records
        if r.artifact == ARTIFACT and r.role in roles
    ]
    return ModelReview(
        model_hash=live_hash,
        version=ref.version,
        state=run.state,
        tables=[
            {
                "name": t.name, "role": t.role, "grain": t.grain,
                "source": f"{t.source.schema_name}.{t.source.table}" if t.source else None,
                "columns": [{"name": c.name, "data_type": c.data_type, "is_hidden": c.is_hidden} for c in t.columns],
                "hierarchies": [h.name for h in t.hierarchies],
            }
            for t in spec.tables
        ],
        relationships=[
            {
                "name": r.name, "from": {"table": r.from_.table, "column": r.from_.column}, "to": {"table": r.to.table, "column": r.to.column},
                "cardinality": r.cardinality, "cross_filter": r.cross_filter, "evidence_ref": r.evidence_ref,
                "override_rationale": r.override_rationale,
            }
            for r in spec.relationships
        ],
        measures=measures,
        roles=[r.model_dump(mode="json") for r in spec.roles],
        validation_report=latest,
        validation_current=latest is not None and latest.artifact_hash == live_hash,
        gate=g4_gate(latest, live_hash),
        approvals_required=roles,
        approvals_status=status,
        approvals_done=done,
    )


def decide_model(
    store: RunStore,
    runs_root: Path,
    run_id: str,
    action: Action,
    user: dict[str, str],
    expected_hash: str,
    comment: str | None,
    role: str | None,
    definition_wrong: bool = False,
) -> Outcome:
    """Accept or reject the SemanticModel as one role. Raises UnsupportedAction, InvalidRole,
    Forbidden, WrongState, AlreadyApproved, StaleVersion or GateFailed; then nothing changes."""
    if action not in (Action.ACCEPT, Action.REJECT):
        raise UnsupportedAction(ARTIFACT, action)
    run, ref, folder, live_hash, spec = _current(store, runs_root, run_id)
    required = required_roles(spec)
    if not role or role not in required:
        raise InvalidRole(f"role must be one of {', '.join(required)} for this model" + (f", not {role!r}" if role else ""))
    if not load_roles().has(user["upn"], role):
        raise Forbidden(role, action, f"You don't hold the {role} role, so you can't {action.value.lower()} the model as {role}")
    if definition_wrong and not (role == "business_owner" and action is Action.REJECT):
        raise InvalidRole("definition_wrong is only for a business_owner's Reject")
    if run.state is RunState.MODEL_APPROVED and action is Action.ACCEPT:
        raise AlreadyApproved("The semantic model is already approved")
    if run.state not in REVIEW_STATES:
        raise WrongState(f"{action.value.title()} of the model", run.state, REVIEW_STATES)
    if live_hash != expected_hash:
        raise StaleVersion(expected_hash, live_hash, "model")

    records = list_approvals(store, runs_root, run_id)
    gate = gate_ref = None
    actor = user["upn"]
    if action is Action.ACCEPT:
        assert_fresh(run.artifacts, (TMDL_ARTIFACT, MODEL_ARTIFACT), "Approving the model")
        if next((s for s in role_statuses(records, [role], live_hash, reopened_after(runs_root, run_id)) if s.status == "approved"), None):
            raise AlreadyApproved(f"{role} has already approved this version of the model")
        gate = g4_gate(_latest_validation(runs_root, run_id), live_hash)
        gate_ref = save_gate_result(runs_root, run_id, gate)
        if not gate.passed:
            raise GateFailed(gate)

    record = ApprovalRecord(
        approval_id=str(uuid.uuid4()), run_id=run_id, artifact=ARTIFACT, artifact_version=ref.version, artifact_hash=live_hash,
        action=action, role=role, user={"oid": user["oid"], "upn": actor}, at=utc_now(), comment=comment, gate_result_ref=gate_ref,
    )
    _append(runs_root, record)

    if action is Action.ACCEPT:
        statuses = role_statuses([*records, record], required, live_hash, reopened_after(runs_root, run_id))
        if all(s.status == "approved" for s in statuses):
            run = store.get(run_id)
            for name in (TMDL_ARTIFACT, MODEL_ARTIFACT):
                if name in run.artifacts:
                    run.artifacts[name].status = "approved"
                    run.artifacts[name].updated_at = utc_now()
            store.save(run)
            store.transition(run_id, RunState.MODEL_APPROVED, "Semantic model approved", actor)
    elif definition_wrong:
        store.transition(
            run_id, RunState.NEEDS_INPUT,
            f"The business owner says a KPI definition is wrong: requirement review needed. {(comment or '')[:200]}", actor,
        )
    return Outcome(approval=record, run=store.get(run_id), gate_result=gate)
