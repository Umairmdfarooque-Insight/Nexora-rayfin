"""Run endpoints. Handlers only call the RunStore and shape the response."""

from pathlib import Path

import mimetypes

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import FileResponse

from app.api.deps import (
    get_connector_factory,
    get_bridge_llm,
    get_dax_llm,
    get_items_client,
    get_powerbi_client,
    get_promotion_items_client,
    get_recon_llm,
    get_fabric_client,
    get_feasibility_llm,
    get_llm,
    get_relationship_llm,
    get_report_llm,
    get_report_services,
    get_runs_root,
    get_source_llm,
    get_sql_connector_factory,
    get_store,
    get_workspace_lister,
)
from app.api.schemas import (
    AnalyzeRelationshipsRequest,
    AnalyzeRelationshipsResponse,
    AnswerRequest,
    ApprovalRequest,
    ApprovalResponse,
    ArtifactItem,
    CompileResponse,
    CreateRunRequest,
    DeployRequest,
    DeployResponse,
    DataGapRequest,
    DismissRequest,
    ExceptionRequest,
    ExceptionResponse,
    FeasibilityResponse,
    HarvestRequest,
    HarvestResponse,
    HarvestSummary,
    KpiEditRequest,
    BridgeGenerateResponse,
    MeasuresResponse,
    ModelGenerateResponse,
    ModelValidateResponse,
    KpiEditResponse,
    ProfileRequest,
    PlanEditRequest,
    PlanEditResponse,
    PlanResponse,
    ProfileResponse,
    QuestionResponse,
    ReportGenerateResponse,
    ReportPlanResponse,
    ReportValidateResponse,
    RegenerateStaleResponse,
    ReturnRequest,
    RunDetail,
    RunList,
    RunSummary,
    ScopeRequest,
)
from app.api.security import Principal, current_user
from app.connectors.fabric_api import FabricApiClient
from app.security.run_access import RunAccessDenied, can_access
from app.store.base import RunStore
from app.workflow.approval import ApprovalRecord, check_g1, list_approvals, perform
from app.workflow.clarification import (
    NotFound,
    answer_question,
    dismiss_question,
    edit_kpi,
)
from app.workflow.compile_stage import compile_run, current_contract, open_questions
from app.contracts.metadata_inventory import MetadataInventory
from app.workflow.gates import GateResult
from app.workflow.inventory_review import (
    add_exception,
    check_g2,
    list_data_gaps,
    raise_data_gap,
)
from app.workflow.harvest_stage import ARTIFACT as INVENTORY, harvest_run, load_inventory, summary
from app.contracts.metadata_inventory import RelationshipEvidence
from app.discovery.relationship_candidates import Candidate
from app.workflow.profile_stage import profile_run, summarize
from app.workflow.relationship_stage import analyze_run, list_relationships
from app.workflow.relationship_stage import summarize as summarize_relationships
from app.contracts.source_resolution import SourceResolution
from app.contracts.bridge_proposal import BridgeProposal
from app.workflow.bridge_stage import propose_bridges
from app.workflow.feasibility_stage import check_feasibility
from app.model.dax_generator import LlmCall as DaxLlm
from app.workflow.measure_stage import add_measures
from app.report.planner import LlmCall as ReportLlm
from app.workflow.report_plan_stage import generate_report_plan, read_report_plan
from app.workflow.report_stage import generate_report
from app.workflow.report_validate_stage import validate_report
from app.workflow.report_review import ReportReview, ReportServices, review as review_report
from app.connectors.powerbi_api import PowerBiClient
from app.deploy.fabric_items import FabricItemsClient
from app.model.recon_sql import LlmCall as ReconLlm
from app.repair.models import RepairResult
from app.repair.service import repair as repair_model
from app.model.bridge_sql import LlmCall as BridgeLlm
from app.workflow.bridge_model_stage import generate_bridge
from app.workflow.model_review import ModelReview, review as review_model
from app.workflow.model_stage import generate_model
from app.workflow.validate_stage import validate_model
from app.workflow.plan_review import check_g3, edit_plan
from app.workflow.plan_stage import generate_plan, read_plan
from app.workflow.source_stage import load_saved_resolution
from app.workflow.scope import bind_scope
from app.workflow.source_stage import resolve_sources
from app.workflow import audit as audit_log
from app.workflow.trace import trace as trace_visual
from app.workflow.impact import ImpactReport, read_impact, return_run
from app.workflow.regenerate_stale import StaleServices, regenerate_stale
from app.workflow.promotion import Services as PromotionServices, run_deployment, start_deployment
from app.workflow.states import RunState

def authorize_run(
    request: Request,
    user: Principal = Depends(current_user),
    store: RunStore = Depends(get_store),
    workspaces=Depends(get_workspace_lister),
) -> None:
    """T-705: on every route with a run id, the user must have created the run or be able to access its
    workspace (checked through Fabric, cached for 5 minutes). Otherwise 403 FORBIDDEN."""
    run_id = request.path_params.get("run_id")
    if run_id is None:
        return
    if not can_access(store.get(run_id), user.oid, user.upn, workspaces):
        raise RunAccessDenied(run_id)


router = APIRouter(
    prefix="/runs", tags=["runs"], dependencies=[Depends(current_user), Depends(authorize_run)]
)

def detail(run, runs_root: Path, warnings: list[str] | None = None) -> RunDetail:
    """The run with its open questions and current contract."""
    return RunDetail.from_run(
        run,
        warnings,
        open_questions=open_questions(runs_root, run.run_id),
        contract=current_contract(runs_root, run.run_id),
        data_gaps=list_data_gaps(runs_root, run.run_id),
    )


# The store has no count query yet, so total is taken from an unbounded list.
ALL_RUNS = 1_000_000


@router.post("", status_code=201, response_model=RunDetail)
def create_run(
    body: CreateRunRequest,
    store: RunStore = Depends(get_store),
    user: Principal = Depends(current_user),
):
    run = store.create(
        body.prompt,
        body.subject_area,
        body.desired_outputs,
        body.constraints,
        created_by=user.model_dump(),
    )
    return RunDetail.from_run(run)


@router.get("", response_model=RunList)
def list_runs(
    state: RunState | None = None,
    limit: int = Query(50, ge=1, le=200),
    store: RunStore = Depends(get_store),
    user: Principal = Depends(current_user),
    workspaces=Depends(get_workspace_lister),
):
    runs = [r for r in store.list(state, limit=ALL_RUNS) if can_access(r, user.oid, user.upn, workspaces)]
    return RunList(
        items=[RunSummary.from_run(r) for r in runs[:limit]], total=len(runs)
    )


@router.get("/{run_id}", response_model=RunDetail)
def get_run(
    run_id: str,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
):
    run = store.get(run_id)
    return detail(run, runs_root)


@router.post("/{run_id}/compile", response_model=CompileResponse)
def compile_requirement(
    run_id: str,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
    llm=Depends(get_llm),
    user: Principal = Depends(current_user),
):
    run, contract = compile_run(store, runs_root, run_id, actor=user.upn, llm=llm)
    questions = open_questions(runs_root, run_id)
    return CompileResponse(
        run=detail(run, runs_root),
        contract=contract,
        blocking_questions=sum(1 for q in questions if q["blocking"]),
    )


def _question_response(run, contract, runs_root: Path) -> QuestionResponse:
    return QuestionResponse(
        run=detail(run, runs_root),
        contract=contract,
    )


@router.post("/{run_id}/questions/{qid}/answer", response_model=QuestionResponse)
def answer(
    run_id: str,
    qid: str,
    body: AnswerRequest,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
    llm=Depends(get_llm),
    user: Principal = Depends(current_user),
):
    run, contract = answer_question(
        store, runs_root, run_id, qid, body.answer, actor=user.upn, llm=llm
    )
    return _question_response(run, contract, runs_root)


@router.post("/{run_id}/questions/{qid}/dismiss", response_model=QuestionResponse)
def dismiss(
    run_id: str,
    qid: str,
    body: DismissRequest,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
):
    run, contract = dismiss_question(store, runs_root, run_id, qid, body.reason)
    return _question_response(run, contract, runs_root)


@router.put("/{run_id}/contract/kpis/{kpi_name}", response_model=KpiEditResponse)
def update_kpi(
    run_id: str,
    kpi_name: str,
    body: KpiEditRequest,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
):
    changes = body.model_dump(exclude={"expected_hash"}, exclude_unset=True, mode="json")
    contract = edit_kpi(store, runs_root, run_id, kpi_name, changes, body.expected_hash)
    return KpiEditResponse(contract=contract)


@router.post("/{run_id}/gates/{gate}/check", response_model=GateResult)
def check_gate(
    run_id: str,
    gate: str,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
):
    if gate == "G1":
        return check_g1(store, runs_root, run_id)
    if gate == "G2":
        return check_g2(store, runs_root, run_id)
    if gate == "G3":
        return check_g3(store, runs_root, run_id)
    raise NotFound(f"No gate {gate!r}; only G1, G2 and G3 are available")


@router.post("/{run_id}/approvals", response_model=ApprovalResponse)
def approve(
    run_id: str,
    body: ApprovalRequest,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
    llm=Depends(get_llm),
    report_services: ReportServices = Depends(get_report_services),
    user: Principal = Depends(current_user),
):
    outcome = perform(
        store,
        runs_root,
        run_id,
        body.artifact,
        body.action,
        {"oid": user.oid, "upn": user.upn},
        body.expected_hash,
        comment=body.comment,
        edits=body.edits.model_dump(exclude_unset=True, mode="json") if body.edits else None,
        llm=llm,
        role=body.role,
        definition_wrong=body.definition_wrong,
        target=body.target,
        report_services=report_services,
    )
    return ApprovalResponse(
        approval=outcome.approval,
        run=detail(outcome.run, runs_root),
        gate_result=outcome.gate_result,
        explanation=outcome.explanation,
    )


@router.get("/{run_id}/approvals", response_model=list[ApprovalRecord])
def get_approvals(
    run_id: str,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
):
    return list_approvals(store, runs_root, run_id)


@router.post("/{run_id}/exceptions", status_code=201, response_model=ExceptionResponse)
def approve_exception(
    run_id: str,
    body: ExceptionRequest,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
    user: Principal = Depends(current_user),
):
    record = add_exception(
        store, runs_root, run_id, {"oid": user.oid, "upn": user.upn}, body.table, body.rationale
    )
    return ExceptionResponse(
        exception_id=record.approval_id,
        table=record.artifact.split(":", 1)[1],
        approved_by=record.user["upn"],
        at=record.at,
    )


@router.post("/{run_id}/data-gaps", response_model=RunDetail)
def raise_gap(
    run_id: str,
    body: DataGapRequest,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
    user: Principal = Depends(current_user),
):
    run = raise_data_gap(store, runs_root, run_id, body.description, user.upn)
    return detail(run, runs_root)


@router.put("/{run_id}/scope", response_model=RunDetail)
def set_scope(
    run_id: str,
    body: ScopeRequest,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
    fabric: FabricApiClient = Depends(get_fabric_client),
    connector_for_scope=Depends(get_connector_factory),
):
    run, warnings = bind_scope(
        store,
        fabric,
        connector_for_scope,
        run_id,
        body.workspace_id,
        body.lakehouse_id,
        body.schemas,
    )
    return detail(run, runs_root, warnings)


@router.post("/{run_id}/harvest", response_model=HarvestResponse)
def harvest(
    run_id: str,
    body: HarvestRequest | None = None,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
    connector_for_scope=Depends(get_connector_factory),
    user: Principal = Depends(current_user),
):
    run, inventory = harvest_run(
        store,
        runs_root,
        run_id,
        connector_for_scope,
        schemas=body.schemas if body else None,
        actor=user.upn,
    )
    ref = run.artifacts[INVENTORY]
    return HarvestResponse(
        summary=HarvestSummary(**summary(inventory)),
        artifact=ArtifactItem(
            name=INVENTORY,
            version=ref.version,
            path=ref.path,
            hash=ref.hash,
            status=ref.status,
            updated_at=ref.updated_at,
        ),
        warnings=inventory.warnings,
    )


@router.post("/{run_id}/profile", response_model=ProfileResponse)
def profile(
    run_id: str,
    body: ProfileRequest | None = None,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
    sql_connector_for_scope=Depends(get_sql_connector_factory),
):
    profiles = profile_run(
        store,
        runs_root,
        run_id,
        sql_connector_for_scope,
        tables=body.tables if body else None,
    )
    return ProfileResponse(**summarize(profiles))


@router.post("/{run_id}/relationships/analyze", response_model=AnalyzeRelationshipsResponse)
def analyze_relationships(
    run_id: str,
    body: AnalyzeRelationshipsRequest | None = None,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
    sql_connector_for_scope=Depends(get_sql_connector_factory),
    llm=Depends(get_relationship_llm),
):
    extra = [
        Candidate("user", c.from_.table, c.from_.column, c.to.table, c.to.column)
        for c in (body.candidates if body else [])
    ]
    evidence, warnings = analyze_run(
        store, runs_root, run_id, sql_connector_for_scope, llm=llm, extra=extra
    )
    return AnalyzeRelationshipsResponse(**summarize_relationships(evidence), warnings=warnings)


@router.get("/{run_id}/relationships", response_model=list[RelationshipEvidence])
def get_relationships(
    run_id: str,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
):
    store.get(run_id)
    return list_relationships(runs_root, run_id)


@router.post("/{run_id}/plan/sources", response_model=SourceResolution)
def plan_sources(
    run_id: str,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
    llm=Depends(get_source_llm),
):
    return resolve_sources(store, runs_root, run_id, llm)


@router.post("/{run_id}/plan/feasibility", response_model=FeasibilityResponse)
def plan_feasibility(
    run_id: str,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
    llm=Depends(get_feasibility_llm),
    user: Principal = Depends(current_user),
):
    report, run = check_feasibility(store, runs_root, run_id, llm, actor=user.upn)
    return FeasibilityResponse(kpis=report.kpis, run=detail(run, runs_root))


@router.post("/{run_id}/plan/bridges", response_model=list[BridgeProposal])
def plan_bridges(
    run_id: str,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
    sql_connector_for_scope=Depends(get_sql_connector_factory),
    user: Principal = Depends(current_user),
):
    proposals, _ = propose_bridges(store, runs_root, run_id, sql_connector_for_scope, actor=user.upn)
    return proposals.proposals


@router.post("/{run_id}/plan", response_model=PlanResponse, response_model_by_alias=True)
def plan(
    run_id: str,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
):
    execution_plan, model_spec, validation = generate_plan(store, runs_root, run_id)
    return PlanResponse(execution_plan=execution_plan, model_spec=model_spec, validation=validation)


@router.get("/{run_id}/plan", response_model=PlanResponse, response_model_by_alias=True)
def get_plan(
    run_id: str,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
):
    execution_plan, model_spec, validation = read_plan(store, runs_root, run_id)
    return PlanResponse(execution_plan=execution_plan, model_spec=model_spec, validation=validation)


@router.post("/{run_id}/model/generate", response_model=ModelGenerateResponse)
def model_generate(
    run_id: str,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
    user: Principal = Depends(current_user),
):
    version, files, run = generate_model(store, runs_root, run_id, actor=user.upn)
    return ModelGenerateResponse(version=version, files=files, run=detail(run, runs_root))


@router.post("/{run_id}/model/measures", response_model=MeasuresResponse)
def model_measures(
    run_id: str,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
    llm: DaxLlm = Depends(get_dax_llm),
):
    measures, version = add_measures(store, runs_root, run_id, llm)
    return MeasuresResponse(measures=measures, version=version)


@router.post("/{run_id}/model/validate", response_model=ModelValidateResponse)
def model_validate(
    run_id: str,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
    items: FabricItemsClient = Depends(get_items_client),
    pbi: PowerBiClient = Depends(get_powerbi_client),
    sql_factory=Depends(get_sql_connector_factory),
    llm: ReconLlm = Depends(get_recon_llm),
    user: Principal = Depends(current_user),
):
    entry, run = validate_model(
        store, runs_root, run_id,
        items_client=items, pbi=pbi, sql_connector_factory=sql_factory, recon_llm=llm,
        user_upn=user.upn, actor=user.upn,
    )
    return ModelValidateResponse(report=entry, run=detail(run, runs_root))


@router.post("/{run_id}/model/repair", response_model=RepairResult)
def model_repair(
    run_id: str,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
    items: FabricItemsClient = Depends(get_items_client),
    pbi: PowerBiClient = Depends(get_powerbi_client),
    sql_factory=Depends(get_sql_connector_factory),
    dax_llm: DaxLlm = Depends(get_dax_llm),
    recon_llm: ReconLlm = Depends(get_recon_llm),
    user: Principal = Depends(current_user),
):
    return repair_model(
        store, runs_root, run_id, "SemanticModel",
        llm=dax_llm, items_client=items, pbi=pbi, sql_connector_factory=sql_factory, recon_llm=recon_llm,
        user_upn=user.upn, actor=user.upn,
    )


@router.post("/{run_id}/model/bridges/{bridge_id}/generate", response_model=BridgeGenerateResponse)
def model_bridge_generate(
    run_id: str,
    bridge_id: str,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
    items: FabricItemsClient = Depends(get_items_client),
    pbi: PowerBiClient = Depends(get_powerbi_client),
    sql_factory=Depends(get_sql_connector_factory),
    lakehouse_factory=Depends(get_connector_factory),
    recon_llm: ReconLlm = Depends(get_recon_llm),
    bridge_llm: BridgeLlm = Depends(get_bridge_llm),
    user: Principal = Depends(current_user),
):
    version, entry, _ = generate_bridge(
        store, runs_root, run_id, bridge_id,
        items_client=items, pbi=pbi, sql_connector_factory=sql_factory, recon_llm=recon_llm,
        bridge_llm=bridge_llm, lakehouse_connector_factory=lakehouse_factory, user_upn=user.upn, actor=user.upn,
    )
    return BridgeGenerateResponse(version=version, validation=entry)


@router.post("/{run_id}/report/plan", response_model=ReportPlanResponse)
def report_plan(
    run_id: str,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
    llm: ReportLlm = Depends(get_report_llm),
    user: Principal = Depends(current_user),
):
    plan, validation, run = generate_report_plan(store, runs_root, run_id, llm, actor=user.upn)
    return ReportPlanResponse(plan=plan.to_dict(), validation=validation, run=detail(run, runs_root))


@router.post("/{run_id}/report/generate", response_model=ReportGenerateResponse)
def report_generate(
    run_id: str,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
    items: FabricItemsClient = Depends(get_items_client),
    user: Principal = Depends(current_user),
):
    version, files, published, run = generate_report(store, runs_root, run_id, items, actor=user.upn)
    return ReportGenerateResponse(
        version=version, files=files, report_item_id=published["item_id"], operation=published["operation"], run=detail(run, runs_root)
    )


@router.post("/{run_id}/report/validate", response_model=ReportValidateResponse)
def report_validate(
    run_id: str,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
    items: FabricItemsClient = Depends(get_items_client),
    pbi: PowerBiClient = Depends(get_powerbi_client),
    user: Principal = Depends(current_user),
):
    entry, run = validate_report(store, runs_root, run_id, items_client=items, pbi=pbi, actor=user.upn)
    return ReportValidateResponse(report=entry, run=detail(run, runs_root))


@router.post("/{run_id}/report/repair", response_model=RepairResult)
def report_repair(
    run_id: str,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
    items: FabricItemsClient = Depends(get_items_client),
    pbi: PowerBiClient = Depends(get_powerbi_client),
    user: Principal = Depends(current_user),
):
    return repair_model(store, runs_root, run_id, "Report", items_client=items, pbi=pbi, actor=user.upn)


@router.get("/{run_id}/report/plan", response_model=ReportPlanResponse)
def report_plan_read(
    run_id: str,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
):
    plan, validation = read_report_plan(store, runs_root, run_id)
    return ReportPlanResponse(plan=plan, validation=validation)


@router.get("/{run_id}/report/review", response_model=ReportReview)
def report_review(
    run_id: str,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
):
    return review_report(store, runs_root, run_id)


@router.get("/{run_id}/files/{path:path}")
def run_file(
    run_id: str,
    path: str,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
):
    """A file inside the run's own folder (the page renders, for example). Anything that resolves
    outside `runs/<id>/` is a 404, as is anything that isn't a file."""
    store.get(run_id)  # 404 for an unknown run
    root = (Path(runs_root) / run_id).resolve()
    candidate = Path(path)
    if not path or candidate.is_absolute() or ".." in candidate.parts or "\\" in path or ":" in path:
        raise NotFound("No such file")
    target = (root / candidate).resolve()
    if root not in target.parents or not target.is_file():
        raise NotFound("No such file")
    return FileResponse(target, media_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream", headers={"Cache-Control": "no-store"})


@router.get("/{run_id}/model/review", response_model=ModelReview)
def model_review(
    run_id: str,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
):
    return review_model(store, runs_root, run_id)


@router.patch("/{run_id}/plan", response_model=PlanEditResponse, response_model_by_alias=True)
def patch_plan(
    run_id: str,
    body: PlanEditRequest,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
    user: Principal = Depends(current_user),
):
    model_spec, execution_plan, gate_result = edit_plan(
        store, runs_root, run_id, body.expected_hash, body.ops, {"oid": user.oid, "upn": user.upn}
    )
    return PlanEditResponse(model_spec=model_spec, execution_plan=execution_plan, gate_result=gate_result)


@router.get("/{run_id}/plan/sources", response_model=SourceResolution)
def get_plan_sources(
    run_id: str,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
):
    store.get(run_id)
    return load_saved_resolution(runs_root, run_id)


@router.get("/{run_id}/inventory", response_model=MetadataInventory, response_model_by_alias=True)
def get_inventory(
    run_id: str,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
):
    store.get(run_id)
    return load_inventory(runs_root, run_id)


@router.get("/{run_id}/artifacts", response_model=list[ArtifactItem])
def list_artifacts(run_id: str, store: RunStore = Depends(get_store)):
    run = store.get(run_id)
    return [
        ArtifactItem(
            name=name,
            version=a.version,
            path=a.path,
            hash=a.hash,
            status=a.status,
            updated_at=a.updated_at,
        )
        for name, a in run.artifacts.items()
    ]


@router.post("/{run_id}/return", response_model=RunDetail)
def return_to_stage(
    run_id: str,
    body: ReturnRequest,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
    user: Principal = Depends(current_user),
):
    run = return_run(store, runs_root, run_id, body.to, body.reason, {"oid": user.oid, "upn": user.upn})
    return detail(run, runs_root)


@router.get("/{run_id}/impact", response_model=ImpactReport)
def get_impact(
    run_id: str,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
):
    return read_impact(store, runs_root, run_id)


@router.post("/{run_id}/regenerate-stale", response_model=RegenerateStaleResponse)
def regenerate_stale_objects(
    run_id: str,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
    source_llm=Depends(get_source_llm),
    feasibility_llm=Depends(get_feasibility_llm),
    dax_llm: DaxLlm = Depends(get_dax_llm),
    recon_llm: ReconLlm = Depends(get_recon_llm),
    items: FabricItemsClient = Depends(get_items_client),
    pbi: PowerBiClient = Depends(get_powerbi_client),
    sql_factory=Depends(get_sql_connector_factory),
    user: Principal = Depends(current_user),
):
    result, run = regenerate_stale(
        store, runs_root, run_id,
        StaleServices(
            source_llm=source_llm, feasibility_llm=feasibility_llm, dax_llm=dax_llm, recon_llm=recon_llm,
            items=items, pbi=pbi, sql_factory=sql_factory, user_upn=user.upn,
        ),
        actor=user.upn,
    )
    return RegenerateStaleResponse(**result.model_dump(), run=detail(run, runs_root))


@router.post("/{run_id}/deploy", status_code=202, response_model=DeployResponse)
def deploy(
    run_id: str,
    body: DeployRequest,
    background: BackgroundTasks,
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
    items: FabricItemsClient = Depends(get_promotion_items_client),
    pbi: PowerBiClient = Depends(get_powerbi_client),
    fabric: FabricApiClient = Depends(get_fabric_client),
    user: Principal = Depends(current_user),
):
    """Gate G6 and the hash check run now (a refusal is an error response); the deployment itself runs in
    the background and its progress is in `GET /runs/{id}` (state and `deployment`)."""
    services = PromotionServices(items=items, pbi=pbi, workspaces=fabric.list_workspaces)
    started = start_deployment(store, runs_root, run_id, body.target_workspace_id, services, user.upn)
    background.add_task(run_deployment, store, runs_root, run_id, started, services, user.upn)
    return DeployResponse(deployment_id=started.deployment_id)


@router.get("/{run_id}/audit")
def get_audit(
    run_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
):
    """The audit timeline, a page at a time, with whether the event chain is intact."""
    return audit_log.audit(store, runs_root, run_id, offset, limit)


@router.get("/{run_id}/trace")
def get_trace(
    run_id: str,
    visual_id: str = Query(..., min_length=1),
    store: RunStore = Depends(get_store),
    runs_root: Path = Depends(get_runs_root),
):
    """A visual traced back to its measure, DAX, approved KPI, source columns and the prompt."""
    audit_log.record(store, runs_root, run_id)
    return trace_visual(store, runs_root, run_id, visual_id)
