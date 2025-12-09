import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from backend.config import ENVIRONMENT, app_config
from backend.database import Database, get_db
from backend.database.users import (
    UserPydantic,
    UserRole,
    UserStatus,
)
from backend.database.utils import api_key_is_expired

security = HTTPBearer(auto_error=False)

# Cache the JWKS client to avoid repeated fetches
_jwks_client: PyJWKClient | None = None


def get_jwks_client() -> PyJWKClient:
    """Get or create the JWKS client for WorkOS token validation.

    The JWKS URL must include the client ID:
    https://api.workos.com/sso/jwks/<clientId>
    """
    global _jwks_client
    if _jwks_client is None:
        jwks_url = f"https://api.workos.com/sso/jwks/{app_config.WORKOS_CLIENT_ID}"
        _jwks_client = PyJWKClient(jwks_url, cache_keys=True)
    return _jwks_client


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Database = Depends(get_db),
) -> UserPydantic:
    # Dev mode bypass
    if app_config.ENV.value == ENVIRONMENT.DEV.value and (
        credentials is None or not credentials.credentials
    ):
        user = await db.users.get_by_workos_id(workos_id="lzy_admin")
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Dev mode: admin user not found",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return user

    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    # Check if token is an API key (starts with sk_) or WorkOS JWT
    if token.startswith("sk_"):
        return await _authenticate_api_key(token, db)
    else:
        return await _authenticate_workos_token(token, db)


async def _authenticate_api_key(api_key: str, db: Database) -> UserPydantic:
    """Authenticate using API key (sk_ prefix)"""
    db_api_key = await db.api_keys.find_one(filters={"value": api_key})

    if not db_api_key or api_key_is_expired(db_api_key.expires_at):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = await db.users.get_by_id(id=db_api_key.user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


async def _authenticate_workos_token(token: str, db: Database) -> UserPydantic:
    """Authenticate using WorkOS access token."""
    try:
        # Get the signing key from WorkOS JWKS
        jwks_client = get_jwks_client()
        signing_key = jwks_client.get_signing_key_from_jwt(token)

        # Decode and validate the token
        # Note: WorkOS tokens may not include 'aud' claim by default,
        # so we don't require audience verification
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            options={
                "verify_exp": True,
                "verify_aud": False,  # WorkOS may not include audience
            },
        )

        # WorkOS access tokens have 'sub' claim with user ID
        workos_user_id = payload.get("sub")
        if not workos_user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: missing subject",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Look up user by WorkOS ID
        user = await db.users.get_by_workos_id(workos_id=workos_user_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return user

    except jwt.exceptions.PyJWKClientError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication service unavailable",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError as e:
        # Sanitize error details in production to avoid leaking implementation info
        if app_config.ENV == ENVIRONMENT.DEV:
            detail = f"Invalid token: {str(e)}"
        else:
            detail = "Invalid token"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )


async def get_current_active_user(
    current_user: UserPydantic = Depends(get_current_user),
) -> UserPydantic:
    if current_user.status != UserStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is not active",
        )
    return current_user


async def require_admin(
    current_user: UserPydantic = Depends(get_current_user),
) -> UserPydantic:
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions"
        )
    return current_user
