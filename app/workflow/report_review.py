"""Review and approval of the report (T-604, §12 G5 human decision, FR-018).

The review shows, for each page, the rendered image and, for each visual, the questions it answers
and its binding and query status from the latest Gate G5 validation. The project reviewer and the
business owner each approve as a separate action with the role they act as; an approval is bound to
the report's hash (every file of the PBIR folder except the renders), so any change to the report
invalidates it. When both have approved the current hash and G5 passes, the run moves
REPORT_GENERATING -> REPORT_APPROVED. A reviewer may also regenerate one visual or one page with a
guidance comment: the model proposes a replacement that the plan checks accept, only that part of the
report is rewritten (the scope guard of the repair service), and Gate G5 runs again. Only the store
changes run state.
"""

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from app.contracts.report_plan import ReportPlan
from app.contracts.validation_report import ValidationRun, load_report
from app.report.pbir_generator import PbirGenerationError, folder_digest
from app.report.pbir_reader import read_report
from app.report.regenerate import find_target, regenerate_page, regenerate_visual
from app.repair.models import ClassifiedError
from app.repair.report_adapter import ReportRepairAdapter
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
from app.workflow.harvest_stage import canonical_hash, write_json
from app.workflow.model_review import ApprovalItem, InvalidRole, RoleStatus
from app.workflow.plan_stage import MODEL_ARTIFACT, MODEL_FILE
from app.workflow.report_plan_stage import ARTIFACT as PLAN_ARTIFACT
from app.workflow.report_plan_stage import CURRENT as PLAN_FILE
from app.workflow.report_stage import ARTIFACT as PBIR_ARTIFACT
from app.workflow.report_stage import PUBLISH_FILE, REPORT_DIR
from app.workflow.report_validate_stage import ReportEnvironment, settle
from app.workflow.roles import load_roles
from app.workflow.returns import reopened_after
from app.workflow.stale import assert_fresh
from app.workflow.run import Run, utc_now
from app.workflow.states import RunState
from app.workflow.validate_stage import REPORT_FILE

ARTIFACT = "Report"  # the artifact of approval records
ROLES = ("project_reviewer", "business_owner")
REVIEW_STATES = (RunState.REPORT_GENERATING,)


@dataclass
class ReportServices:
    """What a regeneration needs beyond the store: the model, and the sandbox and Power BI clients
    (built when asked for, so an approval that doesn't regenerate needs neither)."""

    llm: Callable[[str, str, dict], str]
    items: Callable[[], Any]
    pbi: Callable[[], Any]


class RegenerationOutOfScope(Exception):
    """The regeneration would change files outside the visual or page it targets; nothing was kept."""


class QuestionRef(BaseModel):
    id: str
    text: str


class VisualReview(BaseModel):
    id: str
    title: str
    type: str | None = None
    questions: list[QuestionRef] = Field(default_factory=list)
    bind_status: str = "not_validated"  # pass | fail | warning | skipped | not_validated
    query_status: str = "not_validated"
    messages: list[str] = Field(default_factory=list)  # the failing or warning checks, for the screen


class PageReview(BaseModel):
    name: str  # the page's name in the report (the folder name)
    display_name: str | None = None
    image_url: str | None = None  # /runs/{id}/files/…, None when the page wasn't rendered
    render_status: str = "not_validated"  # pass | warning | skipped | not_validated
    render_message: str | None = None
    open_url: str | None = None  # the published report in Power BI
    visuals: list[VisualReview] = Field(default_factory=list)


class ReportReview(BaseModel):
    report_hash: str
    version: int
    state: RunState
    pages: list[PageReview]
    unanswered_questions: list[dict[str, str]] = Field(default_factory=list)
    validation_report: ValidationRun | None
    validation_current: bool  # the latest validation is of the report being reviewed
    gate: GateResult
    approvals_required: list[str]
    approvals_status: list[RoleStatus]
    approvals_done: list[ApprovalItem] = Field(default_factory=list)


def _current(store: RunStore, runs_root: Path, run_id: str):
    run = store.get(run_id)
    ref = run.artifacts.get(PBIR_ARTIFACT)
    run_dir = Path(runs_root) / run_id
    if ref is None or not (run_dir / ref.path).is_dir():
        raise NotFound("The report hasn't been generated yet: POST /runs/{id}/report/generate first")
    folder = run_dir / ref.path
    return run, ref, folder, folder_digest(folder)


def g5_gate(entry: ValidationRun | None, live_hash: str) -> GateResult:
    """Gate G5 as the latest validation of the report being reviewed."""
    if entry is None:
        checks = [GateCheck(id="G5.VALIDATED", description="The report has been validated: POST /runs/{id}/report/validate", status="fail")]
        return GateResult(gate="G5", artifact=ARTIFACT, artifact_hash=live_hash, passed=False, checked_at=utc_now(), checks=checks)
    checks = [
        GateCheck(id=c.id, description=f"{c.object}: {c.message}", status="warning" if c.status == "skipped" else c.status, evidence={"code": c.code, **c.evidence})
        for c in entry.checks
    ]
    current = entry.artifact_hash == live_hash
    if not current:
        checks.append(GateCheck(id="G5.CURRENT", description="The latest validation is of another version of the report: validate it again", status="fail"))
    return GateResult(gate="G5", artifact=ARTIFACT, artifact_hash=live_hash, passed=entry.passed and current, checked_at=entry.started_at, checks=checks)


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
            out.append(RoleStatus(role=role, status="invalidated", approval=last, note="invalidated: report changed"))
        else:
            out.append(RoleStatus(role=role, status="pending", note="the earlier rejection was of another version"))
    return out


_ORDER = {"fail": 0, "warning": 1, "skipped": 2, "pass": 3}


def _worst(checks: list) -> str:
    return min((c.status for c in checks), key=lambda s: _ORDER.get(s, 9)) if checks else "not_validated"


def _open_url(run_dir: Path) -> str | None:
    import json

    try:
        data = json.loads((run_dir / PUBLISH_FILE).read_text(encoding="utf-8"))
        return f"https://app.powerbi.com/groups/{data['workspace_id']}/reports/{data['report_item_id']}"
    except (OSError, ValueError, KeyError):
        return None


def review(store: RunStore, runs_root: Path, run_id: str) -> ReportReview:
    """`GET /runs/{id}/report/review`."""
    run, ref, folder, live_hash = _current(store, runs_root, run_id)
    run_dir = Path(runs_root) / run_id
    latest = load_report(run_dir / REPORT_FILE, run_id).latest("Report")
    checks = latest.checks if latest else []
    plan = None
    if (run_dir / PLAN_FILE).is_file():
        try:
            plan = ReportPlan.model_validate_json((run_dir / PLAN_FILE).read_text(encoding="utf-8"))
        except ValueError:
            plan = None
    plan_visuals = {v.id: v for p in (plan.pages if plan else []) for v in p.visuals}
    questions = {q.id: q for q in (plan.questions if plan else [])}
    open_url = _open_url(run_dir)

    pages = []
    for page in read_report(folder):
        render = [c for c in checks if c.id == "G5.RENDER" and c.object == f"page:{page.name}"]
        image = folder / "renders" / f"{page.name}.png"
        visuals = []
        for v in page.visuals:
            obj = f"visual:{v.id}"
            bind = [c for c in checks if c.id == "G5.BIND" and c.object == obj]
            query = [c for c in checks if c.id == "G5.QUERY" and c.object == obj]
            planned = plan_visuals.get(v.id)
            visuals.append(
                VisualReview(
                    id=v.id,
                    title=planned.title if planned else v.id,
                    type=v.visual_type,
                    questions=[QuestionRef(id=q, text=questions[q].text) for q in (planned.question_ids if planned else []) if q in questions],
                    bind_status=_worst(bind),
                    query_status=_worst(query),
                    messages=[c.message for c in [*bind, *query] if c.status in ("fail", "warning")],
                )
            )
        pages.append(
            PageReview(
                name=page.name,
                display_name=page.display_name,
                image_url=f"/runs/{run_id}/files/{ref.path}/renders/{page.name}.png" if image.is_file() else None,
                render_status=_worst(render),
                render_message=next((c.message for c in render if c.status != "pass"), None),
                open_url=open_url,
                visuals=visuals,
            )
        )
    records = list_approvals(store, runs_root, run_id)
    done = [
        ApprovalItem(**r.model_dump(), current=r.artifact_hash == live_hash, note=None if r.artifact_hash == live_hash else "invalidated: report changed")
        for r in records
        if r.artifact == ARTIFACT and r.action in (Action.ACCEPT, Action.REJECT, Action.REGENERATE)
    ]
    return ReportReview(
        report_hash=live_hash,
        version=ref.version,
        state=run.state,
        pages=pages,
        unanswered_questions=[u.model_dump() for u in (plan.unanswered_questions if plan else [])],
        validation_report=latest,
        validation_current=latest is not None and latest.artifact_hash == live_hash,
        gate=g5_gate(latest, live_hash),
        approvals_required=list(ROLES),
        approvals_status=role_statuses(records, list(ROLES), live_hash, reopened_after(runs_root, run_id)),
        approvals_done=done,
    )


# --- regenerating one visual or page -------------------------------------------------------------


def _save_plan(store: RunStore, runs_root: Path, run_id: str, plan: ReportPlan) -> None:
    run_dir = Path(runs_root) / run_id
    plan.version += 1
    data = plan.to_dict()
    write_json(run_dir / "reports" / f"ReportPlan.v{plan.version}.json", data)
    write_json(run_dir / PLAN_FILE, data)
    run = store.get(run_id)
    previous = run.artifacts[PLAN_ARTIFACT]
    run.artifacts[PLAN_ARTIFACT] = previous.model_copy(
        update={"version": plan.version, "hash": canonical_hash(data), "status": "draft", "updated_at": utc_now()}
    )
    store.save(run)


def _regenerate(store: RunStore, runs_root: Path, run_id: str, target: dict[str, str], comment: str, services: ReportServices, actor: str) -> Run:
    from app.contracts.model_plan import ModelSpec, load_yaml

    run, ref, folder, _ = _current(store, runs_root, run_id)
    run_dir = Path(runs_root) / run_id
    plan_path = run_dir / PLAN_FILE
    if not plan_path.is_file() or not (run_dir / MODEL_FILE).is_file():
        raise NotFound("The report has no plan: POST /runs/{id}/report/plan first")
    plan = ReportPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    spec = load_yaml(ModelSpec, run_dir / MODEL_FILE)
    contract = load_current_contract(runs_root, run_id)
    if contract is None:
        raise NotFound("The run has no business contract yet")
    found = find_target(plan, target.get("page"), target.get("visual"))
    if found is None:
        raise NotFound(f"The report has no {'visual ' + target['visual'] if target.get('visual') else 'page ' + str(target.get('page'))} to regenerate")
    page_i, visual_j = found
    old_ids = [v.id for v in plan.pages[page_i].visuals]
    if visual_j is not None:
        new_plan = regenerate_visual(plan, spec, contract, services.llm, page_i, visual_j, comment)
        objects = [f"visual:{old_ids[visual_j]}"]
        prune: list[str] = []
    else:
        new_plan = regenerate_page(plan, spec, contract, services.llm, page_i, comment)
        new_ids = [v.id for v in new_plan.pages[page_i].visuals]
        objects = [f"page:p{page_i + 1}", *[f"visual:{i}" for i in dict.fromkeys([*old_ids, *new_ids])]]
        prune = [f"p{page_i + 1}"]

    _save_plan(store, runs_root, run_id, new_plan)
    env = ReportEnvironment(store, runs_root, run_id)
    adapter = ReportRepairAdapter(env, ref.version, folder, items_client=services.items(), pbi=services.pbi())
    adapter.begin_attempt()
    try:
        adapter.regenerate(objects, prune)
    except PbirGenerationError as exc:
        adapter.discard()
        raise NotFound(f"The regenerated report part can't be generated: {exc}") from None
    guard = [ClassifiedError(f"regenerate:{o}", o, "REGENERATE", "REGENERATE", "deterministic", "regenerated by a reviewer") for o in objects]
    if not adapter.has_changes():
        adapter.discard()
        return store.get(run_id)  # the replacement is identical to what the report has
    violations = adapter.scope_guard(guard)
    if violations:
        adapter.discard()
        raise RegenerationOutOfScope(f"The regeneration would change files outside its target, so it was dropped: {', '.join(violations)}")
    entry = adapter.validate(None)
    run = store.get(run_id)
    run.artifacts[PBIR_ARTIFACT] = run.artifacts[PBIR_ARTIFACT].model_copy(
        update={
            "path": f"{REPORT_DIR}/v{adapter.version}", "version": adapter.version, "hash": entry.artifact_hash, "status": "draft",
            "based_on": {**run.artifacts[PBIR_ARTIFACT].based_on, PLAN_ARTIFACT: run.artifacts[PLAN_ARTIFACT].hash}, "updated_at": utc_now(),
        }
    )
    store.save(run)
    return settle(store, runs_root, run_id, entry, actor)


# --- the approval actions ----------------------------------------------------------------------------


def decide_report(
    store: RunStore,
    runs_root: Path,
    run_id: str,
    action: Action,
    user: dict[str, str],
    expected_hash: str,
    comment: str | None,
    role: str | None,
    target: dict[str, str] | None = None,
    services: ReportServices | None = None,
) -> Outcome:
    """Accept, reject or regenerate (part of) the report as one role. Raises UnsupportedAction,
    InvalidRole, Forbidden, WrongState, AlreadyApproved, StaleVersion, GateFailed or NotFound; then
    nothing changes."""
    if action not in (Action.ACCEPT, Action.REJECT, Action.REGENERATE):
        raise UnsupportedAction(ARTIFACT, action)
    run, ref, folder, live_hash = _current(store, runs_root, run_id)
    held = load_roles().roles_of(user["upn"])
    if action is Action.REGENERATE:
        acting = role if role in ROLES else next((r for r in ROLES if r in held), None)
    else:
        if not role or role not in ROLES:
            raise InvalidRole(f"role must be one of {', '.join(ROLES)}" + (f", not {role!r}" if role else ""))
        acting = role
    if acting is None or acting not in held:
        raise Forbidden(acting or ROLES[0], action, f"You don't hold the {acting or ' or '.join(ROLES)} role, so you can't {action.value.lower()} the report")
    if run.state is RunState.REPORT_APPROVED and action is not Action.REGENERATE:
        raise AlreadyApproved("The report is already approved")
    if run.state not in REVIEW_STATES:
        raise WrongState(f"{action.value.title()} of the report", run.state, REVIEW_STATES)
    if live_hash != expected_hash:
        raise StaleVersion(expected_hash, live_hash, "report")

    records = list_approvals(store, runs_root, run_id)
    actor = user["upn"]
    gate = gate_ref = None
    if action is Action.ACCEPT:
        assert_fresh(run.artifacts, (PBIR_ARTIFACT, PLAN_ARTIFACT, "TMDLModel"), "Approving the report")
        if next((s for s in role_statuses(records, [acting], live_hash, reopened_after(runs_root, run_id)) if s.status == "approved"), None):
            raise AlreadyApproved(f"{acting} has already approved this version of the report")
        run_dir = Path(runs_root) / run_id
        gate = g5_gate(load_report(run_dir / REPORT_FILE, run_id).latest("Report"), live_hash)
        gate_ref = save_gate_result(runs_root, run_id, gate)
        if not gate.passed:
            raise GateFailed(gate)
    if action is Action.REGENERATE:
        if services is None:
            raise UnsupportedAction(ARTIFACT, action)
        if not target or not (target.get("page") or target.get("visual")):
            raise InvalidRole("a regenerate needs a target: a page, a visual or both")
        _regenerate(store, runs_root, run_id, target, comment or "", services, actor)

    record = ApprovalRecord(
        approval_id=str(uuid.uuid4()), run_id=run_id, artifact=ARTIFACT,
        artifact_version=store.get(run_id).artifacts[PBIR_ARTIFACT].version if action is Action.REGENERATE else ref.version,
        artifact_hash=store.get(run_id).artifacts[PBIR_ARTIFACT].hash if action is Action.REGENERATE else live_hash,
        target=target if action is Action.REGENERATE else None,
        action=action, role=acting, user={"oid": user["oid"], "upn": actor}, at=utc_now(), comment=comment, gate_result_ref=gate_ref,
    )
    _append(runs_root, record)

    if action is Action.ACCEPT:
        if all(s.status == "approved" for s in role_statuses([*records, record], list(ROLES), live_hash, reopened_after(runs_root, run_id))):
            run = store.get(run_id)
            for name in (PBIR_ARTIFACT, PLAN_ARTIFACT):
                if name in run.artifacts:
                    run.artifacts[name].status = "approved"
                    run.artifacts[name].updated_at = utc_now()
            store.save(run)
            store.transition(run_id, RunState.REPORT_APPROVED, "Report approved", actor)
    return Outcome(approval=record, run=store.get(run_id), gate_result=gate)
