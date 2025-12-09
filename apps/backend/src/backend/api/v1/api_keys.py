from api_requests.api_keys import (
    CreateApiKeyRequest,
    UpdateApiKeyRequest,
)
from fastapi import APIRouter, Depends, HTTPException

from backend.api.security import require_admin
from backend.database import Database, get_db
from backend.database.api_keys import ApiKeyPydantic
from backend.database.utils import (
    generate_api_key,
    generate_api_key_expires_at,
)

api_keys_router = APIRouter(
    prefix="/api-keys", tags=["api-keys"], dependencies=[Depends(require_admin)]
)


@api_keys_router.get("")
async def get_api_keys(
    user_id: str | None = None,
    db: Database = Depends(get_db),
) -> list[ApiKeyPydantic]:
    # user_id here is the WorkOS ID from the frontend
    if user_id:
        # Look up the user by WorkOS ID to get the internal UUID
        user = await db.users.get_by_workos_id(workos_id=user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        api_keys = await db.api_keys.find(filters={"user_id": user.id})

    else:
        api_keys = await db.api_keys.find(filters={})

    return api_keys


@api_keys_router.post("")
async def create_api_key(
    request: CreateApiKeyRequest,
    db: Database = Depends(get_db),
) -> ApiKeyPydantic:
    # Look up the user by WorkOS ID to get the internal UUID
    user = await db.users.get_by_workos_id(workos_id=request.workos_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Check if an API key with this name already exists for the user
    existing_key = await db.api_keys.find_one(
        filters={"user_id": user.id, "name": request.name}
    )
    if existing_key:
        raise HTTPException(
            status_code=409,
            detail=f"API key with name '{request.name}' already exists. Please use a different name.",
        )

    # Create a new api key that expires at the requested time
    api_key = ApiKeyPydantic(
        name=request.name,
        user_id=user.id,
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
    db: Database = Depends(get_db),
) -> ApiKeyPydantic:
    # Look up the user by WorkOS ID to get the internal UUID
    user = await db.users.get_by_workos_id(workos_id=request.workos_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    api_key = await db.api_keys.find_one(filters={"id": api_key_id, "user_id": user.id})
    if api_key is None:
        raise HTTPException(status_code=404, detail="Api key not found")

    # generate a new api key
    api_key.value = generate_api_key()
    api_key.expires_at = generate_api_key_expires_at(request.expires_at)

    new_api_key = await db.api_keys.update(api_key)
    if new_api_key is None:
        raise HTTPException(status_code=404, detail="Unable to update api key")

    return new_api_key


@api_keys_router.delete("")
async def delete_api_keys(
    api_key_id: str | None = None,
    workos_id: str | None = None,
    db: Database = Depends(get_db),
) -> list[ApiKeyPydantic]:
    """
    If user_id is provided, delete all api keys for the user.
    If api_key_id is provided, delete the api key with the given id.
    If both, filter by both id and user_id to ensure we only delete the correct api key.
    """
    filters = {}
    if api_key_id:
        filters["id"] = api_key_id

    if workos_id:
        user = await db.users.get_by_workos_id(workos_id=workos_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        filters["user_id"] = user.id

    api_keys = await db.api_keys.find(filters=filters)

    for api_key in api_keys:
        if api_key.id is not None:
            await db.api_keys.delete(api_key.id)

    return api_keys
