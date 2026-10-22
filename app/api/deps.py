"""Shared FastAPI dependencies."""

from functools import lru_cache
from pathlib import Path

from fastapi import Depends

from app.api.obo import get_msal_app, obo_token_provider
from app.api.security import user_token
from app.auth import FABRIC_SCOPE, POWERBI_SCOPE, SQL_SCOPE, STORAGE_SCOPE
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
        items=lambda: FabricItemsClient(token_provider=obo_token_provider(get_msal_app(), token, FABRIC_SCOPE)),
        pbi=lambda: PowerBiClient(token_provider=obo_token_provider(get_msal_app(), token, POWERBI_SCOPE)),
    )


def get_promotion_items_client(
    token: str = Depends(user_token), msal_app=Depends(get_msal_app)
) -> FabricItemsClient:
    """The one Fabric items client that may leave the sandbox guard: only the promotion service (T-702)
    uses it, and only after Gate G6 has passed. It acts as the signed-in user (OBO)."""
    return FabricItemsClient(token_provider=obo_token_provider(msal_app, token, FABRIC_SCOPE), allow_any_workspace=True)


def get_workspace_lister(token: str = Depends(user_token)):
    """What run authorization asks Fabric (T-705): the workspaces the signed-in user can access, read as the
    user (OBO). Built lazily, so a run its creator opens never calls Fabric."""

    def list_workspaces() -> list[dict]:
        client = FabricApiClient(token_provider=obo_token_provider(get_msal_app(), token, FABRIC_SCOPE))
        return client.list_workspaces()

    return list_workspaces


def get_bridge_llm() -> BridgeLlm:
    return default_bridge_llm()


def get_recon_llm() -> ReconLlm:
    return default_recon_llm()


def get_items_client(
    token: str = Depends(user_token), msal_app=Depends(get_msal_app)
) -> FabricItemsClient:
    """A Fabric items client (sandbox only) that deploys as the signed-in user (OBO)."""
    return FabricItemsClient(token_provider=obo_token_provider(msal_app, token, FABRIC_SCOPE))


def get_powerbi_client(
    token: str = Depends(user_token), msal_app=Depends(get_msal_app)
) -> PowerBiClient:
    """A Power BI client that refreshes and queries as the signed-in user (OBO)."""
    return PowerBiClient(token_provider=obo_token_provider(msal_app, token, POWERBI_SCOPE))


def get_fabric_client(
    token: str = Depends(user_token), msal_app=Depends(get_msal_app)
) -> FabricApiClient:
    """A Fabric client that calls Fabric as the signed-in user (OBO)."""
    return FabricApiClient(
        token_provider=obo_token_provider(msal_app, token, FABRIC_SCOPE)
    )


def get_connector_factory(
    token: str = Depends(user_token), msal_app=Depends(get_msal_app)
):
    """Builds Lakehouse connectors that read OneLake as the signed-in user."""
    provider = obo_token_provider(msal_app, token, STORAGE_SCOPE)
    return lambda scope: FabricLakehouseConnector.for_scope(
        scope, token_provider=provider
    )


def get_sql_connector_factory(
    token: str = Depends(user_token), msal_app=Depends(get_msal_app)
):
    """Builds SQL endpoint connectors that query as the signed-in user."""
    provider = obo_token_provider(msal_app, token, SQL_SCOPE)
    return lambda scope: FabricSQLConnector.for_scope(scope, token_provider=provider)
