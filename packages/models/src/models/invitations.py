from enum import StrEnum


class InvitationType(StrEnum):
    """Type of workspace invitation"""

    MEMBER = "member"
    OWNERSHIP_TRANSFER = "ownership_transfer"
