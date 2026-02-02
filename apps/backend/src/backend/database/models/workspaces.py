from datetime import datetime

from models.workspaces import WorkspaceStatus
from pydantic import BaseModel

from backend.database.models.base import BaseDbModel


class Workspace(BaseModel):
    """Pydantic model for a workspace"""

    name: str
    is_personal: bool = False
    status: WorkspaceStatus = WorkspaceStatus.ACTIVE
    deleted_at: datetime | None = None


class WorkspaceInDb(Workspace, BaseDbModel):
    """Pydantic model for a workspace that is stored in the database"""

    ...
