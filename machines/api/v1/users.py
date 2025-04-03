from fastapi import APIRouter, Depends
from machines.api.security import get_current_active_user, UserData

users_router = APIRouter(prefix="/users", tags=["users"])


@users_router.get("/id")
async def current_user(
    current_user: UserData = Depends(get_current_active_user),
) -> str:
    return current_user.user_id
