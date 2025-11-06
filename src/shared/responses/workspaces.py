import uuid

from pydantic import BaseModel

from shared.responses.deployments import DeploymentOverview


class WorkspaceResponse(BaseModel):
    id: uuid.UUID
    name: str
    is_personal: bool
    role: str  # owner, admin, member, viewer


class WorkspaceMemberResponse(BaseModel):
    user_id: str
    name: str
    email: str
    role: str
    status: str


class WorkspaceSuccessResponse(BaseModel):
    success: bool


class WorkspaceWithDeploymentsResponse(BaseModel):
    """Workspace with deployment overviews."""

    id: uuid.UUID
    name: str
    is_personal: bool
    role: str
    deployments: list[DeploymentOverview]
