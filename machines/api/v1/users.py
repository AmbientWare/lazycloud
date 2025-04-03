from fastapi import APIRouter, Depends

from machines.api.utils import get_user_id

users_router = APIRouter(prefix="/users")


@users_router.get("/id")
async def current_user(user_id: str = Depends(get_user_id)):
    return user_id
