from typing import List, Optional
from datetime import datetime
import asyncio
from sqlalchemy import Column, String, DateTime
from sqlalchemy.future import select

from machines.database.base import BaseModel, BaseTable, DatabaseService


class TokenTable(BaseTable):
    """SQLAlchemy model for a token"""

    __tablename__ = "tokens"

    token = Column(String, nullable=False, unique=True, index=True)
    expires_at = Column(DateTime, nullable=False)


class TokenPydantic(BaseModel):
    """Pydantic model for a token"""

    token: str
    expires_at: datetime


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
