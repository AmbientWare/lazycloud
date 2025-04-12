from fastapi import APIRouter, Depends
from machines.api.security import get_current_active_user, UserData
from machines.services import fly_app_manager

users_router = APIRouter(prefix="/users", tags=["users"])


@users_router.get("/id")
async def current_user(
    current_user: UserData = Depends(get_current_active_user),
) -> str:
    return current_user.user_id


@users_router.get("/machine-user")
async def machine_user(
    current_user: UserData = Depends(get_current_active_user),
) -> str:
    machine_user = await fly_app_manager.get_user_id(current_user.user_id)
    return machine_user
