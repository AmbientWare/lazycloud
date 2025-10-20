import uuid

from pydantic import BaseModel


class WorkspaceResponse(BaseModel):
    id: uuid.UUID
    name: str
    is_personal: bool
    role: str  # owner, admin, member, viewer


class WorkspaceMemberResponse(BaseModel):
    user_id: uuid.UUID
    role: str
    status: str


class WorkspaceSuccessResponse(BaseModel):
    success: bool
