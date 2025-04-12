from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Optional

from machines.api.security import require_admin
from machines.database import db
from machines.database.ssh_keys import SshKeyPydantic
from machines.api.security import get_current_active_user, require_admin
from machines.api.security import UserData

ssh_keys_router = APIRouter(prefix="/ssh-keys", tags=["ssh-keys"])


# NOTE: Api keys access is limited to admins. This allows us to keep track of authenticated users without a users db table.


@ssh_keys_router.get("")
async def get_ssh_keys(
    user_id: Optional[str] = None,
    current_user: UserData = Depends(get_current_active_user),
) -> List[SshKeyPydantic]:
    # check if user is admin
    if user_id:
        if not await require_admin(current_user):
            # only admins can access other users' ssh keys
            raise HTTPException(
                status_code=403, detail="You are not authorized to access this resource"
            )
    else:
        user_id = current_user.user_id

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
    if request.user_id:
        if not await require_admin(current_user):
            # only admins can create ssh keys for other users
            raise HTTPException(
                status_code=403,
                detail="You are not authorized to create ssh keys for other users",
            )
        user_id = request.user_id

    else:
        user_id = current_user.user_id

    ssh_key = SshKeyPydantic(
        name=request.name,
        user_id=user_id,
        public_key=request.public_key,
    )
    new_ssh_key = await db.ssh_keys.acreate(ssh_key)
    if new_ssh_key is None:
        raise HTTPException(status_code=404, detail="Unable to create ssh key")

    return new_ssh_key


@ssh_keys_router.delete("")
async def delete_ssh_keys(
    ssh_key_id: Optional[int] = None,
    user_id: Optional[str] = None,
    current_user: UserData = Depends(get_current_active_user),
) -> List[SshKeyPydantic]:
    if user_id:
        if not await require_admin(current_user):
            # only admins can delete ssh keys for other users
            raise HTTPException(
                status_code=403,
                detail="You are not authorized to delete ssh keys for other users",
            )

    else:
        user_id = current_user.user_id

    filters = {}
    if ssh_key_id:
        filters["id"] = ssh_key_id
    if user_id:
        filters["user_id"] = user_id

    ssh_keys = await db.ssh_keys.afind(filters=filters)

    for ssh_key in ssh_keys:
        if ssh_key.id is not None:
            await db.ssh_keys.adelete(ssh_key.id)

    return ssh_keys
