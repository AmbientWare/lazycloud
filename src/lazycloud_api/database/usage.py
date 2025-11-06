import uuid
from datetime import datetime

from sqlalchemy import (
    UUID,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lazycloud_api.database.base import (
    BaseDbPydanticModel,
    BaseTable,
    DatabaseService,
    UUIDStr,
)
from lazycloud_api.database.session import session_manager
from shared.models.billing import (
    UsageCollectionConfig,
    UsageRecordStatus,
    UsageRecordType,
)


class UsageRecordTable(BaseTable):
    """Stores periodic usage snapshots for billing at workspace level"""

    __tablename__ = "usage_records"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True
    )
    record_type: Mapped[str] = mapped_column(
        String, default=UsageRecordType.HOURLY.value, index=True
    )
    status: Mapped[str] = mapped_column(String, default=UsageRecordStatus.DRAFT.value)
    collection_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    collection_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cpu_core_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    memory_gb_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    storage_gb_hours: Mapped[float] = mapped_column(Float, default=0.0)
    s3_gb_hours: Mapped[float] = mapped_column(Float, default=0.0)
    efs_gb_hours: Mapped[float] = mapped_column(Float, default=0.0)

    # Relationships
    compute_breakdowns: Mapped[list["ComputeUsageBreakdownTable"]] = relationship(
        "ComputeUsageBreakdownTable",
        back_populates="usage_record",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    storage_breakdowns: Mapped[list["StorageUsageBreakdownTable"]] = relationship(
        "StorageUsageBreakdownTable",
        back_populates="usage_record",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    # Indexes for efficient queries
    __table_args__ = (
        Index("ix_usage_records_workspace_dates", "workspace_id", "collection_start"),
        Index("ix_usage_records_workspace_type", "workspace_id", "record_type"),
        Index("ix_usage_records_status", "status", "record_type", "collection_end"),
    )


class ComputeUsageBreakdownTable(BaseTable):
    """Stores detailed per-pod compute breakdown."""

    __tablename__ = "compute_usage_breakdown"

    usage_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("usage_records.id", ondelete="CASCADE"),
        index=True,
    )
    deployment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("compose_deployments.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    pod_name: Mapped[str] = mapped_column(String, index=True)
    service_name: Mapped[str] = mapped_column(String, index=True)
    cpu_core_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    memory_gb_seconds: Mapped[float] = mapped_column(Float, default=0.0)

    # Relationship
    usage_record: Mapped["UsageRecordTable"] = relationship(
        "UsageRecordTable", back_populates="compute_breakdowns"
    )

    # Unique constraint for efficient upserts
    __table_args__ = (
        UniqueConstraint(
            "usage_record_id", "pod_name", name="uq_compute_breakdown_record_pod"
        ),
    )


class StorageUsageBreakdownTable(BaseTable):
    """Stores detailed per-PVC storage breakdown."""

    __tablename__ = "storage_usage_breakdown"

    usage_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("usage_records.id", ondelete="CASCADE"),
        index=True,
    )
    deployment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("compose_deployments.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    pvc_name: Mapped[str] = mapped_column(String, index=True)
    storage_class: Mapped[str] = mapped_column(String, index=True)
    gb_hours: Mapped[float] = mapped_column(Float, default=0.0)

    # Relationship
    usage_record: Mapped["UsageRecordTable"] = relationship(
        "UsageRecordTable", back_populates="storage_breakdowns"
    )

    # Unique constraint for efficient upserts
    __table_args__ = (
        UniqueConstraint(
            "usage_record_id", "pvc_name", name="uq_storage_breakdown_record_pvc"
        ),
    )


class ComputeUsageBreakdownPydantic(BaseDbPydanticModel):
    """Pydantic model for compute usage breakdown."""

    usage_record_id: UUIDStr
    deployment_id: UUIDStr | None = None
    pod_name: str
    service_name: str
    cpu_core_seconds: float
    memory_gb_seconds: float


class StorageUsageBreakdownPydantic(BaseDbPydanticModel):
    """Pydantic model for storage usage breakdown."""

    usage_record_id: UUIDStr
    deployment_id: UUIDStr | None = None
    pvc_name: str
    storage_class: str
    gb_hours: float


class UsageRecordPydantic(BaseDbPydanticModel):
    """Pydantic model for usage record."""

    workspace_id: UUIDStr
    record_type: str
    status: UsageRecordStatus
    collection_start: datetime
    collection_end: datetime
    cpu_core_seconds: float
    memory_gb_seconds: float
    storage_gb_hours: float
    s3_gb_hours: float
    efs_gb_hours: float
    compute_breakdowns: list[ComputeUsageBreakdownPydantic] = []
    storage_breakdowns: list[StorageUsageBreakdownPydantic] = []


class UsageService(DatabaseService[UsageRecordTable, UsageRecordPydantic]):
    """Service for managing usage records."""

    def __init__(self):
        super().__init__(UsageRecordTable, UsageRecordPydantic)

    async def upsert_usage_record(
        self,
        workspace_id: str,
        collection_start: datetime,
        collection_end: datetime,
        cpu_core_seconds: float,
        memory_gb_seconds: float,
        storage_gb_hours: float,
        s3_gb_hours: float = 0.0,
        efs_gb_hours: float = 0.0,
        record_type: UsageRecordType = UsageRecordType.HOURLY,
        status: UsageRecordStatus = UsageRecordStatus.DRAFT,
        session: AsyncSession | None = None,
    ) -> UsageRecordPydantic:
        async def _upsert(sess: AsyncSession):
            # Check if record exists
            result = await sess.execute(
                select(UsageRecordTable)
                .where(UsageRecordTable.workspace_id == workspace_id)
                .where(UsageRecordTable.record_type == record_type.value)
                .where(UsageRecordTable.collection_start == collection_start)
                .where(UsageRecordTable.collection_end == collection_end)
            )
            existing_record = result.scalar_one_or_none()

            if existing_record:
                # Update existing record
                # Only update if not already finalized or reported to prevent data corruption
                if existing_record.status not in (
                    UsageRecordStatus.FINALIZED.value,
                    UsageRecordStatus.REPORTED.value,
                ):
                    existing_record.cpu_core_seconds = cpu_core_seconds
                    existing_record.memory_gb_seconds = memory_gb_seconds
                    existing_record.storage_gb_hours = storage_gb_hours
                    existing_record.s3_gb_hours = s3_gb_hours
                    existing_record.efs_gb_hours = efs_gb_hours
                    existing_record.status = status.value

                await sess.flush()
                await sess.refresh(existing_record)
                return existing_record.to_pydantic(UsageRecordPydantic)
            else:
                # Create new record
                usage_record = UsageRecordTable(
                    workspace_id=workspace_id,
                    record_type=record_type.value,
                    status=status.value,
                    collection_start=collection_start,
                    collection_end=collection_end,
                    cpu_core_seconds=cpu_core_seconds,
                    memory_gb_seconds=memory_gb_seconds,
                    storage_gb_hours=storage_gb_hours,
                    s3_gb_hours=s3_gb_hours,
                    efs_gb_hours=efs_gb_hours,
                )
                sess.add(usage_record)
                await sess.flush()
                await sess.refresh(usage_record)
                return usage_record.to_pydantic(UsageRecordPydantic)

        return await self._execute_in_session(_upsert, session)

    async def upsert_compute_breakdown(
        self,
        usage_record_id: str,
        pod_name: str,
        cpu_core_seconds: float,
        memory_gb_seconds: float,
        service_name: str,
        deployment_id: str,
        session: AsyncSession | None = None,
    ) -> ComputeUsageBreakdownPydantic:
        async def _upsert(sess: AsyncSession):
            stmt = insert(ComputeUsageBreakdownTable).values(
                usage_record_id=usage_record_id,
                pod_name=pod_name,
                cpu_core_seconds=cpu_core_seconds,
                memory_gb_seconds=memory_gb_seconds,
                deployment_id=deployment_id,
                service_name=service_name,
            )

            # On conflict, update the values
            stmt = stmt.on_conflict_do_update(
                index_elements=["usage_record_id", "pod_name"],
                set_={
                    "cpu_core_seconds": stmt.excluded.cpu_core_seconds,
                    "memory_gb_seconds": stmt.excluded.memory_gb_seconds,
                    "deployment_id": stmt.excluded.deployment_id,
                    "service_name": stmt.excluded.service_name,
                    "updated_at": func.now(),
                },
            ).returning(ComputeUsageBreakdownTable)

            result = await sess.execute(stmt)
            breakdown = result.scalar_one()
            await sess.flush()

            return breakdown.to_pydantic(ComputeUsageBreakdownPydantic)

        return await self._execute_in_session(_upsert, session)

    async def upsert_storage_breakdown(
        self,
        usage_record_id: str,
        pvc_name: str,
        storage_class: str,
        gb_hours: float,
        deployment_id: str,
        session: AsyncSession | None = None,
    ) -> StorageUsageBreakdownPydantic:
        async def _upsert(sess: AsyncSession):
            stmt = insert(StorageUsageBreakdownTable).values(
                usage_record_id=usage_record_id,
                pvc_name=pvc_name,
                storage_class=storage_class,
                gb_hours=gb_hours,
                deployment_id=deployment_id,
            )

            # On conflict, update the values
            stmt = stmt.on_conflict_do_update(
                index_elements=["usage_record_id", "pvc_name"],
                set_={
                    "storage_class": stmt.excluded.storage_class,
                    "gb_hours": stmt.excluded.gb_hours,
                    "deployment_id": stmt.excluded.deployment_id,
                    "updated_at": func.now(),
                },
            ).returning(StorageUsageBreakdownTable)

            result = await sess.execute(stmt)
            breakdown = result.scalar_one()
            await sess.flush()

            return breakdown.to_pydantic(StorageUsageBreakdownPydantic)

        return await self._execute_in_session(_upsert, session)

    async def get_workspace_usage(
        self,
        workspace_id: str,
        start_date: datetime,
        end_date: datetime,
        record_type: UsageRecordType | None = None,
    ) -> list[UsageRecordPydantic]:
        async with session_manager.get_session() as session:
            query = (
                select(UsageRecordTable)
                .where(UsageRecordTable.workspace_id == workspace_id)
                .where(UsageRecordTable.collection_start <= end_date)
                .where(UsageRecordTable.collection_end >= start_date)
                .order_by(UsageRecordTable.collection_start)
            )
            if record_type:
                query = query.where(UsageRecordTable.record_type == record_type.value)

            result = await session.execute(query)
            records = result.scalars().all()
            return [record.to_pydantic(UsageRecordPydantic) for record in records]

    async def get_finalized_usage(self) -> list[UsageRecordPydantic]:
        """Get DAILY usage records that are finalized and ready for billing."""
        async with session_manager.get_session() as session:
            result = await session.execute(
                select(UsageRecordTable)
                .where(UsageRecordTable.record_type == UsageRecordType.DAILY.value)
                .where(UsageRecordTable.status == UsageRecordStatus.FINALIZED.value)
                .order_by(UsageRecordTable.collection_end)
            )
            records = result.scalars().all()
            return [record.to_pydantic(UsageRecordPydantic) for record in records]

    async def mark_as_reported(self, usage_record_id: str) -> None:
        """Mark a usage record as reported to billing system."""
        async with session_manager.get_session() as session:
            result = await session.execute(
                select(UsageRecordTable).where(UsageRecordTable.id == usage_record_id)
            )
            record = result.scalar_one_or_none()
            if record:
                record.status = UsageRecordStatus.REPORTED.value
                await session.commit()

    async def finalize_record(self, usage_record_id: str) -> None:
        """Mark a usage record as finalized and ready for billing."""
        async with session_manager.get_session() as session:
            result = await session.execute(
                select(UsageRecordTable).where(UsageRecordTable.id == usage_record_id)
            )
            record = result.scalar_one_or_none()
            if record:
                record.status = UsageRecordStatus.FINALIZED.value
                await session.commit()

    async def get_incomplete_usage(self) -> list[UsageRecordPydantic]:
        """Get DAILY usage records that are incomplete and need retry."""
        async with session_manager.get_session() as session:
            result = await session.execute(
                select(UsageRecordTable)
                .where(UsageRecordTable.record_type == UsageRecordType.DAILY.value)
                .where(UsageRecordTable.status == UsageRecordStatus.INCOMPLETE.value)
                .order_by(UsageRecordTable.collection_end)
            )
            records = result.scalars().all()
            return [record.to_pydantic(UsageRecordPydantic) for record in records]

    async def get_latest_interval_usage(
        self, workspace_id: str
    ) -> UsageRecordPydantic | None:
        """Get the most recent interval usage record based on current collection config."""
        async with session_manager.get_session() as session:
            result = await session.execute(
                select(UsageRecordTable)
                .where(UsageRecordTable.workspace_id == workspace_id)
                .where(
                    UsageRecordTable.record_type
                    == UsageCollectionConfig.get_record_type().value
                )
                .order_by(UsageRecordTable.collection_end.desc())
                .limit(1)
            )
            record = result.scalar_one_or_none()
            if record:
                return record.to_pydantic(UsageRecordPydantic)
            return None
