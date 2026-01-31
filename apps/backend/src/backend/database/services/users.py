from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import User
from backend.database.services.base import DatabaseService
from backend.database.tables import UserTable


class UserService(DatabaseService[UserTable, User]):
    """Service layer for user operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(UserTable, User, session)

    async def get_by_workos_id(self, workos_id: str) -> User | None:
        """Get user by WorkOS ID"""
        filters = {"workos_id": workos_id}
        return await self.find_one(filters=filters)

    async def get_by_email(self, email: str) -> User | None:
        """Get user by email"""
        filters = {"email": email.lower().strip()}
        return await self.find_one(filters=filters)
