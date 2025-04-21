from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Dict, Any

from machines.api.security import (
    require_admin,
    check_user_id_request,
    get_current_active_user,
)
from machines.database import db
from machines.database.ssh_keys import SshKeyPydantic
from machines.api.security import UserData

ssh_keys_router = APIRouter(prefix="/ssh-keys", tags=["ssh-keys"])


# NOTE: Api keys access is limited to admins. This allows us to keep track of authenticated users without a users db table.


@ssh_keys_router.get("")
async def get_ssh_keys(
    user_id: Optional[str] = None,
    current_user: UserData = Depends(get_current_active_user),
) -> List[SshKeyPydantic]:
    user_id = await check_user_id_request(user_id, current_user)
    ssh_keys = await db.ssh_keys.afind(filters={"user_id": user_id})

    return ssh_keys


class CreateSshKeyRequest(BaseModel):
    user_id: Optional[str] = None
    name: str
    public_key: str


@ssh_keys_router.post("")
async def create_ssh_key(
    request: CreateSshKeyRequest,
    current_user: UserData = Depends(get_current_active_user),
) -> SshKeyPydantic:
    # create a new api key that expires at the requested time
    user_id = await check_user_id_request(request.user_id, current_user)

    ssh_key = SshKeyPydantic(
        name=request.name,
        user_id=user_id,
        public_key=request.public_key,
    )
    new_ssh_key = await db.ssh_keys.acreate(ssh_key)
    if new_ssh_key is None:
        raise HTTPException(status_code=404, detail="Unable to create ssh key")

    return new_ssh_key


class DeleteSshKeyRequest(BaseModel):
    ssh_key_id: Optional[int] = None
    user_id: Optional[str] = None


@ssh_keys_router.delete("")
async def delete_ssh_keys(
    request: DeleteSshKeyRequest,
    current_user: UserData = Depends(get_current_active_user),
) -> List[SshKeyPydantic]:
    user_id = await check_user_id_request(request.user_id, current_user)

    filters: Dict[str, Any] = {"user_id": user_id}
    if request.ssh_key_id is not None:
        filters["id"] = request.ssh_key_id

    ssh_keys = await db.ssh_keys.afind(filters=filters)

    for ssh_key in ssh_keys:
        if ssh_key.id is not None:
            await db.ssh_keys.adelete(ssh_key.id)

    return ssh_keys
