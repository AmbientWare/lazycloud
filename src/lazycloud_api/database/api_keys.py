from typing import Optional
from datetime import datetime
import asyncio
from sqlalchemy import Column, String, DateTime
from sqlalchemy.future import select
from enum import StrEnum, IntEnum

from lazycloud_api.database.base import BaseModel, BaseTable, DatabaseService


class ApiKeyRole(StrEnum):
    ADMIN = "admin"
    USER = "user"


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

    name = Column(String, nullable=False, unique=True, index=True)
    value = Column(String, nullable=False, unique=True, index=True)
    role = Column(String, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)


class ApiKeyPydantic(BaseModel):
    """Pydantic model for a api key"""

    name: str
    value: str
    expires_at: datetime
    role: ApiKeyRole


class ApiKeyService(DatabaseService[ApiKeyTable, ApiKeyPydantic]):
    """Service layer for api key operations"""

    def __init__(self):
        super().__init__(ApiKeyTable, ApiKeyPydantic)

    async def auser_by_value(self, value: str) -> Optional[str]:
        """Get a user by value"""
        async with self._session_manager.get_session() as session:
            query = select(ApiKeyTable).where(ApiKeyTable.value == value)
            result = await session.execute(query)
            api_key = result.scalar_one_or_none()
            api_key_pydantic = self._to_pydantic(api_key)
            if api_key_pydantic is not None:
                return api_key_pydantic.user_id

            return None

    def user_by_value(self, value: str) -> Optional[str]:
        """Get a user by value"""
        return asyncio.run(self.auser_by_value(value))
