from typing import List, Optional
from datetime import datetime
import asyncio
from sqlalchemy import Column, String, DateTime
from sqlalchemy.future import select

from machines.database.base import BaseModel, BaseTable, DatabaseService
from enum import Enum


class TokenRole(str, Enum):
    ADMIN = "admin"
    USER = "user"


class TokenExpirationMinutes(int, Enum):
    """The expiration time for a token in minutes"""

    THIRTY_MINUTES = 30
    ONE_HOUR = 60
    THREE_HOURS = 180


class TokenExpirationDays(int, Enum):
    """The expiration time for a token in days"""

    ONE_DAY = 1
    THIRTY_DAYS = 30
    ONE_HUNDRED_DAYS = 100
    THREE_HUNDRED_SIXTY_FIVE_DAYS = 365


class TokenTable(BaseTable):
    """SQLAlchemy model for a token"""

    __tablename__ = "tokens"

    token = Column(String, nullable=False, unique=True, index=True)
    role = Column(String, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)


class TokenPydantic(BaseModel):
    """Pydantic model for a token"""

    token: str
    expires_at: datetime
    role: TokenRole


class TokenService(DatabaseService[TokenTable, TokenPydantic]):
    """Service layer for token operations"""

    def __init__(self):
        super().__init__(TokenTable, TokenPydantic)

    async def auser_by_token(self, token: str) -> Optional[str]:
        """Get a user by token"""
        async with self._session_manager.get_session() as session:
            query = select(TokenTable).where(TokenTable.token == token)
            result = await session.execute(query)
            token = result.scalar_one_or_none()
            token_pydantic = self._to_pydantic(token)
            if token_pydantic is not None:
                return token_pydantic.user_id

            return None

    def user_by_token(self, token: str) -> Optional[str]:
        """Get a user by token"""
        return asyncio.run(self.auser_by_token(token))
