from datetime import datetime

from models.invitations import InvitationType
from models.user_workspaces import WorkspaceRole
from pydantic import BaseModel

from backend.database.models.base import (
    BaseDbModel,
    UUIDStr,
)


class WorkspaceInvitation(BaseModel):
    """Pydantic model for workspace invitation"""

    workspace_id: UUIDStr
    email: str
    role: WorkspaceRole
    token: str
    invited_by_user_id: UUIDStr
    expires_at: datetime
    accepted_at: datetime | None = None
    invitation_type: str = InvitationType.MEMBER.value


class WorkspaceInvitationInDb(WorkspaceInvitation, BaseDbModel):
    """Pydantic model for a workspace invitation that is stored in the database"""

    ...
