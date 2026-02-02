from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from backend.database.models.api_keys import ApiKeyInDb
from backend.database.services.base import (
    DatabaseService,
)
from backend.database.tables.api_keys import ApiKeyTable


class ApiKeyService(DatabaseService[ApiKeyTable, ApiKeyInDb]):
    """Service layer for api key operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(ApiKeyTable, ApiKeyInDb, session)

    async def get_by_user_id(self, user_id: str) -> list[ApiKeyInDb]:
        """Get a api key by reference id"""
        query = select(ApiKeyTable).where(ApiKeyTable.user_id == user_id)
        result = await self._session.execute(query)
        api_keys = result.scalars().all()
        return [self._to_pydantic(api_key) for api_key in api_keys]

    async def user_by_value(self, value: str) -> str | None:
        """Get a user by value"""
        query = select(ApiKeyTable).where(ApiKeyTable.value == value)
        result = await self._session.execute(query)
        api_key = result.scalar_one_or_none()
        if api_key is None:
            return None
        api_key_pydantic = self._to_pydantic(api_key)
        return api_key_pydantic.user_id
