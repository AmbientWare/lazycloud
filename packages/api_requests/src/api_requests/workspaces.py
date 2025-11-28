from models.workspaces import WorkspaceRole
from pydantic import BaseModel


class CreateWorkspaceRequest(BaseModel):
    name: str


class InviteUserRequest(BaseModel):
    email: str
    role: WorkspaceRole = WorkspaceRole.MEMBER
    acceptance_url: str | None = None


class UpdateMemberRoleRequest(BaseModel):
    role: WorkspaceRole
    user_id: str


class TransferOwnershipRequest(BaseModel):
    new_owner_user_id: str
    acceptance_url: str | None = None
