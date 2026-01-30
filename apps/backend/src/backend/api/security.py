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
from backend.services import get_polar_service, get_subscription_service
from backend.services.exceptions import NoActiveSubscriptionError

security = HTTPBearer(auto_error=False)

# Cache the JWKS client to avoid repeated fetches
# lifespan controls how long keys are cached before being refreshed (in seconds)
_jwks_client: PyJWKClient | None = None
_JWKS_CACHE_LIFESPAN = 3600  # Refresh keys every hour


def get_jwks_client(force_refresh: bool = False) -> PyJWKClient:
    """Get or create the JWKS client for WorkOS token validation.

    The JWKS URL must include the client ID:
    https://api.workos.com/sso/jwks/<clientId>

    Args:
        force_refresh: If True, recreate the client to force a fresh key fetch
    """
    global _jwks_client
    if _jwks_client is None or force_refresh:
        jwks_url = f"https://api.workos.com/sso/jwks/{app_config.WORKOS_CLIENT_ID}"
        _jwks_client = PyJWKClient(
            jwks_url, cache_keys=True, lifespan=_JWKS_CACHE_LIFESPAN
        )
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
    # Try validation, and if it fails due to key issues, retry with refreshed keys
    for attempt in range(2):
        try:
            # Get the signing key from WorkOS JWKS
            # On retry (attempt 1), force refresh to handle key rotation
            jwks_client = get_jwks_client(force_refresh=(attempt > 0))
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
            # If this is the first attempt, retry with refreshed keys
            if attempt == 0:
                continue
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication service unavailable",
                headers={"WWW-Authenticate": "Bearer"},
            )
        except jwt.InvalidTokenError as e:
            # Key mismatch errors should trigger a retry with fresh keys
            if attempt == 0 and "kid" in str(e).lower():
                continue
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
        except jwt.ExpiredSignatureError:
            # Expired tokens should not retry - the token itself is expired
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has expired",
                headers={"WWW-Authenticate": "Bearer"},
            )

    # Should not reach here, but handle gracefully
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication failed",
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


async def get_current_active_user_with_sub(
    current_user: UserPydantic = Depends(get_current_active_user),
) -> UserPydantic:
    """Require authenticated active user with an active subscription.

    Use this for user-level actions that require billing (e.g., workspace creation).
    Admins bypass the subscription check.
    """
    # Admin users bypass subscription requirement
    if current_user.role == UserRole.ADMIN:
        return current_user

    # Check if billing is enabled
    polar_service = get_polar_service()
    if not polar_service.enabled:
        return current_user

    subscription_service = get_subscription_service()
    try:
        await subscription_service.get_user_features(current_user.workos_id)

    except NoActiveSubscriptionError:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Payment method required. Please add a payment method to continue.",
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
