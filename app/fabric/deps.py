"""What the dispatcher builds for each stage service (T-102). Every connector
and client already defaults its `token_provider` to `app.auth.get_token(SCOPE)`
(T-002), which resolves to the job's own user through `notebookutils` in a
Fabric job and to the local interactive credential otherwise — so, unlike the
first build's FastAPI dependencies, none of this needs an OBO exchange or a
per-request user token. A UDF never builds any of these (see
fabric-hosting.md: a UDF has no user-delegated Fabric token at all).
"""

import contextvars
from functools import lru_cache
from pathlib import Path

from app.compiler.requirement_compiler import LlmCall, default_llm
from app.config import PROJECT_ROOT
from app.connectors.fabric_api import FabricApiClient
from app.connectors.fabric_lakehouse import FabricLakehouseConnector
from app.connectors.fabric_sql_connector import FabricSQLConnector
from app.connectors.powerbi_api import PowerBiClient
from app.deploy.fabric_items import FabricItemsClient
from app.discovery.relationship_candidates import RelationshipLlm, default_relationship_llm
from app.discovery.source_resolver import SourceLlm, default_source_llm
from app.model.bridge_sql import LlmCall as BridgeLlm, default_bridge_llm
from app.model.dax_generator import LlmCall as DaxLlm, default_dax_llm
from app.model.recon_sql import LlmCall as ReconLlm, default_recon_llm
from app.report.planner import LlmCall as ReportLlm, default_report_llm
from app.store.base import RunStore
from app.store.factory import get_run_store
from app.workflow.report_review import ReportServices


_store_override: contextvars.ContextVar["RunStore | None"] = contextvars.ContextVar(
    "store_override", default=None
)


def set_store_override(run_store: "RunStore | None") -> None:
    """`nexora_api`'s `function-app.py` uses this: a UDF has no user-delegated
    Fabric token (fabric-hosting.md), so it can't build a `SqlRunStore` the
    usual way (`app.auth.get_token`); instead it builds one from its own
    Manage Connections-injected `fn.FabricSqlConnection` and sets it here for
    the duration of one invocation. A context variable (not a plain global)
    keeps concurrent invocations in the same worker process isolated."""
    _store_override.set(run_store)


@lru_cache(maxsize=1)
def _default_store() -> RunStore:
    return get_run_store()


def store() -> RunStore:
    override = _store_override.get()
    return override if override is not None else _default_store()


def runs_root() -> Path:
    """Where run artifact files (contracts, logs) live locally. T-101's
    ArtifactStore is what stage code should use going forward; stage services
    not yet updated to take it still read this local folder directly."""
    return PROJECT_ROOT / "runs"


def llm() -> LlmCall:
    return default_llm()


def relationship_llm() -> RelationshipLlm:
    return default_relationship_llm()


def source_llm() -> SourceLlm:
    return default_source_llm()


def feasibility_llm() -> SourceLlm:
    return default_source_llm()


def dax_llm() -> DaxLlm:
    return default_dax_llm()


def report_llm() -> ReportLlm:
    return default_report_llm()


def bridge_llm() -> BridgeLlm:
    return default_bridge_llm()


def recon_llm() -> ReconLlm:
    return default_recon_llm()


def report_services() -> ReportServices:
    return ReportServices(
        llm=default_report_llm(),
        items=lambda: FabricItemsClient(),
        pbi=lambda: PowerBiClient(),
    )


def items_client() -> FabricItemsClient:
    """A Fabric items client, acting as the job's user, sandbox-only."""
    return FabricItemsClient()


def promotion_items_client() -> FabricItemsClient:
    """The one Fabric items client allowed to leave the sandbox guard (T-702),
    used only by the promotion job after Gate G6 has passed."""
    return FabricItemsClient(allow_any_workspace=True)


def powerbi_client() -> PowerBiClient:
    return PowerBiClient()


def fabric_client() -> FabricApiClient:
    return FabricApiClient()


def workspace_lister():
    """The job's own user's workspace list (run authorization, T-705's cache,
    and T-103's scope binding). Only callable inside a job."""

    def list_workspaces() -> list[dict]:
        return FabricApiClient().list_workspaces()

    return list_workspaces


def connector_factory():
    """Builds Lakehouse connectors that read OneLake as the job's user."""
    return lambda scope: FabricLakehouseConnector.for_scope(scope)


def sql_connector_factory():
    """Builds SQL endpoint connectors that query as the job's user."""
    return lambda scope: FabricSQLConnector.for_scope(scope)
