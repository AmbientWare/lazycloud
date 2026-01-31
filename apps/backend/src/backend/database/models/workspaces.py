from datetime import datetime
from enum import StrEnum

from backend.database.models.base import BaseDbModel


class WorkspaceStatus(StrEnum):
    """Status of a workspace"""

    ACTIVE = "active"
    INACTIVE = "inactive"
    DELETED = "deleted"


class Workspace(BaseDbModel):
    """Pydantic model for a workspace"""

    name: str
    is_personal: bool = False
    status: WorkspaceStatus = WorkspaceStatus.ACTIVE
    deleted_at: datetime | None = None
