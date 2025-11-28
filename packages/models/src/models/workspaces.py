from enum import StrEnum


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


class InvitationType(StrEnum):
    """Type of workspace invitation"""

    MEMBER = "member"
    OWNERSHIP_TRANSFER = "ownership_transfer"
