from enum import StrEnum


class WorkspaceStatus(StrEnum):
    """Status of a workspace"""

    ACTIVE = "active"
    INACTIVE = "inactive"
    DELETED = "deleted"
