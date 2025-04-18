from typing import List, Optional
from sqlalchemy import Column, String, Float, DateTime
import uuid
from datetime import datetime

from machines.database.base import BaseModel, BaseTable, DatabaseService


class UsageTable(BaseTable):
    """SQLAlchemy model for usage"""

    __tablename__ = "usage"

    # NOTE: uuid will be used to identify the app in fly along with machine id
    uuid = Column(String, default=lambda: str(uuid.uuid4()), nullable=False)
    balance = Column(Float, nullable=False)
    last_collected_at = Column(DateTime(timezone=True), nullable=False)


class UsagePydantic(BaseModel):
    """Pydantic model for usage"""

    uuid: Optional[str] = None
    balance: float
    last_collected_at: datetime


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
