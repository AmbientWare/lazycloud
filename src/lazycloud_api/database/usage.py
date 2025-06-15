from typing import List, Optional
from sqlalchemy import Column, String, Float
import uuid
from enum import StrEnum
from sqlalchemy.orm import relationship

from lazycloud_api.database.base import BaseModel, BaseTable, DatabaseService


class UsageStatus(StrEnum):
    """Status of usage"""

    ACTIVE = "active"
    INACTIVE = "inactive"
    DELETED = "deleted"


class UsageTable(BaseTable):
    """SQLAlchemy model for usage"""

    __tablename__ = "usage"

    # NOTE: uuid will be used to identify the app in fly along with machine id
    uuid = Column(String, default=lambda: str(uuid.uuid4()), nullable=False)
    balance = Column(Float, nullable=False, default=0)
    status = Column(String, nullable=False, default=UsageStatus.ACTIVE)

    # one to many relationship with usage periods
    usage_periods = relationship(
        "UsagePeriodTable", back_populates="usage", cascade="all, delete-orphan"
    )


class UsagePydantic(BaseModel):
    """Pydantic model for usage"""

    uuid: Optional[str] = None
    balance: float
    status: UsageStatus


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
