import uuid
from datetime import datetime, timezone

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
    update,
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
from shared.models.billing import (
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
    standard_gb_hours: Mapped[float] = mapped_column(Float, default=0.0)
    shared_gb_hours: Mapped[float] = mapped_column(Float, default=0.0)
    build_minutes: Mapped[float] = mapped_column(Float, default=0.0)
    public_endpoint_hours: Mapped[float] = mapped_column(Float, default=0.0)

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
    networking_breakdowns: Mapped[list["NetworkingUsageBreakdownTable"]] = relationship(
        "NetworkingUsageBreakdownTable",
        back_populates="usage_record",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    build_breakdowns: Mapped[list["BuildUsageBreakdownTable"]] = relationship(
        "BuildUsageBreakdownTable",
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


class NetworkingUsageBreakdownTable(BaseTable):
    """Stores detailed per-service networking breakdown (endpoints, future: ingress/egress)."""

    __tablename__ = "networking_usage_breakdown"

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
    service_name: Mapped[str] = mapped_column(String, index=True)
    endpoint_hours: Mapped[float] = mapped_column(Float, default=0.0)

    # Relationship
    usage_record: Mapped["UsageRecordTable"] = relationship(
        "UsageRecordTable", back_populates="networking_breakdowns"
    )

    # Unique constraint for efficient upserts (one record per service per usage record)
    __table_args__ = (
        UniqueConstraint(
            "usage_record_id",
            "service_name",
            name="uq_networking_breakdown_record_service",
        ),
    )


class BuildUsageBreakdownTable(BaseTable):
    """Stores detailed per-deployment build breakdown."""

    __tablename__ = "build_usage_breakdown"

    usage_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("usage_records.id", ondelete="CASCADE"),
        index=True,
    )
    deployment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("compose_deployments.id", ondelete="RESTRICT"),
        index=True,
    )
    build_minutes: Mapped[float] = mapped_column(Float, default=0.0)

    # Relationship
    usage_record: Mapped["UsageRecordTable"] = relationship(
        "UsageRecordTable", back_populates="build_breakdowns"
    )

    # Unique constraint for efficient upserts (one record per deployment per usage record)
    __table_args__ = (
        UniqueConstraint(
            "usage_record_id",
            "deployment_id",
            name="uq_build_breakdown_record_deployment",
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


class NetworkingUsageBreakdownPydantic(BaseDbPydanticModel):
    """Pydantic model for networking usage breakdown."""

    usage_record_id: UUIDStr
    deployment_id: UUIDStr | None = None
    service_name: str
    endpoint_hours: float


class BuildUsageBreakdownPydantic(BaseDbPydanticModel):
    """Pydantic model for build usage breakdown."""

    usage_record_id: UUIDStr
    deployment_id: UUIDStr
    build_minutes: float


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
    standard_gb_hours: float
    shared_gb_hours: float
    build_minutes: float = 0.0
    public_endpoint_hours: float = 0.0
    compute_breakdowns: list[ComputeUsageBreakdownPydantic] = []
    storage_breakdowns: list[StorageUsageBreakdownPydantic] = []
    networking_breakdowns: list[NetworkingUsageBreakdownPydantic] = []
    build_breakdowns: list[BuildUsageBreakdownPydantic] = []


class UsageService(DatabaseService[UsageRecordTable, UsageRecordPydantic]):
    """Service for managing usage records."""

    # Fields to exclude when updating (relationship fields)
    _update_exclude_fields = {
        "id",
        "created_at",
        "updated_at",
        "compute_breakdowns",
        "storage_breakdowns",
        "networking_breakdowns",
        "build_breakdowns",
    }

    def __init__(self, session: AsyncSession):
        super().__init__(UsageRecordTable, UsageRecordPydantic, session)

    async def update(self, model: UsageRecordPydantic) -> UsageRecordPydantic | None:
        """Update a usage record, excluding relationship fields."""

        update_data = model.model_dump(exclude=self._update_exclude_fields)
        update_data["updated_at"] = datetime.now(timezone.utc)

        stmt = (
            update(UsageRecordTable)
            .where(UsageRecordTable.id == model.id)
            .values(**update_data)
            .returning(UsageRecordTable)
        )
        result = await self._session.execute(
            stmt, execution_options={"populate_existing": True}
        )
        updated_model = result.scalar_one_or_none()
        return self._to_pydantic(updated_model)

    async def upsert_usage_record(
        self,
        workspace_id: str,
        collection_start: datetime,
        collection_end: datetime,
        cpu_core_seconds: float,
        memory_gb_seconds: float,
        storage_gb_hours: float,
        standard_gb_hours: float = 0.0,
        shared_gb_hours: float = 0.0,
        build_minutes: float = 0.0,
        public_endpoint_hours: float = 0.0,
        record_type: UsageRecordType = UsageRecordType.HOURLY,
        status: UsageRecordStatus = UsageRecordStatus.DRAFT,
    ) -> UsageRecordPydantic:
        """Upsert a usage record for a workspace."""
        result = await self._session.execute(
            select(UsageRecordTable)
            .where(UsageRecordTable.workspace_id == workspace_id)
            .where(UsageRecordTable.record_type == record_type.value)
            .where(UsageRecordTable.collection_start == collection_start)
            .where(UsageRecordTable.collection_end == collection_end)
        )
        existing_record = result.scalar_one_or_none()

        if existing_record:
            # Only update if not already finalized or reported
            if existing_record.status not in (
                UsageRecordStatus.FINALIZED.value,
                UsageRecordStatus.REPORTED.value,
            ):
                existing_record.cpu_core_seconds = cpu_core_seconds
                existing_record.memory_gb_seconds = memory_gb_seconds
                existing_record.storage_gb_hours = storage_gb_hours
                existing_record.standard_gb_hours = standard_gb_hours
                existing_record.shared_gb_hours = shared_gb_hours
                existing_record.build_minutes = build_minutes
                existing_record.public_endpoint_hours = public_endpoint_hours
                existing_record.status = status.value

            await self._session.flush()
            await self._session.refresh(existing_record)
            return existing_record.to_pydantic(UsageRecordPydantic)

        else:
            usage_record = UsageRecordTable(
                workspace_id=workspace_id,
                record_type=record_type.value,
                status=status.value,
                collection_start=collection_start,
                collection_end=collection_end,
                cpu_core_seconds=cpu_core_seconds,
                memory_gb_seconds=memory_gb_seconds,
                storage_gb_hours=storage_gb_hours,
                standard_gb_hours=standard_gb_hours,
                shared_gb_hours=shared_gb_hours,
                build_minutes=build_minutes,
                public_endpoint_hours=public_endpoint_hours,
            )
            self._session.add(usage_record)
            await self._session.flush()
            await self._session.refresh(usage_record)
            return usage_record.to_pydantic(UsageRecordPydantic)

    async def upsert_compute_breakdown(
        self,
        usage_record_id: str,
        pod_name: str,
        cpu_core_seconds: float,
        memory_gb_seconds: float,
        service_name: str,
        deployment_id: str,
    ) -> ComputeUsageBreakdownPydantic:
        """Upsert a compute usage breakdown."""
        stmt = insert(ComputeUsageBreakdownTable).values(
            usage_record_id=usage_record_id,
            pod_name=pod_name,
            cpu_core_seconds=cpu_core_seconds,
            memory_gb_seconds=memory_gb_seconds,
            deployment_id=deployment_id,
            service_name=service_name,
        )
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

        result = await self._session.execute(stmt)
        breakdown = result.scalar_one()
        await self._session.flush()
        return breakdown.to_pydantic(ComputeUsageBreakdownPydantic)

    async def upsert_storage_breakdown(
        self,
        usage_record_id: str,
        pvc_name: str,
        storage_class: str,
        gb_hours: float,
        deployment_id: str,
    ) -> StorageUsageBreakdownPydantic:
        """Upsert a storage usage breakdown."""
        stmt = insert(StorageUsageBreakdownTable).values(
            usage_record_id=usage_record_id,
            pvc_name=pvc_name,
            storage_class=storage_class,
            gb_hours=gb_hours,
            deployment_id=deployment_id,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["usage_record_id", "pvc_name"],
            set_={
                "storage_class": stmt.excluded.storage_class,
                "gb_hours": stmt.excluded.gb_hours,
                "deployment_id": stmt.excluded.deployment_id,
                "updated_at": func.now(),
            },
        ).returning(StorageUsageBreakdownTable)

        result = await self._session.execute(stmt)
        breakdown = result.scalar_one()
        await self._session.flush()
        return breakdown.to_pydantic(StorageUsageBreakdownPydantic)

    async def upsert_networking_breakdown(
        self,
        usage_record_id: str,
        service_name: str,
        endpoint_hours: float,
        deployment_id: str,
    ) -> NetworkingUsageBreakdownPydantic:
        """Upsert a networking usage breakdown."""
        stmt = insert(NetworkingUsageBreakdownTable).values(
            usage_record_id=usage_record_id,
            service_name=service_name,
            endpoint_hours=endpoint_hours,
            deployment_id=deployment_id,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["usage_record_id", "service_name"],
            set_={
                "endpoint_hours": stmt.excluded.endpoint_hours,
                "deployment_id": stmt.excluded.deployment_id,
                "updated_at": func.now(),
            },
        ).returning(NetworkingUsageBreakdownTable)

        result = await self._session.execute(stmt)
        breakdown = result.scalar_one()
        await self._session.flush()
        return breakdown.to_pydantic(NetworkingUsageBreakdownPydantic)

    async def upsert_build_breakdown(
        self,
        usage_record_id: str,
        deployment_id: str,
        build_minutes: float,
    ) -> BuildUsageBreakdownPydantic:
        """Upsert a build usage breakdown."""
        stmt = insert(BuildUsageBreakdownTable).values(
            usage_record_id=usage_record_id,
            deployment_id=deployment_id,
            build_minutes=build_minutes,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["usage_record_id", "deployment_id"],
            set_={
                "build_minutes": stmt.excluded.build_minutes,
                "updated_at": func.now(),
            },
        ).returning(BuildUsageBreakdownTable)

        result = await self._session.execute(stmt)
        breakdown = result.scalar_one()
        await self._session.flush()
        return breakdown.to_pydantic(BuildUsageBreakdownPydantic)

    async def get_workspace_usage(
        self,
        workspace_id: str,
        start_date: datetime,
        end_date: datetime,
        record_type: UsageRecordType | None = None,
    ) -> list[UsageRecordPydantic]:
        query = (
            select(UsageRecordTable)
            .where(UsageRecordTable.workspace_id == workspace_id)
            .where(UsageRecordTable.collection_start <= end_date)
            .where(UsageRecordTable.collection_end >= start_date)
            .order_by(UsageRecordTable.collection_start)
        )
        if record_type:
            query = query.where(UsageRecordTable.record_type == record_type.value)

        result = await self._session.execute(query)
        records = result.scalars().all()
        return [record.to_pydantic(UsageRecordPydantic) for record in records]

    async def get_finalized_usage(self) -> list[UsageRecordPydantic]:
        """Get DAILY usage records that are finalized and ready for billing."""
        result = await self._session.execute(
            select(UsageRecordTable)
            .where(UsageRecordTable.record_type == UsageRecordType.DAILY.value)
            .where(UsageRecordTable.status == UsageRecordStatus.FINALIZED.value)
            .order_by(UsageRecordTable.collection_end)
        )
        records = result.scalars().all()
        return [record.to_pydantic(UsageRecordPydantic) for record in records]

    async def mark_as_reported(self, usage_record_id: str) -> None:
        """Mark a usage record as reported to billing system."""
        result = await self._session.execute(
            select(UsageRecordTable).where(UsageRecordTable.id == usage_record_id)
        )
        record = result.scalar_one_or_none()
        if record:
            record.status = UsageRecordStatus.REPORTED.value
            await self._session.flush()

    async def finalize_record(self, usage_record_id: str) -> None:
        """Mark a usage record as finalized and ready for billing."""
        result = await self._session.execute(
            select(UsageRecordTable).where(UsageRecordTable.id == usage_record_id)
        )
        record = result.scalar_one_or_none()
        if record:
            record.status = UsageRecordStatus.FINALIZED.value
            await self._session.flush()

    async def get_incomplete_usage(self) -> list[UsageRecordPydantic]:
        """Get DAILY usage records that are incomplete and need retry."""
        result = await self._session.execute(
            select(UsageRecordTable)
            .where(UsageRecordTable.record_type == UsageRecordType.DAILY.value)
            .where(UsageRecordTable.status == UsageRecordStatus.INCOMPLETE.value)
            .order_by(UsageRecordTable.collection_end)
        )
        records = result.scalars().all()
        return [record.to_pydantic(UsageRecordPydantic) for record in records]
