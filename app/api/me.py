"""The signed-in user and their roles, so the UI can hide actions the user can't take (T-205)."""

from fastapi import APIRouter, Depends

from app.api.schemas import MeResponse
from app.api.security import Principal, current_user
from app.workflow.roles import load_roles

router = APIRouter(tags=["me"])


@router.get("/me", response_model=MeResponse)
def me(user: Principal = Depends(current_user)):
    return MeResponse(
        oid=user.oid, upn=user.upn, name=user.name, roles=load_roles().roles_of(user.upn)
    )
