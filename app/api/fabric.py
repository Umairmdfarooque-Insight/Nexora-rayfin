"""Fabric discovery endpoints: what the caller's identity can see."""

from fastapi import APIRouter, Depends

from app.api.deps import get_fabric_client, get_lakehouse_connector_factory
from app.api.schemas import LakehouseItem, WorkspaceItem
from app.api.security import current_user
from app.connectors.fabric_api import FabricApiClient

router = APIRouter(
    prefix="/fabric", tags=["fabric"], dependencies=[Depends(current_user)]
)


@router.get("/workspaces", response_model=list[WorkspaceItem])
def list_workspaces(fabric: FabricApiClient = Depends(get_fabric_client)):
    return fabric.list_workspaces()


@router.get(
    "/workspaces/{workspace_id}/lakehouses", response_model=list[LakehouseItem]
)
def list_lakehouses(
    workspace_id: str, fabric: FabricApiClient = Depends(get_fabric_client)
):
    return fabric.list_lakehouses(workspace_id)


@router.get(
    "/workspaces/{workspace_id}/lakehouses/{lakehouse_id}/schemas",
    response_model=list[str],
)
def list_schemas(
    workspace_id: str,
    lakehouse_id: str,
    lakehouse_name: str,
    connector_factory=Depends(get_lakehouse_connector_factory),
):
    connector = connector_factory(workspace_id, lakehouse_id, lakehouse_name)
    return sorted((s["name"] for s in connector.list_schemas()), key=str.casefold)


@router.get(
    "/workspaces/{workspace_id}/lakehouses/{lakehouse_id}/schemas/{schema_name}/tables",
    response_model=list[str],
)
def list_tables(
    workspace_id: str,
    lakehouse_id: str,
    schema_name: str,
    lakehouse_name: str,
    connector_factory=Depends(get_lakehouse_connector_factory),
):
    connector = connector_factory(workspace_id, lakehouse_id, lakehouse_name)
    return sorted((t["name"] for t in connector.list_tables(schema_name)), key=str.casefold)
