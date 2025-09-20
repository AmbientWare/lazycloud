from fastapi import APIRouter, Depends, HTTPException

from lazycloud_api.api.security import require_admin
from lazycloud_api.database import db
from lazycloud_api.database.api_keys import (
    ApiKeyPydantic,
    ApiKeyRole,
)
from lazycloud_api.database.utils import (
    generate_api_key,
    generate_api_key_expires_at,
)
from shared.requests.api_keys import (
    CreateApiKeyRequest,
    DeleteApiKeyRequest,
    UpdateApiKeyRequest,
)

api_keys_router = APIRouter(
    prefix="/api-keys", tags=["api-keys"], dependencies=[Depends(require_admin)]
)


# NOTE: Api keys access is limited to admins. This allows us to keep track of authenticated users without a users db table.


@api_keys_router.get("")
async def get_api_keys(
    user_id: str | None = None,
) -> list[ApiKeyPydantic]:
    # check if user is admin
    if user_id:
        api_keys = await db.api_keys.afind(filters={"user_id": user_id})
    else:
        api_keys = await db.api_keys.afind(filters={})

    return api_keys


@api_keys_router.post("")
async def create_api_key(
    request: CreateApiKeyRequest,
) -> ApiKeyPydantic:
    # create a new api key that expires at the requested time
    api_key = ApiKeyPydantic(
        name=request.name,
        user_id=request.user_id,
        value=generate_api_key(),
        expires_at=generate_api_key_expires_at(request.expires_at),
        role=ApiKeyRole.USER,
    )
    new_api_key = await db.api_keys.acreate(api_key)
    if new_api_key is None:
        raise HTTPException(status_code=404, detail="Unable to create api key")

    return new_api_key


@api_keys_router.put("/{api_key_id}")
async def update_api_key(
    api_key_id: int,
    request: UpdateApiKeyRequest,
) -> ApiKeyPydantic:
    api_key = await db.api_keys.afind_one(
        filters={"id": api_key_id, "user_id": request.user_id}
    )
    if api_key is None:
        raise HTTPException(status_code=404, detail="Api key not found")

    # generate a new api key
    api_key.value = generate_api_key()
    api_key.expires_at = generate_api_key_expires_at(request.expires_at)

    new_api_key = await db.api_keys.aupdate(api_key)
    if new_api_key is None:
        raise HTTPException(status_code=404, detail="Unable to update api key")

    return new_api_key


@api_keys_router.delete("")
async def delete_api_keys(
    request: DeleteApiKeyRequest,
    _=Depends(require_admin),
) -> list[ApiKeyPydantic]:
    """
    If user_id is provided, delete all api keys for the user.
    If api_key_id is provided, delete the api key with the given id.
    If both, filter by both id and user_id to ensure we only delete the correct api key.
    """
    filters = {}
    if request.api_key_id:
        filters["id"] = request.api_key_id
    if request.user_id:
        filters["user_id"] = request.user_id

    api_keys = await db.api_keys.afind(filters=filters)

    for api_key in api_keys:
        if api_key.id is not None:
            await db.api_keys.adelete(api_key.id)

    return api_keys
