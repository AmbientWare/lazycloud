from models.user_workspaces import UserWorkspaceStatus, WorkspaceRole
from pydantic import BaseModel

from backend.database.models.base import BaseDbModel, UUIDStr


class UserWorkspace(BaseModel):
    """Pydantic model for user-workspace membership"""

    user_id: UUIDStr
    workspace_id: UUIDStr
    role: WorkspaceRole
    status: UserWorkspaceStatus


class UserWorkspaceInDb(UserWorkspace, BaseDbModel):
    """Pydantic model for a user-workspace membership that is stored in the database"""

    ...
