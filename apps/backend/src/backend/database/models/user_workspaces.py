from enum import StrEnum

from pydantic import BaseModel

from backend.database.models.base import BaseDbModel, UUIDStr


class WorkspaceRole(StrEnum):
    """Role of a user in a workspace"""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class UserWorkspaceStatus(StrEnum):
    """Status of user membership in a workspace"""

    ACTIVE = "active"
    INVITED = "invited"
    SUSPENDED = "suspended"


class UserWorkspace(BaseModel):
    """Pydantic model for user-workspace membership"""

    user_id: UUIDStr
    workspace_id: UUIDStr
    role: WorkspaceRole
    status: UserWorkspaceStatus


class UserWorkspaceInDb(UserWorkspace, BaseDbModel):
    """Pydantic model for a user-workspace membership that is stored in the database"""

    ...
