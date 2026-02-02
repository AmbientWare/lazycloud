from typing import TYPE_CHECKING, List

from sqlalchemy import Enum, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database.models import SubscriptionState, UserRole, UserStatus
from backend.database.tables.base import BaseTable

if TYPE_CHECKING:
    from backend.database.tables.api_keys import ApiKeyTable
    from backend.database.tables.user_workspaces import UserWorkspaceTable


class UserTable(BaseTable):
    """SQLAlchemy model for a user account"""

    __tablename__ = "users"

    # WorkOS user ID
    name: Mapped[str] = mapped_column(String)
    email: Mapped[str] = mapped_column(String)
    workos_id: Mapped[str] = mapped_column(String, unique=True)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole), default=UserRole.USER, index=True
    )
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus), default=UserStatus.ACTIVE, index=True
    )
    subscription_state: Mapped[SubscriptionState] = mapped_column(
        Enum(SubscriptionState), default=SubscriptionState.WITHIN_LIMITS, index=True
    )

    # Relationships
    api_keys: Mapped[List["ApiKeyTable"]] = relationship(
        "ApiKeyTable",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    # Relationships - using Association Object pattern (SQLAlchemy 2.0 best practice)
    user_workspaces: Mapped[List["UserWorkspaceTable"]] = relationship(
        "UserWorkspaceTable",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
