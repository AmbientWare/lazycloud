import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    UUID,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    select,
)
from sqlalchemy.orm import Mapped, joinedload, mapped_column, relationship

from lazycloud_api.database.base import (
    BaseDbPydanticModel,
    BaseTable,
    DatabaseService,
    UUIDStr,
)
from lazycloud_api.database.session import session_manager


class UsageRecordType(StrEnum):
    """Type of usage record for aggregation level."""

    HOURLY = "hourly"
    DAILY = "daily"


class UsageRecordTable(BaseTable):
    """Stores periodic usage snapshots for billing at workspace level"""

    __tablename__ = "usage_records"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    record_type: Mapped[str] = mapped_column(
        String, default=UsageRecordType.HOURLY.value, index=True
    )
    collection_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    collection_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cpu_core_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    memory_gb_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    storage_gb_hours: Mapped[float] = mapped_column(Float, default=0.0)

    # Billing fields (only used for DAILY records)
    reported_to_billing: Mapped[bool] = mapped_column(Boolean, default=False)
    reported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    breakdowns: Mapped[list["UsageBreakdownTable"]] = relationship(
        "UsageBreakdownTable",
        back_populates="usage_record",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    # Indexes for efficient queries
    __table_args__ = (
        Index("ix_usage_records_workspace_dates", "workspace_id", "collection_start"),
        Index("ix_usage_records_workspace_type", "workspace_id", "record_type"),
        Index(
            "ix_usage_records_billing",
            "record_type",
            "reported_to_billing",
            "collection_end",
        ),
    )


class UsageBreakdownTable(BaseTable):
    """Stores detailed service-level breakdown."""

    __tablename__ = "usage_breakdown"

    usage_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("usage_records.id", ondelete="CASCADE"),
        index=True,
    )
    service_name: Mapped[str] = mapped_column(String, index=True)
    cpu_core_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    memory_gb_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    pod_count: Mapped[int] = mapped_column(Integer, default=0)

    # Relationship
    usage_record: Mapped["UsageRecordTable"] = relationship(
        "UsageRecordTable", back_populates="breakdowns"
    )

    __table_args__ = (Index("ix_usage_breakdown_service", "service_name"),)


class UsageBreakdownPydantic(BaseDbPydanticModel):
    """Pydantic model for usage breakdown."""

    usage_record_id: UUIDStr
    service_name: str
    cpu_core_seconds: float
    memory_gb_seconds: float
    pod_count: int


class UsageRecordPydantic(BaseDbPydanticModel):
    """Pydantic model for usage record."""

    workspace_id: UUIDStr
    record_type: str
    collection_start: datetime
    collection_end: datetime
    cpu_core_seconds: float
    memory_gb_seconds: float
    storage_gb_hours: float
    reported_to_billing: bool
    reported_at: datetime | None = None
    breakdowns: list[UsageBreakdownPydantic] = []


class UsageService(DatabaseService[UsageRecordTable, UsageRecordPydantic]):
    """Service for managing usage records."""

    def __init__(self):
        super().__init__(UsageRecordTable, UsageRecordPydantic)

    async def upsert_usage_record(
        self,
        workspace_id: uuid.UUID,
        collection_start: datetime,
        collection_end: datetime,
        cpu_core_seconds: float,
        memory_gb_seconds: float,
        storage_gb_hours: float,
        record_type: UsageRecordType = UsageRecordType.HOURLY,
    ) -> UsageRecordPydantic:
        async with session_manager.get_session() as session:
            # Check if record exists
            result = await session.execute(
                select(UsageRecordTable)
                .where(UsageRecordTable.workspace_id == workspace_id)
                .where(UsageRecordTable.record_type == record_type.value)
                .where(UsageRecordTable.collection_start == collection_start)
                .where(UsageRecordTable.collection_end == collection_end)
            )
            existing_record = result.scalar_one_or_none()

            if existing_record:
                # Update existing record
                existing_record.cpu_core_seconds = cpu_core_seconds
                existing_record.memory_gb_seconds = memory_gb_seconds
                existing_record.storage_gb_hours = storage_gb_hours
                await session.commit()
                await session.refresh(existing_record)
                return existing_record.to_pydantic(UsageRecordPydantic)
            else:
                # Create new record
                usage_record = UsageRecordTable(
                    workspace_id=workspace_id,
                    record_type=record_type.value,
                    collection_start=collection_start,
                    collection_end=collection_end,
                    cpu_core_seconds=cpu_core_seconds,
                    memory_gb_seconds=memory_gb_seconds,
                    storage_gb_hours=storage_gb_hours,
                    reported_to_billing=False,
                )
                session.add(usage_record)
                await session.commit()
                await session.refresh(usage_record)
                return usage_record.to_pydantic(UsageRecordPydantic)

    async def upsert_usage_breakdown(
        self,
        usage_record_id: uuid.UUID,
        service_name: str,
        cpu_core_seconds: float,
        memory_gb_seconds: float,
        pod_count: int,
    ) -> UsageBreakdownPydantic:
        async with session_manager.get_session() as session:
            # Check if breakdown exists
            result = await session.execute(
                select(UsageBreakdownTable)
                .where(UsageBreakdownTable.usage_record_id == usage_record_id)
                .where(UsageBreakdownTable.service_name == service_name)
            )
            existing_breakdown = result.scalar_one_or_none()

            if existing_breakdown:
                # Update existing breakdown
                existing_breakdown.cpu_core_seconds = cpu_core_seconds
                existing_breakdown.memory_gb_seconds = memory_gb_seconds
                existing_breakdown.pod_count = pod_count
                await session.commit()
                await session.refresh(existing_breakdown)
                return existing_breakdown.to_pydantic(UsageBreakdownPydantic)
            else:
                # Create new breakdown
                breakdown = UsageBreakdownTable(
                    usage_record_id=usage_record_id,
                    service_name=service_name,
                    cpu_core_seconds=cpu_core_seconds,
                    memory_gb_seconds=memory_gb_seconds,
                    pod_count=pod_count,
                )
                session.add(breakdown)
                await session.commit()
                await session.refresh(breakdown)
                return breakdown.to_pydantic(UsageBreakdownPydantic)

    async def get_workspace_usage(
        self,
        workspace_id: uuid.UUID,
        start_date: datetime,
        end_date: datetime,
        record_type: UsageRecordType | None = None,
    ) -> list[UsageRecordPydantic]:
        async with session_manager.get_session() as session:
            query = (
                select(UsageRecordTable)
                .where(UsageRecordTable.workspace_id == workspace_id)
                .where(UsageRecordTable.collection_start >= start_date)
                .where(UsageRecordTable.collection_end <= end_date)
                .order_by(UsageRecordTable.collection_start)
                .options(joinedload(UsageRecordTable.breakdowns))
            )
            if record_type:
                query = query.where(UsageRecordTable.record_type == record_type.value)

            result = await session.execute(query)
            records = result.scalars().all()
            return [record.to_pydantic(UsageRecordPydantic) for record in records]

    async def get_unreported_usage(self) -> list[UsageRecordPydantic]:
        async with session_manager.get_session() as session:
            result = await session.execute(
                select(UsageRecordTable)
                .where(UsageRecordTable.record_type == UsageRecordType.DAILY.value)
                .where(UsageRecordTable.reported_to_billing == False)  # noqa: E712
                .order_by(UsageRecordTable.collection_end)
                .options(joinedload(UsageRecordTable.breakdowns))
            )
            records = result.scalars().all()
            return [record.to_pydantic(UsageRecordPydantic) for record in records]

    async def mark_as_reported(
        self, usage_record_id: uuid.UUID, reported_at: datetime
    ) -> None:
        async with session_manager.get_session() as session:
            result = await session.execute(
                select(UsageRecordTable).where(UsageRecordTable.id == usage_record_id)
            )
            record = result.scalar_one_or_none()
            if record:
                record.reported_to_billing = True
                record.reported_at = reported_at
                await session.commit()
