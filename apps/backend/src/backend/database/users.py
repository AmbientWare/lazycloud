from enum import StrEnum
from typing import TYPE_CHECKING, List

from sqlalchemy import Enum, String
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database.base import BaseDbPydanticModel, BaseTable, DatabaseService

if TYPE_CHECKING:
    from backend.database.api_keys import ApiKeyTable
    from backend.database.user_workspaces import UserWorkspaceTable


class UserStatus(StrEnum):
    """Status of usage"""

    ACTIVE = "active"
    INACTIVE = "inactive"
    DELETED = "deleted"


class SubscriptionState(StrEnum):
    """Subscription/billing state"""

    WITHIN_LIMITS = "within_limits"
    OVER_LIMITS = "over_limits"
    PAYMENT_FAILED = "payment_failed"
    TRIAL_EXPIRED = "trial_expired"
    SUSPENDED = "suspended"


class UserRole(StrEnum):
    ADMIN = "admin"
    USER = "user"


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


class UserPydantic(BaseDbPydanticModel):
    """Pydantic model for a user account"""

    name: str
    email: str
    workos_id: str
    role: UserRole
    status: UserStatus
    subscription_state: SubscriptionState


class UserService(DatabaseService[UserTable, UserPydantic]):
    """Service layer for user operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(UserTable, UserPydantic, session)

    async def get_by_workos_id(self, workos_id: str) -> UserPydantic | None:
        """Get user by WorkOS ID"""
        filters = {"workos_id": workos_id}
        return await self.find_one(filters=filters)

    async def get_by_email(self, email: str) -> UserPydantic | None:
        """Get user by email"""
        filters = {"email": email.lower().strip()}
        return await self.find_one(filters=filters)
