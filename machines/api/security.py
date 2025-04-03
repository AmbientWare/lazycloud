from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from machines.database import db
from machines.database.tokens import TokenRole
from machines.database.utils import token_is_expired
from machines.config import app_config, ENVIRONMENT

security = HTTPBearer()


class UserData(BaseModel):
    user_id: str
    role: TokenRole


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> UserData:
    if app_config.ENV.value == ENVIRONMENT.DEV.value:
        return UserData(user_id="admin", role=TokenRole.ADMIN)

    token = credentials.credentials
    db_token = await db.tokens.afind_one(filters={"token": token})

    if not db_token or token_is_expired(db_token.expires_at):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return UserData(user_id=db_token.user_id, role=db_token.role)


async def get_current_active_user(
    current_user: UserData = Depends(get_current_user),
) -> UserData:
    return current_user


async def require_admin(current_user: UserData = Depends(get_current_user)) -> UserData:
    if current_user.role != TokenRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions"
        )
    return current_user
