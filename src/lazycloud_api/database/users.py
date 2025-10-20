from enum import StrEnum
from typing import TYPE_CHECKING, List

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lazycloud_api.database.base import BaseDbPydanticModel, BaseTable, DatabaseService

if TYPE_CHECKING:
    from lazycloud_api.database.api_keys import ApiKeyTable
    from lazycloud_api.database.user_workspaces import UserWorkspaceTable


class UserStatus(StrEnum):
    """Status of usage"""

    ACTIVE = "active"
    INACTIVE = "inactive"
    DELETED = "deleted"


class UserRole(StrEnum):
    ADMIN = "admin"
    USER = "user"


class UserTable(BaseTable):
    """SQLAlchemy model for a user account"""

    __tablename__ = "users"

    # Clerk user ID
    clerk_id: Mapped[str] = mapped_column(String, unique=True)
    role: Mapped[str] = mapped_column(String, default=UserRole.USER)
    status: Mapped[str] = mapped_column(String, default=UserStatus.ACTIVE)

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

    clerk_id: str
    role: UserRole
    status: UserStatus


class UserService(DatabaseService[UserTable, UserPydantic]):
    """Service layer for user operations"""

    def __init__(self):
        super().__init__(UserTable, UserPydantic)

    async def aget_by_clerk_id(self, clerk_id: str) -> UserPydantic | None:
        """Get user by Clerk ID"""
        filters = {"clerk_id": clerk_id}
        return await self.afind_one(filters=filters)
