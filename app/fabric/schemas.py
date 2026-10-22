"""Request and response shapes for every Fabric operation (T-102 FR-1). Carried
over verbatim from `app/api/schemas.py` (the first build); `app/api/schemas.py`
now just re-exports from here so the local FastAPI app keeps working."""

import uuid
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator

from app.contracts.validation_report import ValidationRun
from app.contracts.business_contract import BusinessContract, ExpectedExample, Feasibility
from app.contracts.model_plan import ExecutionPlan, ModelSpec
from app.workflow.approval import Action, ApprovalRecord
from app.workflow.gates import GateResult
from app.workflow.inventory_review import DataGap
from app.workflow.regenerate_stale import Regenerated, ValidationSummary
from app.workflow.next_action import next_action_for
from app.workflow.plan_review import PlanOp
from app.workflow.run import MAX_PROMPT_LENGTH, Run
from app.workflow.states import RunState

PREVIEW_LENGTH = 80


def _guid(value: str) -> str:
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError:
        raise ValueError("must be a GUID") from None


Guid = Annotated[str, AfterValidator(_guid)]


class CreateRunRequest(BaseModel):
    prompt: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=1, max_length=MAX_PROMPT_LENGTH
        ),
    ]
    subject_area: str | None = None
    desired_outputs: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)


class RunSummary(BaseModel):
    run_id: str
    state: RunState
    prompt_preview: str
    created_at: str
    created_by: dict[str, Any] | None = None

    @classmethod
    def from_run(cls, run: Run) -> "RunSummary":
        return cls(
            run_id=run.run_id,
            state=run.state,
            prompt_preview=" ".join(run.prompt.split())[:PREVIEW_LENGTH],
            created_at=run.created_at,
            created_by=run.created_by,
        )


class RunList(BaseModel):
    items: list[RunSummary]
    total: int


class RunDetail(Run):
    next_action: str
    open_questions: list[dict[str, Any]] = Field(default_factory=list)  # open questions of the current contract
    contract: BusinessContract | None = None  # the run's current contract (T-205)
    data_gaps: list[DataGap] = Field(default_factory=list)  # raised in the data stage (T-304)
    warnings: list[str] = Field(default_factory=list)

    @classmethod
    def from_run(
        cls,
        run: Run,
        warnings: list[str] | None = None,
        open_questions: list[dict[str, Any]] | None = None,
        contract: BusinessContract | None = None,
        data_gaps: list[DataGap] | None = None,
    ) -> "RunDetail":
        return cls(
            **run.model_dump(),
            next_action=next_action_for(run),
            open_questions=open_questions or [],
            warnings=warnings or [],
            contract=contract,
            data_gaps=data_gaps or [],
        )


class CompileResponse(BaseModel):
    run: RunDetail
    contract: BusinessContract
    blocking_questions: int


class FeasibilityResponse(BaseModel):
    kpis: list[Feasibility]
    run: RunDetail


class PlanResponse(BaseModel):
    execution_plan: ExecutionPlan
    model_spec: ModelSpec
    validation: GateResult


class ReportPlanResponse(BaseModel):
    plan: dict  # ReportPlan.json as saved (contracts/report_plan.py)
    validation: GateResult  # RPLAN.1-RPLAN.7
    run: RunDetail | None = None  # absent when the plan is only read


class ReportGenerateResponse(BaseModel):
    version: int
    files: list[str]  # relative to runs/<id>/report/v<version>/
    report_item_id: str
    operation: Literal["created", "updated"]
    run: RunDetail


class ReportValidateResponse(BaseModel):
    report: ValidationRun  # this validation run's entry of ValidationReport.json (artifact "Report")
    run: RunDetail


class ModelGenerateResponse(BaseModel):
    version: int
    files: list[str]  # relative to runs/<id>/model/v<version>/
    run: RunDetail


class BridgeGenerateResponse(BaseModel):
    version: int  # the TMDL version that includes the bridge
    validation: ValidationRun  # its Gate G4 run, with the G4.BRIDGE.* checks


class ModelValidateResponse(BaseModel):
    report: ValidationRun  # this validation run's entry of ValidationReport.json
    run: RunDetail


class MeasureResult(BaseModel):
    name: str
    dax: str | None = None
    status: Literal["accepted", "rejected"]
    reason: str | None = None


class MeasuresResponse(BaseModel):
    measures: list[MeasureResult]
    version: int  # the TMDL version that includes the accepted measures


class PlanEditRequest(BaseModel):
    expected_hash: str
    ops: Annotated[list[PlanOp], Field(min_length=1, max_length=50)]


class PlanEditResponse(BaseModel):
    model_spec: ModelSpec
    execution_plan: ExecutionPlan
    gate_result: GateResult


class MeResponse(BaseModel):
    oid: str
    upn: str
    name: str
    roles: list[str]


class AnswerRequest(BaseModel):
    answer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class DismissRequest(BaseModel):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class QuestionResponse(BaseModel):
    run: RunDetail
    contract: BusinessContract


class KpiChanges(BaseModel):
    """Partial Kpi: only the fields sent are changed."""

    model_config = {"extra": "forbid"}

    formula_intent: str | None = None
    grain: str | None = None
    filters: list[str] | None = None
    time_logic: str | None = None
    exclusions: list[str] | None = None
    currency: str | None = None
    expected_examples: list[ExpectedExample] | None = None
    rationale: str | None = None


class KpiEditRequest(KpiChanges):
    expected_hash: str


class ContractEdits(BaseModel):
    """The `edits` of an EDIT approval: partial KPI changes by KPI name."""

    model_config = {"extra": "forbid"}

    kpis: dict[str, KpiChanges] = Field(default_factory=dict)


class ApprovalRequest(BaseModel):
    artifact: str
    action: Action
    comment: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None
    edits: ContractEdits | None = None
    expected_hash: str
    role: str | None = None  # the role the user acts as, for the SemanticModel (T-507)
    definition_wrong: bool = False  # a business_owner's Reject says the KPI definition itself is wrong
    target: dict[str, str] | None = None  # the page and/or visual of a REGENERATE of the Report (T-604)

    @model_validator(mode="after")
    def _action_rules(self) -> "ApprovalRequest":
        if self.action is Action.REJECT and not self.comment:
            raise ValueError("a comment is required to reject")
        if self.action is Action.EDIT and not (self.edits and self.edits.kpis):
            raise ValueError("edits.kpis is required for an edit")
        if self.action is Action.REGENERATE and self.artifact == "Report":
            if not (self.target and (self.target.get("page") or self.target.get("visual")) and set(self.target) <= {"page", "visual"}):
                raise ValueError("target is required to regenerate: {\"page\": …, \"visual\": …} with at least one of them")
            if not self.comment:
                raise ValueError("a comment (the guidance) is required to regenerate")
        return self


class ApprovalResponse(BaseModel):
    approval: ApprovalRecord
    run: RunDetail
    gate_result: GateResult | None = None
    explanation: dict[str, Any] | None = None


class KpiEditResponse(BaseModel):
    contract: BusinessContract


class ScopeRequest(BaseModel):
    workspace_id: Guid
    lakehouse_id: Guid
    schemas: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]] = Field(
        default_factory=list
    )


class WorkspaceItem(BaseModel):
    id: str
    display_name: str
    type: str


class SqlEndpointItem(BaseModel):
    server: str
    database: str


class LakehouseItem(BaseModel):
    id: str
    display_name: str
    workspace_id: str
    sql_endpoint: SqlEndpointItem | None = None


class ArtifactItem(BaseModel):
    name: str
    version: int
    path: str
    hash: str
    status: str
    updated_at: str


class HarvestRequest(BaseModel):
    schemas: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]] | None = None


class HarvestSummary(BaseModel):
    tables: int
    columns: int
    errors: int


class HarvestResponse(BaseModel):
    summary: HarvestSummary
    artifact: ArtifactItem
    warnings: list[str] = Field(default_factory=list)


class ProfileRequest(BaseModel):
    tables: list[Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[^.]+\.[^.]+$")]] | None = None


class ProfileResponse(BaseModel):
    profiled: int
    warnings: int
    errors: int


class ColumnRefBody(BaseModel):
    table: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    column: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class RelationshipCandidateBody(BaseModel):
    """A join named by the user. It is measured like any other candidate."""

    from_: ColumnRefBody = Field(alias="from")
    to: ColumnRefBody

    model_config = {"populate_by_name": True}


class AnalyzeRelationshipsRequest(BaseModel):
    candidates: list[RelationshipCandidateBody] = Field(default_factory=list, max_length=50)


class AnalyzeRelationshipsResponse(BaseModel):
    candidates: int
    high: int
    medium: int
    low: int
    rejected: int
    warnings: list[str] = Field(default_factory=list)


class ExceptionRequest(BaseModel):
    table: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[^.]+\.[^.]+$")]
    rationale: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class ExceptionResponse(BaseModel):
    exception_id: str
    table: str
    approved_by: str
    at: str


class DataGapRequest(BaseModel):
    description: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class Health(BaseModel):
    status: str
    app: str
    version: str


class ReturnRequest(BaseModel):
    to: Literal["REQUIREMENT_ANALYSIS", "PLANNING", "MODEL"]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class DeployRequest(BaseModel):
    target_workspace_id: Guid


class DeployResponse(BaseModel):
    deployment_id: str


class RegenerateStaleResponse(BaseModel):
    regenerated: list[Regenerated]
    validation: list[ValidationSummary]
    needs_review: list[str] = Field(default_factory=list)  # removed objects, and what a person must decide
    run: RunDetail
