from sqlalchemy import Column, DateTime, JSON, Integer, ForeignKey
from datetime import datetime, timezone
from sqlalchemy.orm import relationship
from typing import Optional

from lazycloud_api.database.base import BaseModel, BaseTable, DatabaseService
from lazycloud_api.database.usage import UsageStatus


class UsagePeriodTable(BaseTable):
    """SQLAlchemy model for usage"""

    __tablename__ = "usage_periods"

    start_at = Column(DateTime(timezone=True), nullable=False)
    end_at = Column(DateTime(timezone=True), nullable=False)
    data = Column(JSON, nullable=False, default={})

    # many to one relationship with usage
    usage_id = Column(Integer, ForeignKey("usage.id"), nullable=False)
    usage = relationship("UsageTable", back_populates="usage_periods")


class UsagePeriodPydantic(BaseModel):
    """Pydantic model for usage"""

    start_at: datetime
    end_at: datetime
    data: dict = {}
    usage_id: Optional[int] = None


class UsagePeriodService(DatabaseService[UsagePeriodTable, UsagePeriodPydantic]):
    """Service layer for usage operations"""

    def __init__(self):
        super().__init__(UsagePeriodTable, UsagePeriodPydantic)

    async def add_usage_to_current_period(
        self, user_id: str, usage_id: int, data: dict
    ) -> UsagePeriodPydantic:
        """Add usage data"""
        today = datetime.now(timezone.utc)
        # we need to check to see if there is already a usage period for today
        current_usage_period = await self.get_current_usage_period(usage_id)
        if current_usage_period and current_usage_period.id:
            # update the usage period
            current_usage_period.data = data
            await self.aupdate(current_usage_period)
        else:
            # usage can be addeed at any time during month so we need to see which period to create

            if today.day <= 15:
                start_at = today.replace(day=1)
                end_at = today.replace(day=15)
            else:
                start_at = today.replace(day=16)
                end_at = today.replace(day=31)

            # create a new usage period
            new_usage_period = UsagePeriodPydantic(
                start_at=start_at,
                end_at=end_at,
                data=data,
                user_id=user_id,
            )
            await self.acreate(new_usage_period)

        return current_usage_period or new_usage_period

    async def get_current_usage_period(
        self, usage_id: int
    ) -> UsagePeriodPydantic | None:
        """Get current usage period"""
        # Usage is broken into 1st-15th and 16th-end of the month
        today = datetime.now(timezone.utc)
        if today.day <= 15:
            filters = {
                "usage_id": usage_id,
                "status": UsageStatus.ACTIVE,
                "start_at": today.replace(day=1),
            }
        else:
            filters = {
                "usage_id": usage_id,
                "status": UsageStatus.ACTIVE,
                "start_at": today.replace(day=16),
            }

        return await self.afind_one(filters=filters)
