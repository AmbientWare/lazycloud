from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import UserInDb
from backend.database.services.base import DatabaseService
from backend.database.tables import UserTable


class UserService(DatabaseService[UserTable, UserInDb]):
    """Service layer for user operations."""

    def __init__(self, session: AsyncSession):
        super().__init__(UserTable, UserInDb, session)

    async def get_by_workos_id(self, workos_id: str) -> UserInDb | None:
        """Get user by WorkOS ID."""
        return await self.find_one(filters={"workos_id": workos_id})

    async def get_by_email(self, email: str) -> UserInDb | None:
        """Get user by email."""
        return await self.find_one(filters={"email": email.lower().strip()})
