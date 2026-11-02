"""Shared FastAPI dependencies."""

from functools import lru_cache
from pathlib import Path
from typing import Callable

from fastapi import Depends

from app.api.obo import get_msal_app, obo_token_provider
from app.api.security import _dev_bypass_enabled, user_token
from app.auth import FABRIC_SCOPE, POWERBI_SCOPE, SQL_SCOPE, STORAGE_SCOPE
from app.auth import get_token as _personal_token
from app.connectors.fabric_api import FabricApiClient
from app.connectors.powerbi_api import PowerBiClient
from app.deploy.fabric_items import FabricItemsClient
from app.model.bridge_sql import LlmCall as BridgeLlm, default_bridge_llm
from app.model.recon_sql import LlmCall as ReconLlm, default_recon_llm
from app.compiler.requirement_compiler import LlmCall, default_llm
from app.model.dax_generator import LlmCall as DaxLlm, default_dax_llm
from app.report.planner import LlmCall as ReportLlm, default_report_llm
from app.workflow.report_review import ReportServices
from app.discovery.relationship_candidates import RelationshipLlm, default_relationship_llm
from app.discovery.source_resolver import SourceLlm, default_source_llm
from app.config import PROJECT_ROOT
from app.connectors.fabric_lakehouse import FabricLakehouseConnector
from app.connectors.fabric_sql_connector import FabricSQLConnector
from app.store.base import RunStore
from app.store.factory import get_run_store


@lru_cache(maxsize=1)
def _store() -> RunStore:
    return get_run_store()


def get_store() -> RunStore:
    return _store()


def get_runs_root() -> Path:
    """Where run artifact files (contracts, logs) live. Both stores keep them here."""
    return PROJECT_ROOT / "runs"


def _scoped_token_provider(scope: str, token: str) -> Callable[[], str]:
    """A token provider for `scope`: on behalf of the signed-in user (OBO) normally, or
    directly through the user's own Azure sign-in (app.auth.get_token, the same shared
    interactive credential the CLI uses) when there is no Entra API app registration yet
    (`_dev_bypass_enabled`). This is a stand-in only: once ENTRA_API_CLIENT_ID/_SECRET are
    set, OBO takes over automatically and every call acts as the signed-in user properly."""
    if _dev_bypass_enabled():
        return lambda: _personal_token(scope)
    return obo_token_provider(get_msal_app(), token, scope)


def get_llm() -> LlmCall:
    return default_llm()


def get_relationship_llm() -> RelationshipLlm:
    return default_relationship_llm()


def get_source_llm() -> SourceLlm:
    return default_source_llm()


def get_feasibility_llm() -> SourceLlm:
    return default_source_llm()


def get_dax_llm() -> DaxLlm:
    return default_dax_llm()


def get_report_llm() -> ReportLlm:
    return default_report_llm()


def get_report_services(token: str = Depends(user_token)) -> ReportServices:
    """What regenerating part of a report needs: the model, and sandbox and Power BI clients acting as
    the signed-in user, built only when a regeneration asks for them."""
    return ReportServices(
        llm=default_report_llm(),
        items=lambda: FabricItemsClient(token_provider=_scoped_token_provider(FABRIC_SCOPE, token)),
        pbi=lambda: PowerBiClient(token_provider=_scoped_token_provider(POWERBI_SCOPE, token)),
    )


def get_promotion_items_client(token: str = Depends(user_token)) -> FabricItemsClient:
    """The one Fabric items client that may leave the sandbox guard: only the promotion service (T-702)
    uses it, and only after Gate G6 has passed. It acts as the signed-in user (OBO), or as the user's
    own Azure sign-in when there is no Entra app registration yet."""
    return FabricItemsClient(token_provider=_scoped_token_provider(FABRIC_SCOPE, token), allow_any_workspace=True)


def get_workspace_lister(token: str = Depends(user_token)):
    """What run authorization asks Fabric (T-705): the workspaces the signed-in user can access, read as the
    user (OBO, or their own Azure sign-in without an Entra app registration). Built lazily, so a run its
    creator opens never calls Fabric."""

    def list_workspaces() -> list[dict]:
        client = FabricApiClient(token_provider=_scoped_token_provider(FABRIC_SCOPE, token))
        return client.list_workspaces()

    return list_workspaces


def get_bridge_llm() -> BridgeLlm:
    return default_bridge_llm()


def get_recon_llm() -> ReconLlm:
    return default_recon_llm()


def get_items_client(token: str = Depends(user_token)) -> FabricItemsClient:
    """A Fabric items client (sandbox only) that deploys as the signed-in user (OBO, or
    their own Azure sign-in without an Entra app registration)."""
    return FabricItemsClient(token_provider=_scoped_token_provider(FABRIC_SCOPE, token))


def get_powerbi_client(token: str = Depends(user_token)) -> PowerBiClient:
    """A Power BI client that refreshes and queries as the signed-in user (OBO, or their
    own Azure sign-in without an Entra app registration)."""
    return PowerBiClient(token_provider=_scoped_token_provider(POWERBI_SCOPE, token))


def get_fabric_client(token: str = Depends(user_token)) -> FabricApiClient:
    """A Fabric client that calls Fabric as the signed-in user (OBO, or their own Azure
    sign-in without an Entra app registration)."""
    return FabricApiClient(token_provider=_scoped_token_provider(FABRIC_SCOPE, token))


def get_connector_factory(token: str = Depends(user_token)):
    """Builds Lakehouse connectors that read OneLake as the signed-in user."""
    provider = _scoped_token_provider(STORAGE_SCOPE, token)
    return lambda scope: FabricLakehouseConnector.for_scope(
        scope, token_provider=provider
    )


def get_sql_connector_factory(token: str = Depends(user_token)):
    """Builds SQL endpoint connectors that query as the signed-in user."""
    provider = _scoped_token_provider(SQL_SCOPE, token)
    return lambda scope: FabricSQLConnector.for_scope(scope, token_provider=provider)


def get_lakehouse_connector_factory(token: str = Depends(user_token)):
    """Builds a Lakehouse connector for an arbitrary workspace/Lakehouse (not yet a bound
    run scope) -- for the New Run form's schema/table pickers, which read OneLake before a
    scope exists. Same display-only trust level as the Workspace/Lakehouse pickers; the job
    re-verifies everything when the scope is actually bound (bind_scope)."""
    provider = _scoped_token_provider(STORAGE_SCOPE, token)
    return lambda workspace_id, lakehouse_id, lakehouse_name: FabricLakehouseConnector(
        workspace_id, lakehouse_id, lakehouse_name, token_provider=provider
    )
