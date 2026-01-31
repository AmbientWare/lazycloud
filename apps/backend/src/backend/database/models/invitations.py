from datetime import datetime
from enum import StrEnum

from backend.database.models.base import (
    BaseDbModel,
    UUIDStr,
)
from backend.database.models.user_workspaces import WorkspaceRole


class InvitationType(StrEnum):
    """Type of workspace invitation"""

    MEMBER = "member"
    OWNERSHIP_TRANSFER = "ownership_transfer"


class WorkspaceInvitation(BaseDbModel):
    """Pydantic model for workspace invitation"""

    workspace_id: UUIDStr
    email: str
    role: WorkspaceRole
    token: str
    invited_by_user_id: UUIDStr
    expires_at: datetime
    accepted_at: datetime | None = None
    invitation_type: str = InvitationType.MEMBER.value
