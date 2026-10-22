"""Map exceptions to the shared error envelope (CONTRACTS.md#error-envelope),
framework-free (no FastAPI) so both the UDF and job transports can use it.

This is `app/api/errors.py`'s mapping, carried over 1:1 and extended with the
Fabric-only cases (`RUN_BUSY`, `ACCESS_CHECK_REQUIRED`, `app.auth.ConsentRequired`).
`app/api/` (and its FastAPI exception handlers) stays as a local-development
fallback until every "Needs Fabric re-test" task has checked its own operations
against this dispatcher (see CLAUDE.md's "Target hosting" section); nothing here
depends on it.
"""

import logging
from typing import Callable

from azure.core.exceptions import ClientAuthenticationError

from app.auth import ConsentRequired
from app.compiler.critical_terms import ConfigError
from app.connectors.fabric_api import (
    FabricError,
    FabricForbidden,
    FabricNotFound,
    FabricRateLimited,
    FabricTimeout,
)
from app.deploy.fabric_items import FabricOperationFailed, NameConflict, TargetNotAllowed
from app.discovery.model_planner import NoFeasibleKpis
from app.llm.errors import LlmInvalidOutput, LlmTimeout, LlmUpstreamError, PromptRejected
from app.model.bridge_model import BridgeError
from app.model.bridge_sql import BridgeSqlRejected
from app.model.tmdl_generator import ModelGenerationError
from app.repair.service import NothingToRepair, UnsupportedArtifact
from app.security.llm_guard import PayloadRejected
from app.security.run_access import RunAccessDenied
from app.store.base import IllegalTransition, LeaseBusy, RunCorrupt, RunNotFound, StoreUnavailable
from app.workflow.approval import (
    AlreadyApproved,
    Forbidden,
    GateFailed,
    UnknownArtifact,
    UnsupportedAction,
)
from app.workflow.bridge_model_stage import BridgeNotApproved
from app.workflow.bridge_stage import MappingRulesRequired
from app.workflow.clarification import ContractLocked, NotFound, QuestionConflict, StaleVersion
from app.workflow.compile_stage import WrongState
from app.workflow.harvest_stage import ScopeMissing
from app.workflow.inventory_review import NotACandidate
from app.workflow.model_review import InvalidRole
from app.workflow.model_stage import ModelSpecInvalid
from app.workflow.plan_review import PlanEditInvalid
from app.workflow.promotion import HashMismatch, TargetBusy
from app.workflow.report_review import RegenerationOutOfScope
from app.workflow.report_stage import ReportPlanInvalid
from app.workflow.scope import SchemaNotFound, ScopeLocked
from app.workflow.scope_guard import ScopeViolation
from app.workflow.stale import StaleArtifacts

logger = logging.getLogger(__name__)


class ApiError(Exception):
    """A dispatcher-level error that already carries its HTTP status and code
    (bad method/path, validation, job/UDF-host mismatch, authorization) rather
    than a business exception mapped below."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: dict | None = None,
        headers: dict | None = None,
    ):
        self.status = status
        self.code = code
        self.details = details or {}
        self.headers = headers
        super().__init__(message)


def envelope(
    status: int,
    code: str,
    message: str,
    details: dict | None = None,
    headers: dict | None = None,
) -> dict:
    return {
        "status": status,
        "body": {"error": {"code": code, "message": message, "details": details or {}}},
        "headers": headers,
    }


def _fabric_details(exc: FabricError) -> dict:
    return {"fabric_request_id": exc.request_id} if exc.request_id else {}


def _warn_then(log_call, result: dict) -> dict:
    log_call()
    return result


# Ordered exactly like app/api/errors.py's handler registration: a subclass
# (FabricForbidden, …) is listed before its base class (FabricError) so the
# first isinstance match below is always the most specific one.
_HANDLERS: list[tuple[type, Callable[[Exception], dict]]] = [
    (ApiError, lambda exc: envelope(exc.status, exc.code, str(exc), exc.details, exc.headers)),
    (RunNotFound, lambda exc: envelope(404, "RUN_NOT_FOUND", str(exc), {"run_id": exc.run_id})),
    (
        IllegalTransition,
        lambda exc: envelope(
            409, "ILLEGAL_TRANSITION", str(exc), {"from": exc.from_.value, "to": exc.to.value}
        ),
    ),
    (
        LeaseBusy,
        lambda exc: envelope(409, "RUN_BUSY", str(exc), {"run_id": exc.run_id, "held_by": exc.holder}),
    ),
    (ConsentRequired, lambda exc: envelope(403, "CONSENT_REQUIRED", str(exc))),
    (
        WrongState,
        lambda exc: envelope(
            409, "WRONG_STATE", str(exc), {"state": exc.state.value, "allowed": [s.value for s in exc.allowed]}
        ),
    ),
    (Forbidden, lambda exc: envelope(403, "FORBIDDEN", str(exc), {"required_role": exc.role})),
    (
        GateFailed,
        lambda exc: envelope(
            409,
            "GATE_FAILED",
            f"Gate {exc.result.gate} failed: {', '.join(exc.failed)}",
            {
                "gate": exc.result.gate,
                "failed_checks": exc.failed,
                "gate_result": exc.result.model_dump(mode="json"),
            },
        ),
    ),
    (AlreadyApproved, lambda exc: envelope(409, "ALREADY_APPROVED", str(exc))),
    (UnknownArtifact, lambda exc: envelope(422, "VALIDATION_ERROR", str(exc), {"artifact": exc.artifact})),
    (UnsupportedAction, lambda exc: envelope(422, "VALIDATION_ERROR", str(exc), {"artifact": exc.artifact})),
    (NotACandidate, lambda exc: envelope(422, "VALIDATION_ERROR", str(exc), {"table": exc.table})),
    (NotFound, lambda exc: envelope(404, "NOT_FOUND", str(exc))),
    (ContractLocked, lambda exc: envelope(409, "CONTRACT_LOCKED", str(exc))),
    (
        StaleVersion,
        lambda exc: envelope(
            409, "STALE_VERSION", str(exc), {"expected_hash": exc.expected, "current_hash": exc.current}
        ),
    ),
    (PlanEditInvalid, lambda exc: envelope(422, "VALIDATION_ERROR", str(exc))),
    (
        ModelSpecInvalid,
        lambda exc: envelope(
            422,
            "VALIDATION_ERROR",
            str(exc),
            {"failed_checks": [c.model_dump(mode="json") for c in exc.failed]},
        ),
    ),
    (RegenerationOutOfScope, lambda exc: envelope(409, "SCOPE_VIOLATION", str(exc))),
    (StaleArtifacts, lambda exc: envelope(409, "STALE_ARTIFACTS", str(exc), {"stale": exc.names})),
    (RunAccessDenied, lambda exc: envelope(403, "FORBIDDEN", str(exc), {"run_id": exc.run_id})),
    (
        PayloadRejected,
        lambda exc: _warn_then(
            lambda: logger.warning("The model payload was rejected: %s", exc.reason),
            envelope(422, "PAYLOAD_REJECTED", str(exc), {"reason": exc.reason}),
        ),
    ),
    (
        HashMismatch,
        lambda exc: envelope(
            409,
            "HASH_MISMATCH",
            str(exc),
            {"artifact": exc.artifact, "approved_hash": exc.expected, "actual_hash": exc.actual},
        ),
    ),
    (TargetBusy, lambda exc: envelope(409, "TARGET_BUSY", str(exc), {"workspace_id": exc.workspace_id})),
    (ScopeViolation, lambda exc: envelope(409, "SCOPE_VIOLATION", str(exc), {"files": exc.files})),
    (ReportPlanInvalid, lambda exc: envelope(422, "VALIDATION_ERROR", str(exc))),
    (BridgeNotApproved, lambda exc: envelope(409, "BRIDGE_NOT_APPROVED", str(exc), {"bridge_id": exc.bridge_id})),
    (BridgeError, lambda exc: envelope(422, "VALIDATION_ERROR", str(exc))),
    (
        BridgeSqlRejected,
        lambda exc: envelope(
            422, "VALIDATION_ERROR", f"No usable derivation SQL could be produced: {exc.reason}"
        ),
    ),
    (InvalidRole, lambda exc: envelope(422, "VALIDATION_ERROR", str(exc))),
    (NothingToRepair, lambda exc: envelope(409, "WRONG_STATE", str(exc))),
    (UnsupportedArtifact, lambda exc: envelope(422, "VALIDATION_ERROR", str(exc), {"artifact": exc.artifact})),
    (ModelGenerationError, lambda exc: envelope(422, "VALIDATION_ERROR", str(exc))),
    (NoFeasibleKpis, lambda exc: envelope(409, "NO_FEASIBLE_KPIS", str(exc))),
    (MappingRulesRequired, lambda exc: envelope(409, "MAPPING_RULES_REQUIRED", str(exc))),
    (QuestionConflict, lambda exc: envelope(409, "QUESTION_CONFLICT", str(exc))),
    (
        LlmInvalidOutput,
        lambda exc: envelope(
            502,
            "LLM_INVALID_OUTPUT",
            "The model returned an unusable answer. The raw output was saved in the run's logs folder. Try again.",
        ),
    ),
    (LlmTimeout, lambda exc: envelope(504, "UPSTREAM_TIMEOUT", str(exc))),
    (PromptRejected, lambda exc: envelope(422, "PROMPT_REJECTED", str(exc))),
    (
        LlmUpstreamError,
        lambda exc: _warn_then(lambda: logger.error("LLM error: %s", exc), envelope(502, "UPSTREAM_ERROR", str(exc))),
    ),
    (ConfigError, lambda exc: _warn_then(lambda: logger.error("%s", exc), envelope(500, "CONFIG_ERROR", str(exc)))),
    (ScopeLocked, lambda exc: envelope(409, "SCOPE_LOCKED", str(exc), {"state": exc.state.value})),
    (ScopeMissing, lambda exc: envelope(400, "BAD_REQUEST", str(exc))),
    (SchemaNotFound, lambda exc: envelope(404, "SCHEMA_NOT_FOUND", str(exc), {"schema": exc.schema})),
    (TargetNotAllowed, lambda exc: envelope(403, "TARGET_NOT_ALLOWED", str(exc))),
    (NameConflict, lambda exc: envelope(409, "NAME_CONFLICT", str(exc), {"item_type": exc.item_type})),
    (
        FabricOperationFailed,
        lambda exc: envelope(
            502,
            "UPSTREAM_ERROR",
            f"{exc}" + (f" (Fabric request ID: {exc.request_id})" if exc.request_id else ""),
            _fabric_details(exc) | {"fabric_error": exc.details},
        ),
    ),
    (FabricForbidden, lambda exc: envelope(403, "FORBIDDEN", str(exc), _fabric_details(exc))),
    (FabricNotFound, lambda exc: envelope(404, "NOT_FOUND", str(exc), _fabric_details(exc))),
    (
        FabricRateLimited,
        lambda exc: envelope(
            429,
            "RATE_LIMITED",
            f"{exc}. Try again in {exc.retry_after} seconds.",
            _fabric_details(exc) | {"retry_after": exc.retry_after},
            {"Retry-After": str(exc.retry_after)},
        ),
    ),
    (FabricTimeout, lambda exc: envelope(504, "UPSTREAM_TIMEOUT", str(exc), _fabric_details(exc))),
    (
        FabricError,
        lambda exc: _warn_then(
            lambda: logger.error("Fabric error: %s (request id %s)", exc, exc.request_id),
            envelope(
                502,
                "UPSTREAM_ERROR",
                f"{exc}" + (f" (Fabric request ID: {exc.request_id})" if exc.request_id else ""),
                _fabric_details(exc),
            ),
        ),
    ),
    (
        ClientAuthenticationError,
        lambda exc: envelope(401, "UNAUTHENTICATED", str(getattr(exc, "message", None) or "Sign-in failed")),
    ),
    (
        StoreUnavailable,
        lambda exc: _warn_then(
            lambda: logger.error("Run store unavailable: %s", exc),
            envelope(502, "UPSTREAM_ERROR", "The run store is unavailable"),
        ),
    ),
    (
        RunCorrupt,
        lambda exc: _warn_then(lambda: logger.error("%s", exc), envelope(500, "INTERNAL_ERROR", "An internal error occurred")),
    ),
]


def map_exception(exc: Exception) -> dict:
    """`{"status", "body", "headers"}` for any exception a stage service raises.
    Unmapped exceptions become 500 INTERNAL_ERROR (the message never leaks
    details; the real exception is only in the log)."""
    for exc_type, handler in _HANDLERS:
        if isinstance(exc, exc_type):
            return handler(exc)
    logger.exception("Unhandled error in dispatch: %s", exc)
    return envelope(500, "INTERNAL_ERROR", "An internal error occurred")
