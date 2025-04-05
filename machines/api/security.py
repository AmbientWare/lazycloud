from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from machines.database import db
from machines.database.api_keys import ApiKeyRole
from machines.database.utils import api_key_is_expired
from machines.config import app_config, ENVIRONMENT

security = HTTPBearer()


class UserData(BaseModel):
    user_id: str
    role: ApiKeyRole


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> UserData:
    if app_config.ENV.value == ENVIRONMENT.DEV.value:
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


async def require_admin(current_user: UserData = Depends(get_current_user)) -> UserData:
    if current_user.role != ApiKeyRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions"
        )
    return current_user
