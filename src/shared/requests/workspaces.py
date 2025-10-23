from pydantic import BaseModel

from lazycloud_api.database.user_workspaces import WorkspaceRole


class CreateWorkspaceRequest(BaseModel):
    name: str


class RenameWorkspaceRequest(BaseModel):
    name: str


class TransferOwnershipRequest(BaseModel):
    new_owner_user_id: str


class InviteUserRequest(BaseModel):
    user_id: str
    role: WorkspaceRole = WorkspaceRole.MEMBER


class UpdateMemberRoleRequest(BaseModel):
    role: WorkspaceRole
    user_id: str
