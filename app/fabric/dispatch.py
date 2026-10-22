"""The one dispatcher (T-102): maps (method, path pattern) to the existing
stage service, for both `nexora_api` (host `udf`) and `nexora_stage_runner`
(host `job`). UDF and job handlers hold no business logic; they only call
`dispatch()`. See CONTRACTS.md#fabric-operations for the operation table and
fabric-hosting.md for the hosting rule this table encodes.
"""

import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from app.contracts.metadata_inventory import MetadataInventory, RelationshipEvidence
from app.contracts.source_resolution import SourceResolution
from app.discovery.relationship_candidates import Candidate
from app.fabric import deps
from app.fabric.errors import ApiError, map_exception
from app.fabric.schemas import (
    AnalyzeRelationshipsRequest,
    AnalyzeRelationshipsResponse,
    AnswerRequest,
    ApprovalRequest,
    ApprovalResponse,
    ArtifactItem,
    BridgeGenerateResponse,
    CompileResponse,
    CreateRunRequest,
    DataGapRequest,
    DeployRequest,
    DeployResponse,
    DismissRequest,
    ExceptionRequest,
    ExceptionResponse,
    FeasibilityResponse,
    Health,
    HarvestRequest,
    HarvestResponse,
    HarvestSummary,
    KpiEditRequest,
    KpiEditResponse,
    MeasuresResponse,
    MeResponse,
    ModelGenerateResponse,
    ModelValidateResponse,
    PlanEditRequest,
    PlanEditResponse,
    PlanResponse,
    ProfileRequest,
    ProfileResponse,
    QuestionResponse,
    RegenerateStaleResponse,
    ReportGenerateResponse,
    ReportPlanResponse,
    ReportValidateResponse,
    ReturnRequest,
    RunDetail,
    RunList,
    RunSummary,
    ScopeRequest,
)
from app.observability import telemetry
from app.observability.context import bind as bind_context
from app.repair.models import RepairResult
from app.repair.service import repair as repair_model
from app.security.run_access import RunAccessDenied, can_access, cached_decision
from app.workflow import audit as audit_log
from app.workflow.approval import ApprovalRecord, check_g1, list_approvals, perform
from app.workflow.bridge_model_stage import generate_bridge
from app.workflow.bridge_stage import propose_bridges
from app.workflow.clarification import NotFound, answer_question, dismiss_question, edit_kpi
from app.workflow.compile_stage import compile_run, current_contract, open_questions
from app.workflow.feasibility_stage import check_feasibility
from app.workflow.gates import GateResult
from app.workflow.harvest_stage import ARTIFACT as INVENTORY, harvest_run, load_inventory, summary
from app.workflow.impact import ImpactReport, read_impact, return_run
from app.workflow.inventory_review import add_exception, list_data_gaps, raise_data_gap
from app.workflow.measure_stage import add_measures
from app.workflow.model_review import ModelReview, review as review_model
from app.workflow.model_stage import generate_model
from app.workflow.plan_review import check_g3, edit_plan
from app.workflow.plan_stage import generate_plan, read_plan
from app.workflow.profile_stage import profile_run, summarize
from app.workflow.promotion import Services as PromotionServices, resume_rollback, run_deployment, start_deployment
from app.workflow.regenerate_stale import StaleServices, regenerate_stale
from app.workflow.relationship_stage import analyze_run, list_relationships
from app.workflow.relationship_stage import summarize as summarize_relationships
from app.workflow.report_plan_stage import generate_report_plan, read_report_plan
from app.workflow.report_review import ReportReview, review as review_report
from app.workflow.report_stage import generate_report
from app.workflow.report_validate_stage import validate_report
from app.workflow.roles import load_roles
from app.workflow.scope import bind_scope
from app.workflow.source_stage import load_saved_resolution, resolve_sources
from app.workflow.states import RunState
from app.workflow.trace import trace as trace_visual
from app.workflow.validate_stage import validate_model

HEALTH_APP_NAME = "Nexora"


def _wheel_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("nexora")
    except Exception:
        return "0.0.0"


@dataclass(frozen=True)
class Call:
    params: dict[str, str]
    query: dict[str, Any]
    body: dict | None
    principal: dict | None
    as_job: bool


@dataclass(frozen=True)
class Operation:
    # "udf", "job", or a function of the parsed body that returns one of those
    # (POST /runs/{id}/approvals is "job" only when action is REGENERATE).
    host: "str | Callable[[dict | None], str]"
    handler: "Callable[[Call], Any]"
    status: int = 200
    needs_run: bool = True

    def host_for(self, body: dict | None) -> str:
        return self.host(body) if callable(self.host) else self.host


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _parse_body(model: type[BaseModel], body: dict | None) -> BaseModel:
    try:
        return model.model_validate(body or {})
    except ValidationError as exc:
        errors = [
            {
                "field": ".".join(str(p) for p in e["loc"]),
                "message": e["msg"],
            }
            for e in exc.errors()
        ]
        first = errors[0] if errors else {"field": "", "message": "Invalid request"}
        label = first["field"] or "request"
        raise ApiError(
            422, "VALIDATION_ERROR", f"Invalid {label}: {first['message']}", {"errors": errors}
        ) from None


def _int_query(query: dict, name: str, default: int, *, ge: int, le: int) -> int:
    raw = query.get(name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ApiError(422, "VALIDATION_ERROR", f"Invalid {name}: must be an integer") from None
    if value < ge or value > le:
        raise ApiError(422, "VALIDATION_ERROR", f"Invalid {name}: must be between {ge} and {le}")
    return value


def _state_query(query: dict) -> RunState | None:
    raw = query.get("state")
    if not raw:
        return None
    try:
        return RunState(str(raw).upper())
    except ValueError:
        valid = ", ".join(s.value for s in RunState)
        raise ApiError(422, "VALIDATION_ERROR", f"Invalid state: {raw!r}. Valid states: {valid}") from None


def _json(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json", by_alias=True)
    if isinstance(obj, dict):
        return {k: _json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json(v) for v in obj]
    return obj


def _authorize(run_id: str, principal: dict | None, *, as_job: bool):
    """A caller may act on a run if they created it. Otherwise (T-705): a job lists its own user's Fabric
    workspaces (the only caller with a user-delegated token) and writes the decision; a UDF has no such
    token, so it only reads the cached decision (`dbo.nexora_run_access` when `RUN_STORE=fabric_sql`, else
    the in-process cache) -- no fresh row is `403 ACCESS_CHECK_REQUIRED`, so the UI runs `job:check_access`
    and retries; a fresh "not allowed" row is `403 FORBIDDEN` straight away, no job needed."""
    if principal is None:
        raise ApiError(401, "UNAUTHENTICATED", "A signed-in caller is required")
    from app.fabric.job import reconcile_active_job

    run = reconcile_active_job(deps.store(), deps.store().get(run_id))
    owner = run.created_by
    if (
        owner is None
        or owner.get("oid") == principal.get("oid")
        or str(owner.get("upn") or "").casefold() == str(principal.get("upn") or "").casefold()
    ):
        return run
    if as_job:
        if can_access(run, principal.get("oid"), principal.get("upn"), deps.workspace_lister()):
            return run
        raise RunAccessDenied(run_id)
    decision = cached_decision(run, principal.get("oid"))
    if decision is None:
        raise ApiError(
            403,
            "ACCESS_CHECK_REQUIRED",
            "Access to this run must be verified as a job first",
            {"run_id": run_id},
        )
    if decision:
        return run
    raise RunAccessDenied(run_id)


def _visible_to(run, principal: dict) -> bool:
    owner = run.created_by
    if owner is None:
        return True
    return owner.get("oid") == principal.get("oid") or str(owner.get("upn") or "").casefold() == str(
        principal.get("upn") or ""
    ).casefold()


def detail(run, warnings: list[str] | None = None) -> RunDetail:
    runs_root = deps.runs_root()
    return RunDetail.from_run(
        run,
        warnings,
        open_questions=open_questions(runs_root, run.run_id),
        contract=current_contract(runs_root, run.run_id),
        data_gaps=list_data_gaps(runs_root, run.run_id),
    )


ALL_RUNS = 1_000_000


# --------------------------------------------------------------------------
# handlers: health, me, runs, jobs
# --------------------------------------------------------------------------


def _health(call: Call) -> Health:
    return Health(status="ok", app=HEALTH_APP_NAME, version=_wheel_version())


def _me(call: Call) -> MeResponse:
    if call.principal is None:
        raise ApiError(401, "UNAUTHENTICATED", "A signed-in caller is required")
    p = call.principal
    return MeResponse(oid=p["oid"], upn=p["upn"], name=p.get("name", p["upn"]), roles=load_roles().roles_of(p["upn"]))


def _create_run(call: Call) -> RunDetail:
    if call.principal is None:
        raise ApiError(401, "UNAUTHENTICATED", "A signed-in caller is required")
    body = _parse_body(CreateRunRequest, call.body)
    run = deps.store().create(
        body.prompt, body.subject_area, body.desired_outputs, body.constraints, created_by=call.principal
    )
    return RunDetail.from_run(run)


def _list_runs(call: Call) -> RunList:
    if call.principal is None:
        raise ApiError(401, "UNAUTHENTICATED", "A signed-in caller is required")
    state = _state_query(call.query)
    limit = _int_query(call.query, "limit", 50, ge=1, le=200)
    runs = [r for r in deps.store().list(state, limit=ALL_RUNS) if _visible_to(r, call.principal)]
    return RunList(items=[RunSummary.from_run(r) for r in runs[:limit]], total=len(runs))


def _get_run(call: Call) -> RunDetail:
    run = _authorize(call.params["run_id"], call.principal, as_job=call.as_job)
    return detail(run)


def _list_artifacts(call: Call) -> list[ArtifactItem]:
    run = _authorize(call.params["run_id"], call.principal, as_job=call.as_job)
    return [
        ArtifactItem(name=name, version=a.version, path=a.path, hash=a.hash, status=a.status, updated_at=a.updated_at)
        for name, a in run.artifacts.items()
    ]


# --------------------------------------------------------------------------
# requirement compiler and clarification (T-202, T-203, T-204)
# --------------------------------------------------------------------------


def _compile(call: Call) -> CompileResponse:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    actor = call.principal["upn"]
    run, contract = compile_run(deps.store(), deps.runs_root(), run_id, actor=actor, llm=deps.llm())
    questions = open_questions(deps.runs_root(), run_id)
    return CompileResponse(
        run=detail(run), contract=contract, blocking_questions=sum(1 for q in questions if q["blocking"])
    )


def _answer(call: Call) -> QuestionResponse:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    body = _parse_body(AnswerRequest, call.body)
    run, contract = answer_question(
        deps.store(), deps.runs_root(), run_id, call.params["qid"], body.answer, actor=call.principal["upn"], llm=deps.llm()
    )
    return QuestionResponse(run=detail(run), contract=contract)


def _dismiss(call: Call) -> QuestionResponse:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    body = _parse_body(DismissRequest, call.body)
    run, contract = dismiss_question(deps.store(), deps.runs_root(), run_id, call.params["qid"], body.reason)
    return QuestionResponse(run=detail(run), contract=contract)


def _update_kpi(call: Call) -> KpiEditResponse:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    body = _parse_body(KpiEditRequest, call.body)
    changes = body.model_dump(exclude={"expected_hash"}, exclude_unset=True, mode="json")
    contract = edit_kpi(deps.store(), deps.runs_root(), run_id, call.params["kpi_name"], changes, body.expected_hash)
    return KpiEditResponse(contract=contract)


def _check_gate(call: Call) -> GateResult:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    gate = call.params["gate"]
    if gate == "G1":
        return check_g1(deps.store(), deps.runs_root(), run_id)
    if gate == "G2":
        from app.workflow.inventory_review import check_g2

        return check_g2(deps.store(), deps.runs_root(), run_id)
    if gate == "G3":
        return check_g3(deps.store(), deps.runs_root(), run_id)
    raise NotFound(f"No gate {gate!r}; only G1, G2 and G3 are available")


def _approve(call: Call) -> ApprovalResponse:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    body = _parse_body(ApprovalRequest, call.body)
    outcome = perform(
        deps.store(),
        deps.runs_root(),
        run_id,
        body.artifact,
        body.action,
        {"oid": call.principal["oid"], "upn": call.principal["upn"]},
        body.expected_hash,
        comment=body.comment,
        edits=body.edits.model_dump(exclude_unset=True, mode="json") if body.edits else None,
        llm=deps.llm(),
        role=body.role,
        definition_wrong=body.definition_wrong,
        target=body.target,
        report_services=deps.report_services(),
    )
    return ApprovalResponse(
        approval=outcome.approval, run=detail(outcome.run), gate_result=outcome.gate_result, explanation=outcome.explanation
    )


def _approve_host(body: dict | None) -> str:
    return "job" if (body or {}).get("action") == "REGENERATE" else "udf"


def _get_approvals(call: Call) -> list[ApprovalRecord]:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    return list_approvals(deps.store(), deps.runs_root(), run_id)


def _approve_exception(call: Call) -> ExceptionResponse:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    body = _parse_body(ExceptionRequest, call.body)
    record = add_exception(
        deps.store(), deps.runs_root(), run_id, {"oid": call.principal["oid"], "upn": call.principal["upn"]}, body.table, body.rationale
    )
    return ExceptionResponse(
        exception_id=record.approval_id, table=record.artifact.split(":", 1)[1], approved_by=record.user["upn"], at=record.at
    )


def _raise_gap(call: Call) -> RunDetail:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    body = _parse_body(DataGapRequest, call.body)
    run = raise_data_gap(deps.store(), deps.runs_root(), run_id, body.description, call.principal["upn"])
    return detail(run)


# --------------------------------------------------------------------------
# scope, harvest, profile, relationships (T-103, T-301, T-302, T-303)
# --------------------------------------------------------------------------


def _set_scope(call: Call) -> RunDetail:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    body = _parse_body(ScopeRequest, call.body)
    run, warnings = bind_scope(
        deps.store(), deps.fabric_client(), deps.connector_factory(), run_id, body.workspace_id, body.lakehouse_id, body.schemas
    )
    return detail(run, warnings)


def _harvest(call: Call) -> HarvestResponse:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    body = _parse_body(HarvestRequest, call.body) if call.body else None
    run, inventory = harvest_run(
        deps.store(), deps.runs_root(), run_id, deps.connector_factory(), schemas=body.schemas if body else None, actor=call.principal["upn"]
    )
    ref = run.artifacts[INVENTORY]
    return HarvestResponse(
        summary=HarvestSummary(**summary(inventory)),
        artifact=ArtifactItem(name=INVENTORY, version=ref.version, path=ref.path, hash=ref.hash, status=ref.status, updated_at=ref.updated_at),
        warnings=inventory.warnings,
    )


def _profile(call: Call) -> ProfileResponse:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    body = _parse_body(ProfileRequest, call.body) if call.body else None
    profiles = profile_run(deps.store(), deps.runs_root(), run_id, deps.sql_connector_factory(), tables=body.tables if body else None)
    return ProfileResponse(**summarize(profiles))


def _analyze_relationships(call: Call) -> AnalyzeRelationshipsResponse:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    body = _parse_body(AnalyzeRelationshipsRequest, call.body) if call.body else None
    extra = [
        Candidate("user", c.from_.table, c.from_.column, c.to.table, c.to.column) for c in (body.candidates if body else [])
    ]
    evidence, warnings = analyze_run(deps.store(), deps.runs_root(), run_id, deps.sql_connector_factory(), llm=deps.relationship_llm(), extra=extra)
    return AnalyzeRelationshipsResponse(**summarize_relationships(evidence), warnings=warnings)


def _get_relationships(call: Call) -> list[RelationshipEvidence]:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    return list_relationships(deps.runs_root(), run_id)


def _get_inventory(call: Call) -> MetadataInventory:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    return load_inventory(deps.runs_root(), run_id)


# --------------------------------------------------------------------------
# planning (T-401 - T-405)
# --------------------------------------------------------------------------


def _plan_sources(call: Call) -> SourceResolution:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    return resolve_sources(deps.store(), deps.runs_root(), run_id, deps.source_llm())


def _plan_feasibility(call: Call) -> FeasibilityResponse:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    report, run = check_feasibility(deps.store(), deps.runs_root(), run_id, deps.feasibility_llm(), actor=call.principal["upn"])
    return FeasibilityResponse(kpis=report.kpis, run=detail(run))


def _plan_bridges(call: Call) -> list:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    proposals, _ = propose_bridges(deps.store(), deps.runs_root(), run_id, deps.sql_connector_factory(), actor=call.principal["upn"])
    return proposals.proposals


def _plan(call: Call) -> PlanResponse:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    execution_plan, model_spec, validation = generate_plan(deps.store(), deps.runs_root(), run_id)
    return PlanResponse(execution_plan=execution_plan, model_spec=model_spec, validation=validation)


def _get_plan(call: Call) -> PlanResponse:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    execution_plan, model_spec, validation = read_plan(deps.store(), deps.runs_root(), run_id)
    return PlanResponse(execution_plan=execution_plan, model_spec=model_spec, validation=validation)


def _patch_plan(call: Call) -> PlanEditResponse:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    body = _parse_body(PlanEditRequest, call.body)
    model_spec, execution_plan, gate_result = edit_plan(
        deps.store(), deps.runs_root(), run_id, body.expected_hash, body.ops, {"oid": call.principal["oid"], "upn": call.principal["upn"]}
    )
    return PlanEditResponse(model_spec=model_spec, execution_plan=execution_plan, gate_result=gate_result)


def _get_plan_sources(call: Call) -> SourceResolution:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    return load_saved_resolution(deps.runs_root(), run_id)


# --------------------------------------------------------------------------
# semantic model (T-501 - T-507)
# --------------------------------------------------------------------------


def _model_generate(call: Call) -> ModelGenerateResponse:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    version, files, run = generate_model(deps.store(), deps.runs_root(), run_id, actor=call.principal["upn"])
    return ModelGenerateResponse(version=version, files=files, run=detail(run))


def _model_measures(call: Call) -> MeasuresResponse:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    measures, version = add_measures(deps.store(), deps.runs_root(), run_id, deps.dax_llm())
    return MeasuresResponse(measures=measures, version=version)


def _model_validate(call: Call) -> ModelValidateResponse:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    entry, run = validate_model(
        deps.store(), deps.runs_root(), run_id,
        items_client=deps.items_client(), pbi=deps.powerbi_client(), sql_connector_factory=deps.sql_connector_factory(),
        recon_llm=deps.recon_llm(), user_upn=call.principal["upn"], actor=call.principal["upn"],
    )
    return ModelValidateResponse(report=entry, run=detail(run))


def _model_repair(call: Call) -> RepairResult:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    return repair_model(
        deps.store(), deps.runs_root(), run_id, "SemanticModel",
        llm=deps.dax_llm(), items_client=deps.items_client(), pbi=deps.powerbi_client(),
        sql_connector_factory=deps.sql_connector_factory(), recon_llm=deps.recon_llm(),
        user_upn=call.principal["upn"], actor=call.principal["upn"],
    )


def _model_bridge_generate(call: Call) -> BridgeGenerateResponse:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    version, entry, _ = generate_bridge(
        deps.store(), deps.runs_root(), run_id, call.params["bridge_id"],
        items_client=deps.items_client(), pbi=deps.powerbi_client(), sql_connector_factory=deps.sql_connector_factory(),
        recon_llm=deps.recon_llm(), bridge_llm=deps.bridge_llm(), lakehouse_connector_factory=deps.connector_factory(),
        user_upn=call.principal["upn"], actor=call.principal["upn"],
    )
    return BridgeGenerateResponse(version=version, validation=entry)


def _model_review(call: Call) -> ModelReview:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    return review_model(deps.store(), deps.runs_root(), run_id)


# --------------------------------------------------------------------------
# report (T-601 - T-604)
# --------------------------------------------------------------------------


def _report_plan(call: Call) -> ReportPlanResponse:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    plan, validation, run = generate_report_plan(deps.store(), deps.runs_root(), run_id, deps.report_llm(), actor=call.principal["upn"])
    return ReportPlanResponse(plan=plan.to_dict(), validation=validation, run=detail(run))


def _report_plan_read(call: Call) -> ReportPlanResponse:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    plan, validation = read_report_plan(deps.store(), deps.runs_root(), run_id)
    return ReportPlanResponse(plan=plan, validation=validation)


def _report_generate(call: Call) -> ReportGenerateResponse:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    version, files, published, run = generate_report(deps.store(), deps.runs_root(), run_id, deps.items_client(), actor=call.principal["upn"])
    return ReportGenerateResponse(version=version, files=files, report_item_id=published["item_id"], operation=published["operation"], run=detail(run))


def _report_validate(call: Call) -> ReportValidateResponse:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    entry, run = validate_report(deps.store(), deps.runs_root(), run_id, items_client=deps.items_client(), pbi=deps.powerbi_client(), actor=call.principal["upn"])
    return ReportValidateResponse(report=entry, run=detail(run))


def _report_repair(call: Call) -> RepairResult:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    return repair_model(deps.store(), deps.runs_root(), run_id, "Report", items_client=deps.items_client(), pbi=deps.powerbi_client(), actor=call.principal["upn"])


def _report_review(call: Call) -> ReportReview:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    return review_report(deps.store(), deps.runs_root(), run_id)


# The UDF response-size limit is 30 MB (fabric-hosting.md's platform-limits
# table). Base64 adds ~33%, plus the JSON envelope, so a raw file over 20 MB
# can't fit; `/report/review`'s `open_url` is the fallback for those.
_MAX_RUN_FILE_BYTES = 20 * 1024 * 1024


def _run_file(call: Call) -> dict:
    """`{"content_type", "base64", "size"}`: the UDF equivalent of the old
    `FileResponse` (CONTRACTS.md#fabric-operations: `get_run_file`)."""
    import base64

    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    path = call.params["path"]
    runs_root = deps.runs_root()
    root = (Path(runs_root) / run_id).resolve()
    candidate = Path(path)
    if not path or candidate.is_absolute() or ".." in candidate.parts or "\\" in path or ":" in path:
        raise NotFound("No such file")
    target = (root / candidate).resolve()
    if root not in target.parents or not target.is_file():
        raise NotFound("No such file")
    size = target.stat().st_size
    if size > _MAX_RUN_FILE_BYTES:
        raise ApiError(
            413,
            "FILE_TOO_LARGE",
            f"{path} is too large to return from a User Data Function ({size} bytes). Open it in Power BI instead.",
            {"path": path, "size": size},
        )
    return {
        "content_type": mimetypes.guess_type(target.name)[0] or "application/octet-stream",
        "base64": base64.b64encode(target.read_bytes()).decode("ascii"),
        "size": size,
    }


# --------------------------------------------------------------------------
# operational controls (T-701, T-702, T-703)
# --------------------------------------------------------------------------


def _check_access(call: Call) -> dict:
    """T-705 `job:check_access`: run as a job (the only place a user-delegated Fabric token exists), so it
    can list the caller's own workspaces and write the decision a UDF can't make itself. The UI runs this
    after a `403 ACCESS_CHECK_REQUIRED` from a UDF call, then retries the original call."""
    from app.fabric.job import reconcile_active_job
    from app.workflow.run import utc_now

    run_id = call.params["run_id"]
    principal = call.principal
    if principal is None:
        raise ApiError(401, "UNAUTHENTICATED", "A signed-in caller is required")
    run = reconcile_active_job(deps.store(), deps.store().get(run_id))
    owner = run.created_by
    is_owner = owner is None or owner.get("oid") == principal.get("oid") or str(owner.get("upn") or "").casefold() == str(principal.get("upn") or "").casefold()
    allowed = is_owner or (run.scope is not None and can_access(run, principal.get("oid"), principal.get("upn"), deps.workspace_lister()))
    return {"allowed": allowed, "checked_at": utc_now()}


def _return_to_stage(call: Call) -> RunDetail:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    body = _parse_body(ReturnRequest, call.body)
    run = return_run(deps.store(), deps.runs_root(), run_id, body.to, body.reason, {"oid": call.principal["oid"], "upn": call.principal["upn"]})
    return detail(run)


def _get_impact(call: Call) -> ImpactReport:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    return read_impact(deps.store(), deps.runs_root(), run_id)


def _regenerate_stale(call: Call) -> RegenerateStaleResponse:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    result, run = regenerate_stale(
        deps.store(), deps.runs_root(), run_id,
        StaleServices(
            source_llm=deps.source_llm(), feasibility_llm=deps.feasibility_llm(), dax_llm=deps.dax_llm(), recon_llm=deps.recon_llm(),
            items=deps.items_client(), pbi=deps.powerbi_client(), sql_factory=deps.sql_connector_factory(), user_upn=call.principal["upn"],
        ),
        actor=call.principal["upn"],
    )
    return RegenerateStaleResponse(**result.model_dump(), run=detail(run))


def _deploy(call: Call) -> DeployResponse:
    """The job itself: Gate G6 and the hash check first (a refusal is an error
    and nothing runs), then the deployment runs to completion inside this same
    job (CONTRACTS.md#fabric-operations: the old background task becomes the
    job). Progress is in `GET /runs/{id}` (`state`, `deployment`) throughout.

    T-702 FR-3: if the run is already `DEPLOYING` (a previous deployment job was
    cancelled or crashed, leaving nothing further written), this call only
    resumes the rollback from the saved backup and releases the lock; nothing
    is redeployed, and the request body's `target_workspace_id` is ignored in
    favor of the one the crashed job already recorded."""
    run_id = call.params["run_id"]
    run = _authorize(run_id, call.principal, as_job=call.as_job)
    body = _parse_body(DeployRequest, call.body)
    services = PromotionServices(items=deps.promotion_items_client(), pbi=deps.powerbi_client(), workspaces=deps.fabric_client().list_workspaces)
    if run.state is RunState.DEPLOYING:
        resume_rollback(deps.store(), deps.runs_root(), run_id, services, call.principal["upn"])
        return DeployResponse(deployment_id=(run.deployment or {}).get("deployment_id") or "")
    started = start_deployment(deps.store(), deps.runs_root(), run_id, body.target_workspace_id, services, call.principal["upn"])
    run_deployment(deps.store(), deps.runs_root(), run_id, started, services, call.principal["upn"])
    return DeployResponse(deployment_id=started.deployment_id)


def _get_audit(call: Call) -> dict:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    offset = _int_query(call.query, "offset", 0, ge=0, le=ALL_RUNS)
    limit = _int_query(call.query, "limit", 200, ge=1, le=1000)
    return audit_log.audit(deps.store(), deps.runs_root(), run_id, offset, limit)


def _get_trace(call: Call) -> dict:
    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    visual_id = call.query.get("visual_id")
    if not visual_id:
        raise ApiError(422, "VALIDATION_ERROR", "Invalid query: visual_id is required")
    audit_log.record(deps.store(), deps.runs_root(), run_id)
    return trace_visual(deps.store(), deps.runs_root(), run_id, visual_id)


# --------------------------------------------------------------------------
# jobs (POST /runs/{id}/jobs, GET /runs/{id}/jobs/{job_id}) - T-102 itself
# --------------------------------------------------------------------------


def _reserve_job(call: Call) -> dict:
    from app.fabric.job import reserve_job

    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    body = call.body or {}
    operation = body.get("operation")
    if not operation:
        raise ApiError(422, "VALIDATION_ERROR", "Invalid body: operation is required")
    job_id, run = reserve_job(deps.store(), run_id, operation, body.get("body") or {}, call.principal)
    return {"job_id": job_id, "run": _json(detail(run))}


def _get_job(call: Call) -> dict:
    from app.fabric.job import read_job_result

    run_id = call.params["run_id"]
    _authorize(run_id, call.principal, as_job=call.as_job)
    return read_job_result(run_id, call.params["job_id"])


# --------------------------------------------------------------------------
# the operation table (CONTRACTS.md#fabric-operations)
# --------------------------------------------------------------------------

OPERATIONS: dict[tuple[str, str], Operation] = {
    ("GET", "/health"): Operation("udf", _health, needs_run=False),
    ("GET", "/me"): Operation("udf", _me, needs_run=False),
    ("POST", "/runs"): Operation("udf", _create_run, status=201, needs_run=False),
    ("GET", "/runs"): Operation("udf", _list_runs, needs_run=False),
    ("GET", "/runs/{run_id}"): Operation("udf", _get_run),
    ("GET", "/runs/{run_id}/artifacts"): Operation("udf", _list_artifacts),
    ("POST", "/runs/{run_id}/jobs"): Operation("udf", _reserve_job, status=202),
    ("GET", "/runs/{run_id}/jobs/{job_id}"): Operation("udf", _get_job),
    ("PUT", "/runs/{run_id}/scope"): Operation("job", _set_scope),
    ("POST", "/runs/{run_id}/compile"): Operation("job", _compile),
    ("POST", "/runs/{run_id}/questions/{qid}/answer"): Operation("job", _answer),
    ("POST", "/runs/{run_id}/questions/{qid}/dismiss"): Operation("udf", _dismiss),
    ("PUT", "/runs/{run_id}/contract/kpis/{kpi_name}"): Operation("udf", _update_kpi),
    ("POST", "/runs/{run_id}/approvals"): Operation(_approve_host, _approve),
    ("GET", "/runs/{run_id}/approvals"): Operation("udf", _get_approvals),
    ("POST", "/runs/{run_id}/gates/{gate}/check"): Operation("udf", _check_gate),
    ("POST", "/runs/{run_id}/harvest"): Operation("job", _harvest),
    ("POST", "/runs/{run_id}/profile"): Operation("job", _profile),
    ("POST", "/runs/{run_id}/relationships/analyze"): Operation("job", _analyze_relationships),
    ("GET", "/runs/{run_id}/inventory"): Operation("udf", _get_inventory),
    ("GET", "/runs/{run_id}/relationships"): Operation("udf", _get_relationships),
    ("POST", "/runs/{run_id}/exceptions"): Operation("udf", _approve_exception, status=201),
    ("POST", "/runs/{run_id}/data-gaps"): Operation("udf", _raise_gap),
    ("POST", "/runs/{run_id}/plan/sources"): Operation("job", _plan_sources),
    ("POST", "/runs/{run_id}/plan/feasibility"): Operation("job", _plan_feasibility),
    ("POST", "/runs/{run_id}/plan/bridges"): Operation("job", _plan_bridges),
    ("POST", "/runs/{run_id}/plan"): Operation("udf", _plan),
    ("GET", "/runs/{run_id}/plan"): Operation("udf", _get_plan),
    ("PATCH", "/runs/{run_id}/plan"): Operation("udf", _patch_plan),
    ("GET", "/runs/{run_id}/plan/sources"): Operation("udf", _get_plan_sources),
    ("POST", "/runs/{run_id}/model/generate"): Operation("udf", _model_generate),
    ("POST", "/runs/{run_id}/model/measures"): Operation("job", _model_measures),
    ("POST", "/runs/{run_id}/model/validate"): Operation("job", _model_validate),
    ("POST", "/runs/{run_id}/model/repair"): Operation("job", _model_repair),
    ("POST", "/runs/{run_id}/model/bridges/{bridge_id}/generate"): Operation("job", _model_bridge_generate),
    ("GET", "/runs/{run_id}/model/review"): Operation("udf", _model_review),
    ("POST", "/runs/{run_id}/report/plan"): Operation("job", _report_plan),
    ("GET", "/runs/{run_id}/report/plan"): Operation("udf", _report_plan_read),
    ("POST", "/runs/{run_id}/report/generate"): Operation("job", _report_generate),
    ("POST", "/runs/{run_id}/report/validate"): Operation("job", _report_validate),
    ("POST", "/runs/{run_id}/report/repair"): Operation("job", _report_repair),
    ("GET", "/runs/{run_id}/report/review"): Operation("udf", _report_review),
    ("GET", "/runs/{run_id}/files/{path:path}"): Operation("udf", _run_file),
    ("POST", "/runs/{run_id}/check_access"): Operation("job", _check_access),
    ("POST", "/runs/{run_id}/return"): Operation("udf", _return_to_stage),
    ("GET", "/runs/{run_id}/impact"): Operation("udf", _get_impact),
    ("POST", "/runs/{run_id}/regenerate-stale"): Operation("job", _regenerate_stale),
    ("POST", "/runs/{run_id}/deploy"): Operation("job", _deploy, status=202),
    ("GET", "/runs/{run_id}/audit"): Operation("udf", _get_audit),
    ("GET", "/runs/{run_id}/trace"): Operation("udf", _get_trace),
}


def _compile_pattern(pattern: str) -> re.Pattern:
    parts = []
    for segment in pattern.split("/"):
        if segment.startswith("{") and segment.endswith("}"):
            name = segment[1:-1]
            if name.endswith(":path"):
                parts.append(f"(?P<{name[: -len(':path')]}>.+)")
            else:
                parts.append(f"(?P<{name}>[^/]+)")
        else:
            parts.append(re.escape(segment))
    return re.compile("^" + "/".join(parts) + "$")


_ROUTES: list[tuple[str, re.Pattern, str, Operation]] = [
    (method, _compile_pattern(pattern), pattern, op) for (method, pattern), op in OPERATIONS.items()
]


def _match(method: str, path: str) -> tuple[Operation, dict[str, str]] | None:
    normalized = "/" + path.strip("/")
    for route_method, regex, _pattern, op in _ROUTES:
        if route_method != method:
            continue
        m = regex.match(normalized)
        if m:
            return op, m.groupdict()
    return None


_RUN_PATH = re.compile(r"^/runs/([0-9a-fA-F-]{8,})(?:/(.*))?$")
_STAGE_SEGMENT = re.compile(r"^[a-z][a-z-]*$")


def _stage_for(rest: str | None) -> str:
    """`model/bridges/abc/generate` -> `model.bridges.generate`; no path after the run is `run`. Mirrors
    the first build's `app.observability.middleware.stage_name` (T-704), kept local here so the dispatcher
    stays framework-free (no FastAPI import)."""
    parts = [p for p in (rest or "").split("/") if _STAGE_SEGMENT.match(p)]
    return ".".join(parts) or "run"


def dispatch(
    method: str,
    path: str,
    query: dict[str, Any] | None,
    body: dict | None,
    principal: dict | None,
    *,
    as_job: bool,
) -> dict:
    """`{"status", "body"}` (a `headers` key too, when the error carries one).
    `as_job` is True only when `nexora_stage_runner` calls this; a `nexora_api`
    `call()` with a `job:` operation gets `400 BAD_REQUEST` instead of running it.

    T-704: binds `run_id` (and the stage, derived from the path) for every call
    under `/runs/{id}/...`, and records a span `stage.<name>` for a non-GET one
    (there is no HTTP middleware in Fabric, so the dispatcher is where this
    happens; `run_job` additionally binds `job_id` around its own call here).
    """
    run_match = _RUN_PATH.match("/" + path.strip("/"))
    run_id, stage = (run_match.group(1), _stage_for(run_match.group(2))) if run_match else (None, None)

    def _run() -> dict:
        try:
            matched = _match(method.upper(), path)
            if matched is None:
                raise ApiError(404, "NOT_FOUND", f"No such operation: {method} {path}")
            op, params = matched
            host = op.host_for(body)
            if host == "job" and not as_job:
                raise ApiError(400, "BAD_REQUEST", "This is a long operation; start it as a job (POST /runs/{id}/jobs)")
            call = Call(params=params, query=query or {}, body=body, principal=principal, as_job=as_job)
            result = op.handler(call)
            return {"status": op.status, "body": _json(result), "headers": None}
        except ApiError as exc:
            return {"status": exc.status, "body": {"error": {"code": exc.code, "message": str(exc), "details": exc.details}}, "headers": exc.headers}
        except Exception as exc:  # noqa: BLE001  (every business exception is mapped; anything else is a bug)
            return map_exception(exc)

    if run_id is None or method.upper() == "GET":
        with bind_context(run_id=run_id, stage=stage):
            return _run()
    with telemetry.stage_span(stage, run_id=run_id) as span:
        result = _run()
        status = result["status"]
        span.set_outcome("error" if status >= 500 else "rejected" if status >= 400 else "success")
        span.set("http.status_code", status)
        if status >= 500:
            telemetry.record_error(stage)
    return result
