import uuid
from datetime import datetime
from enum import IntEnum
from typing import TYPE_CHECKING

from sqlalchemy import UUID, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.future import select
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lazycloud_api.database.base import (
    BaseDbPydanticModel,
    BaseTable,
    DatabaseService,
    UUIDStr,
)

if TYPE_CHECKING:
    from lazycloud_api.database.users import UserTable


class ApiKeyExpirationMinutes(IntEnum):
    """The expiration time for a api key in minutes"""

    NEVER = 0
    THIRTY_MINUTES = 30
    ONE_HOUR = 60
    THREE_HOURS = 180


class ApiKeyExpirationDays(IntEnum):
    """The expiration time for a api key in days"""

    NEVER = 0
    ONE_DAY = 1
    THIRTY_DAYS = 30
    ONE_HUNDRED_DAYS = 100
    THREE_HUNDRED_SIXTY_FIVE_DAYS = 365


class ApiKeyTable(BaseTable):
    """SQLAlchemy model for a api key"""

    __tablename__ = "api_keys"

    name: Mapped[str] = mapped_column(String, index=True)
    value: Mapped[str] = mapped_column(String, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # Relationships
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    user: Mapped["UserTable"] = relationship(
        "UserTable",
        back_populates="api_keys",
        lazy="joined",
    )

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_user_api_key_name"),
        Index("ix_api_keys_user_id_name", "user_id", "name"),
    )


class ApiKeyPydantic(BaseDbPydanticModel):
    """Pydantic model for a api key"""

    name: str
    user_id: UUIDStr
    value: str
    expires_at: datetime


class ApiKeyService(DatabaseService[ApiKeyTable, ApiKeyPydantic]):
    """Service layer for api key operations"""

    def __init__(self):
        super().__init__(ApiKeyTable, ApiKeyPydantic)

    async def aget_by_user_id(self, user_id: str) -> list[ApiKeyPydantic]:
        """Get a api key by reference id"""
        async with self._session_manager.get_session() as session:
            query = select(ApiKeyTable).where(ApiKeyTable.user_id == user_id)
            result = await session.execute(query)
            api_keys = result.scalars().all()
            return [self._to_pydantic(api_key) for api_key in api_keys]

    async def auser_by_value(self, value: str) -> str | None:
        """Get a user by value"""
        async with self._session_manager.get_session() as session:
            query = select(ApiKeyTable).where(ApiKeyTable.value == value)
            result = await session.execute(query)
            api_key = result.scalar_one_or_none()
            api_key_pydantic = self._to_pydantic(api_key)
            if api_key_pydantic is not None:
                return api_key_pydantic.user_id

            return None
