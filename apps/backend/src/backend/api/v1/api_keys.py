from api_requests.api_keys import CreateApiKeyRequest, UpdateApiKeyRequest
from fastapi import APIRouter, Depends, HTTPException

from backend.api.security import get_current_active_user
from backend.database import Database, get_db
from backend.database.models import ApiKeyPydantic, UserPydantic
from backend.database.utils import (
    generate_api_key,
    generate_api_key_expires_at,
)

api_keys_router = APIRouter(prefix="/api-keys", tags=["api-keys"])


@api_keys_router.get("")
async def get_api_keys(
    current_user: UserPydantic = Depends(get_current_active_user),
    db: Database = Depends(get_db),
) -> list[ApiKeyPydantic]:
    """Get all API keys for the current user."""
    return await db.api_keys.find(filters={"user_id": current_user.id})


@api_keys_router.post("")
async def create_api_key(
    request: CreateApiKeyRequest,
    current_user: UserPydantic = Depends(get_current_active_user),
    db: Database = Depends(get_db),
) -> ApiKeyPydantic:
    """Create a new API key for the current user."""
    # Check if an API key with this name already exists for the user
    existing_key = await db.api_keys.find_one(
        filters={"user_id": current_user.id, "name": request.name}
    )
    if existing_key:
        raise HTTPException(
            status_code=409,
            detail=f"API key with name '{request.name}' already exists. Please use a different name.",
        )

    api_key = ApiKeyPydantic(
        name=request.name,
        user_id=current_user.id,
        value=generate_api_key(),
        expires_at=generate_api_key_expires_at(request.expires_at),
    )

    new_api_key = await db.api_keys.create(api_key)
    if new_api_key is None:
        raise HTTPException(status_code=500, detail="Unable to create api key")

    return new_api_key


@api_keys_router.put("/{api_key_id}")
async def update_api_key(
    api_key_id: str,
    request: UpdateApiKeyRequest,
    current_user: UserPydantic = Depends(get_current_active_user),
    db: Database = Depends(get_db),
) -> ApiKeyPydantic:
    """Regenerate an API key for the current user."""
    api_key = await db.api_keys.find_one(
        filters={"id": api_key_id, "user_id": current_user.id}
    )
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")

    # Regenerate the API key value
    api_key.value = generate_api_key()
    api_key.expires_at = generate_api_key_expires_at(request.expires_at)

    new_api_key = await db.api_keys.update(api_key)
    if new_api_key is None:
        raise HTTPException(status_code=500, detail="Unable to update api key")

    return new_api_key


@api_keys_router.delete("/{api_key_id}")
async def delete_api_key(
    api_key_id: str,
    current_user: UserPydantic = Depends(get_current_active_user),
    db: Database = Depends(get_db),
) -> ApiKeyPydantic:
    """Delete an API key for the current user."""
    api_key = await db.api_keys.find_one(
        filters={"id": api_key_id, "user_id": current_user.id}
    )
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")

    await db.api_keys.delete(api_key_id)
    return api_key
