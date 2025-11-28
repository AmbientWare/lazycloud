import uuid

from pydantic import BaseModel
from responses.deployments import DeploymentOverview


class WorkspaceResponse(BaseModel):
    id: uuid.UUID
    name: str
    is_personal: bool
    role: str  # owner, admin, member, viewer


class WorkspaceMemberResponse(BaseModel):
    user_id: str | None = None
    name: str | None = None
    email: str
    role: str
    status: str
    invitation_id: str | None = None


class WorkspaceSuccessResponse(BaseModel):
    success: bool


class InviteUserResponse(BaseModel):
    success: bool
    token: str | None = None


class WorkspaceWithDeploymentsResponse(BaseModel):
    """Workspace with deployment overviews."""

    id: uuid.UUID
    name: str
    is_personal: bool
    role: str
    has_more: bool
    deployments: list[DeploymentOverview]
    cursor: str | None = None
