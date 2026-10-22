"""Map exceptions to the shared error envelope (CONTRACTS.md#error-envelope)."""

import logging

from azure.core.exceptions import ClientAuthenticationError
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.obo import ConsentRequired, OboFailed
from app.api.security import AuthConfigError, AuthError, AuthUpstreamError
from app.connectors.fabric_api import (
    FabricError,
    FabricForbidden,
    FabricNotFound,
    FabricRateLimited,
    FabricTimeout,
)
from app.store.base import (
    IllegalTransition,
    RunCorrupt,
    RunNotFound,
    StoreUnavailable,
)

from app.compiler.critical_terms import ConfigError
from app.llm.errors import (
    LlmInvalidOutput,
    LlmTimeout,
    LlmUpstreamError,
    PromptRejected,
)
from app.workflow.approval import (
    AlreadyApproved,
    Forbidden,
    GateFailed,
    UnknownArtifact,
    UnsupportedAction,
)
from app.deploy.fabric_items import FabricOperationFailed, NameConflict, TargetNotAllowed
from app.discovery.model_planner import NoFeasibleKpis
from app.workflow.bridge_stage import MappingRulesRequired
from app.workflow.clarification import ContractLocked, NotFound, QuestionConflict, StaleVersion
from app.workflow.compile_stage import WrongState
from app.workflow.harvest_stage import ScopeMissing
from app.workflow.inventory_review import NotACandidate
from app.model.tmdl_generator import ModelGenerationError
from app.model.bridge_model import BridgeError
from app.model.bridge_sql import BridgeSqlRejected
from app.repair.service import NothingToRepair, UnsupportedArtifact
from app.workflow.bridge_model_stage import BridgeNotApproved
from app.workflow.model_review import InvalidRole
from app.workflow.model_stage import ModelSpecInvalid
from app.workflow.report_stage import ReportPlanInvalid
from app.workflow.report_review import RegenerationOutOfScope
from app.workflow.plan_review import PlanEditInvalid
from app.workflow.scope import SchemaNotFound, ScopeLocked
from app.security.llm_guard import PayloadRejected
from app.security.run_access import RunAccessDenied
from app.workflow.promotion import HashMismatch, TargetBusy
from app.workflow.scope_guard import ScopeViolation
from app.workflow.stale import StaleArtifacts

logger = logging.getLogger(__name__)

HTTP_CODES = {404: "NOT_FOUND"}


def error_response(
    status: int,
    code: str,
    message: str,
    details: dict | None = None,
    headers: dict | None = None,
) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "details": details or {}}}
    return JSONResponse(status_code=status, content=body, headers=headers)


def _fabric_details(exc: FabricError) -> dict:
    return {"fabric_request_id": exc.request_id} if exc.request_id else {}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        errors = [
            {
                "field": ".".join(
                    str(p) for p in e["loc"] if p not in ("body", "query", "path")
                ),
                "message": e["msg"],
            }
            for e in exc.errors()
        ]
        first = errors[0] if errors else {"field": "", "message": "Invalid request"}
        label = first["field"] or "request"
        return error_response(
            422,
            "VALIDATION_ERROR",
            f"Invalid {label}: {first['message']}",
            {"errors": errors},
        )

    @app.exception_handler(RunNotFound)
    async def run_not_found(request: Request, exc: RunNotFound):
        return error_response(404, "RUN_NOT_FOUND", str(exc), {"run_id": exc.run_id})

    @app.exception_handler(IllegalTransition)
    async def illegal_transition(request: Request, exc: IllegalTransition):
        return error_response(
            409,
            "ILLEGAL_TRANSITION",
            str(exc),
            {"from": exc.from_.value, "to": exc.to.value},
        )

    @app.exception_handler(AuthError)
    async def unauthenticated(request: Request, exc: AuthError):
        return error_response(
            401,
            exc.code,
            str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(AuthConfigError)
    async def auth_not_configured(request: Request, exc: AuthConfigError):
        logger.error("%s", exc)
        return error_response(500, "CONFIG_ERROR", str(exc))

    @app.exception_handler(AuthUpstreamError)
    async def auth_upstream(request: Request, exc: AuthUpstreamError):
        logger.error("%s", exc)
        return error_response(502, "UPSTREAM_ERROR", str(exc))

    @app.exception_handler(ConsentRequired)
    async def consent_required(request: Request, exc: ConsentRequired):
        return error_response(403, "CONSENT_REQUIRED", str(exc))

    @app.exception_handler(OboFailed)
    async def obo_failed(request: Request, exc: OboFailed):
        return error_response(502, "UPSTREAM_ERROR", str(exc))

    @app.exception_handler(WrongState)
    async def wrong_state(request: Request, exc: WrongState):
        return error_response(
            409,
            "WRONG_STATE",
            str(exc),
            {"state": exc.state.value, "allowed": [s.value for s in exc.allowed]},
        )

    @app.exception_handler(Forbidden)
    async def forbidden(request: Request, exc: Forbidden):
        return error_response(403, "FORBIDDEN", str(exc), {"required_role": exc.role})

    @app.exception_handler(GateFailed)
    async def gate_failed(request: Request, exc: GateFailed):
        return error_response(
            409,
            "GATE_FAILED",
            f"Gate {exc.result.gate} failed: {', '.join(exc.failed)}",
            {
                "gate": exc.result.gate,
                "failed_checks": exc.failed,
                "gate_result": exc.result.model_dump(mode="json"),
            },
        )

    @app.exception_handler(AlreadyApproved)
    async def already_approved(request: Request, exc: AlreadyApproved):
        return error_response(409, "ALREADY_APPROVED", str(exc))

    @app.exception_handler(UnknownArtifact)
    async def unknown_artifact(request: Request, exc: UnknownArtifact):
        return error_response(422, "VALIDATION_ERROR", str(exc), {"artifact": exc.artifact})

    @app.exception_handler(UnsupportedAction)
    async def unsupported_action(request: Request, exc: UnsupportedAction):
        return error_response(422, "VALIDATION_ERROR", str(exc), {"artifact": exc.artifact})

    @app.exception_handler(NotACandidate)
    async def not_a_candidate(request: Request, exc: NotACandidate):
        return error_response(422, "VALIDATION_ERROR", str(exc), {"table": exc.table})

    @app.exception_handler(NotFound)
    async def contract_item_not_found(request: Request, exc: NotFound):
        return error_response(404, "NOT_FOUND", str(exc))

    @app.exception_handler(ContractLocked)
    async def contract_locked(request: Request, exc: ContractLocked):
        return error_response(409, "CONTRACT_LOCKED", str(exc))

    @app.exception_handler(StaleVersion)
    async def stale_version(request: Request, exc: StaleVersion):
        return error_response(
            409,
            "STALE_VERSION",
            str(exc),
            {"expected_hash": exc.expected, "current_hash": exc.current},
        )

    @app.exception_handler(PlanEditInvalid)
    async def plan_edit_invalid(request: Request, exc: PlanEditInvalid):
        return error_response(422, "VALIDATION_ERROR", str(exc))

    @app.exception_handler(ModelSpecInvalid)
    async def model_spec_invalid(request: Request, exc: ModelSpecInvalid):
        return error_response(
            422,
            "VALIDATION_ERROR",
            str(exc),
            {"failed_checks": [c.model_dump(mode="json") for c in exc.failed]},
        )

    @app.exception_handler(RegenerationOutOfScope)
    async def regeneration_out_of_scope(request: Request, exc: RegenerationOutOfScope):
        return error_response(409, "SCOPE_VIOLATION", str(exc))

    @app.exception_handler(StaleArtifacts)
    async def stale_artifacts(request: Request, exc: StaleArtifacts):
        return error_response(409, "STALE_ARTIFACTS", str(exc), {"stale": exc.names})

    @app.exception_handler(RunAccessDenied)
    async def run_access_denied(request: Request, exc: RunAccessDenied):
        return error_response(403, "FORBIDDEN", str(exc), {"run_id": exc.run_id})

    @app.exception_handler(PayloadRejected)
    async def payload_rejected(request: Request, exc: PayloadRejected):
        logger.warning("The model payload was rejected: %s", exc.reason)
        return error_response(422, "PAYLOAD_REJECTED", str(exc), {"reason": exc.reason})

    @app.exception_handler(HashMismatch)
    async def hash_mismatch(request: Request, exc: HashMismatch):
        return error_response(409, "HASH_MISMATCH", str(exc), {"artifact": exc.artifact, "approved_hash": exc.expected, "actual_hash": exc.actual})

    @app.exception_handler(TargetBusy)
    async def target_busy(request: Request, exc: TargetBusy):
        return error_response(409, "TARGET_BUSY", str(exc), {"workspace_id": exc.workspace_id})

    @app.exception_handler(ScopeViolation)
    async def scope_violation(request: Request, exc: ScopeViolation):
        return error_response(409, "SCOPE_VIOLATION", str(exc), {"files": exc.files})

    @app.exception_handler(ReportPlanInvalid)
    async def report_plan_invalid(request: Request, exc: ReportPlanInvalid):
        return error_response(422, "VALIDATION_ERROR", str(exc))

    @app.exception_handler(BridgeNotApproved)
    async def bridge_not_approved(request: Request, exc: BridgeNotApproved):
        return error_response(409, "BRIDGE_NOT_APPROVED", str(exc), {"bridge_id": exc.bridge_id})

    @app.exception_handler(BridgeError)
    async def bridge_error(request: Request, exc: BridgeError):
        return error_response(422, "VALIDATION_ERROR", str(exc))

    @app.exception_handler(BridgeSqlRejected)
    async def bridge_sql_rejected(request: Request, exc: BridgeSqlRejected):
        return error_response(422, "VALIDATION_ERROR", f"No usable derivation SQL could be produced: {exc.reason}")

    @app.exception_handler(InvalidRole)
    async def invalid_role(request: Request, exc: InvalidRole):
        return error_response(422, "VALIDATION_ERROR", str(exc))

    @app.exception_handler(NothingToRepair)
    async def nothing_to_repair(request: Request, exc: NothingToRepair):
        return error_response(409, "WRONG_STATE", str(exc))

    @app.exception_handler(UnsupportedArtifact)
    async def unsupported_artifact(request: Request, exc: UnsupportedArtifact):
        return error_response(422, "VALIDATION_ERROR", str(exc), {"artifact": exc.artifact})

    @app.exception_handler(ModelGenerationError)
    async def model_generation_error(request: Request, exc: ModelGenerationError):
        return error_response(422, "VALIDATION_ERROR", str(exc))

    @app.exception_handler(NoFeasibleKpis)
    async def no_feasible_kpis(request: Request, exc: NoFeasibleKpis):
        return error_response(409, "NO_FEASIBLE_KPIS", str(exc))

    @app.exception_handler(MappingRulesRequired)
    async def mapping_rules_required(request: Request, exc: MappingRulesRequired):
        return error_response(409, "MAPPING_RULES_REQUIRED", str(exc))

    @app.exception_handler(QuestionConflict)
    async def question_conflict(request: Request, exc: QuestionConflict):
        return error_response(409, "QUESTION_CONFLICT", str(exc))

    @app.exception_handler(LlmInvalidOutput)
    async def llm_invalid_output(request: Request, exc: LlmInvalidOutput):
        return error_response(
            502,
            "LLM_INVALID_OUTPUT",
            "The model returned an unusable answer. The raw output was saved in the run's logs folder. Try again.",
        )

    @app.exception_handler(LlmTimeout)
    async def llm_timeout(request: Request, exc: LlmTimeout):
        return error_response(504, "UPSTREAM_TIMEOUT", str(exc))

    @app.exception_handler(PromptRejected)
    async def prompt_rejected(request: Request, exc: PromptRejected):
        return error_response(422, "PROMPT_REJECTED", str(exc))

    @app.exception_handler(LlmUpstreamError)
    async def llm_upstream(request: Request, exc: LlmUpstreamError):
        logger.error("LLM error: %s", exc)
        return error_response(502, "UPSTREAM_ERROR", str(exc))

    @app.exception_handler(ConfigError)
    async def config_error(request: Request, exc: ConfigError):
        logger.error("%s", exc)
        return error_response(500, "CONFIG_ERROR", str(exc))

    @app.exception_handler(ScopeLocked)
    async def scope_locked(request: Request, exc: ScopeLocked):
        return error_response(409, "SCOPE_LOCKED", str(exc), {"state": exc.state.value})

    @app.exception_handler(ScopeMissing)
    async def scope_missing(request: Request, exc: ScopeMissing):
        return error_response(400, "BAD_REQUEST", str(exc))

    @app.exception_handler(SchemaNotFound)
    async def schema_not_found(request: Request, exc: SchemaNotFound):
        return error_response(404, "SCHEMA_NOT_FOUND", str(exc), {"schema": exc.schema})

    @app.exception_handler(TargetNotAllowed)
    async def target_not_allowed(request: Request, exc: TargetNotAllowed):
        return error_response(403, "TARGET_NOT_ALLOWED", str(exc))

    @app.exception_handler(NameConflict)
    async def name_conflict(request: Request, exc: NameConflict):
        return error_response(409, "NAME_CONFLICT", str(exc), {"item_type": exc.item_type})

    @app.exception_handler(FabricOperationFailed)
    async def fabric_operation_failed(request: Request, exc: FabricOperationFailed):
        suffix = f" (Fabric request ID: {exc.request_id})" if exc.request_id else ""
        return error_response(
            502, "UPSTREAM_ERROR", f"{exc}{suffix}", _fabric_details(exc) | {"fabric_error": exc.details}
        )

    @app.exception_handler(FabricForbidden)
    async def fabric_forbidden(request: Request, exc: FabricForbidden):
        return error_response(403, "FORBIDDEN", str(exc), _fabric_details(exc))

    @app.exception_handler(FabricNotFound)
    async def fabric_not_found(request: Request, exc: FabricNotFound):
        return error_response(404, "NOT_FOUND", str(exc), _fabric_details(exc))

    @app.exception_handler(FabricRateLimited)
    async def fabric_rate_limited(request: Request, exc: FabricRateLimited):
        details = _fabric_details(exc) | {"retry_after": exc.retry_after}
        return error_response(
            429,
            "RATE_LIMITED",
            f"{exc}. Try again in {exc.retry_after} seconds.",
            details,
            headers={"Retry-After": str(exc.retry_after)},
        )

    @app.exception_handler(FabricTimeout)
    async def fabric_timeout(request: Request, exc: FabricTimeout):
        return error_response(504, "UPSTREAM_TIMEOUT", str(exc), _fabric_details(exc))

    @app.exception_handler(FabricError)
    async def fabric_error(request: Request, exc: FabricError):
        logger.error("Fabric error: %s (request id %s)", exc, exc.request_id)
        suffix = f" (Fabric request ID: {exc.request_id})" if exc.request_id else ""
        return error_response(
            502, "UPSTREAM_ERROR", f"{exc}{suffix}", _fabric_details(exc)
        )

    @app.exception_handler(ClientAuthenticationError)
    async def sign_in_failed(request: Request, exc: ClientAuthenticationError):
        return error_response(401, "UNAUTHENTICATED", str(exc.message or "Sign-in failed"))

    @app.exception_handler(StoreUnavailable)
    async def store_unavailable(request: Request, exc: StoreUnavailable):
        logger.error("Run store unavailable: %s", exc)
        return error_response(502, "UPSTREAM_ERROR", "The run store is unavailable")

    @app.exception_handler(RunCorrupt)
    async def run_corrupt(request: Request, exc: RunCorrupt):
        logger.error("%s", exc)
        return error_response(500, "INTERNAL_ERROR", "An internal error occurred")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code >= 500:
            code = "INTERNAL_ERROR"
        else:
            code = HTTP_CODES.get(exc.status_code, "BAD_REQUEST")
        return error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return error_response(500, "INTERNAL_ERROR", "An internal error occurred")
