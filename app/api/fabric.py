"""Fabric discovery endpoints: what the caller's identity can see."""

from fastapi import APIRouter, Depends

from app.api.deps import get_fabric_client
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
