from typing import List, Optional
from sqlalchemy import Column, String, JSON, Float
import uuid
from machines.database.base import BaseModel, BaseTable, DatabaseService


class UsageTable(BaseTable):
    """SQLAlchemy model for usage"""

    __tablename__ = "usage"

    # NOTE: uuid will be used to identify the app in fly along with machine id
    uuid = Column(String, default=lambda: str(uuid.uuid4()), nullable=False)
    balance = Column(Float, nullable=False)
    usage = Column(JSON, nullable=False)


class UsagePydantic(BaseModel):
    """Pydantic model for usage"""

    uuid: Optional[str] = None
    balance: float
    usage: dict


class UsageService(DatabaseService[UsageTable, UsagePydantic]):
    """Service layer for usage operations"""

    def __init__(self):
        super().__init__(UsageTable, UsagePydantic)

    async def usage_by_user_id(self, user_id: str) -> List[UsagePydantic]:
        """Get usage by user id"""
        filters = {"user_id": user_id}
        return await self.afind(filters=filters)

    async def current_balance(self, user_id: str) -> float:
        """Get current balance"""

        filters = {"user_id": user_id}
        usage = await self.afind_one(filters=filters)

        return usage.balance if usage else 0

    async def update_balance(self, user_id: str, balance: float):
        """Update balance"""
        filters = {"user_id": user_id}
        usage = await self.afind_one(filters=filters)
        if usage:
            usage.balance = balance
            await self.aupdate(usage)

        else:
            await self.acreate(
                UsagePydantic(user_id=user_id, balance=balance, usage={})
            )
