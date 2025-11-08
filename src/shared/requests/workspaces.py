from pydantic import BaseModel

from lazycloud_api.database.user_workspaces import WorkspaceRole


class CreateWorkspaceRequest(BaseModel):
    name: str


class InviteUserRequest(BaseModel):
    email: str
    role: WorkspaceRole = WorkspaceRole.MEMBER


class UpdateMemberRoleRequest(BaseModel):
    role: WorkspaceRole
    user_id: str
