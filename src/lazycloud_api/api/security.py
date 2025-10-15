from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from lazycloud_api.config import ENVIRONMENT, app_config
from lazycloud_api.database import db
from lazycloud_api.database.api_keys import ApiKeyRole
from lazycloud_api.database.utils import api_key_is_expired
from shared.models.users import UserData

security = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> UserData:
    if app_config.ENV.value == ENVIRONMENT.DEV.value and not credentials.credentials:
        return UserData(user_id="admin", role=ApiKeyRole.ADMIN)

    api_key = credentials.credentials
    db_api_key = await db.api_keys.afind_one(filters={"value": api_key})

    if not db_api_key or api_key_is_expired(db_api_key.expires_at):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return UserData(user_id=db_api_key.user_id, role=db_api_key.role)


async def get_current_active_user(
    current_user: UserData = Depends(get_current_user),
) -> UserData:
    return current_user


async def get_user_usage_uuid(
    current_user: UserData = Depends(get_current_user),
) -> str:
    usage = await db.usage.afind_one(filters={"user_id": current_user.user_id})
    if not usage or usage.uuid is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Usage not found"
        )

    return usage.uuid


async def require_admin(current_user: UserData = Depends(get_current_user)) -> UserData:
    if current_user.role != ApiKeyRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions"
        )
    return current_user


async def check_user_id_request(user_id: str | None, current_user: UserData) -> str:
    if user_id:
        if not await require_admin(current_user):
            # only admins can delete file systems for other users
            raise HTTPException(
                status_code=403,
                detail="You are not authorized to delete file systems for other users",
            )

        return user_id

    return current_user.user_id
