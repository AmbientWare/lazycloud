from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import (
    DatabaseBase,
    IdPayloadTable,
    NamedWorkspacePayloadTable,
    TimestampMixin,
    uuid_type,
)


class AutoscalerStateTable(NamedWorkspacePayloadTable, DatabaseBase):
    __tablename__ = "autoscaler_states"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint(
            "workspace_id",
            "name",
            name="uq_autoscaler_states_workspace_name",
        ),
        Index("ix_autoscaler_states_workspace_source", "workspace_id", "source"),
        Index("ix_autoscaler_states_target", "target_kind", "target_id"),
    )

    source: Mapped[str] = mapped_column(String(120), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    target_id: Mapped[str] = mapped_column(String(160), nullable=False)
    decision: Mapped[str] = mapped_column(String(80), nullable=False, default="")


class AutoscalingTargetTable(TimestampMixin, DatabaseBase):
    __tablename__ = "autoscaling_targets"
    __table_args__: tuple[SchemaItem, ...] = (
        CheckConstraint("generation >= 1", name="ck_autoscaling_targets_generation"),
        CheckConstraint(
            "target_kind IN ('function', 'endpoint', 'pod')",
            name="ck_autoscaling_targets_kind",
        ),
        CheckConstraint(
            "(claim_token IS NULL) = (claim_expires_at IS NULL)",
            name="ck_autoscaling_targets_claim",
        ),
        Index(
            "ix_autoscaling_targets_due",
            "due_at",
            "stub_id",
        ),
        Index("ix_autoscaling_targets_workspace", "workspace_id"),
    )

    stub_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("stubs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    claim_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class MachineTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "machines"
    __table_args__: tuple[SchemaItem, ...] = (
        Index("ix_machines_workspace_created", "workspace_id", "created_at"),
        Index("ix_machines_pool_status", "pool", "status"),
        Index("ix_machines_workspace_owner", "workspace_id", "capacity_owner_id"),
        Index("ix_machines_provider_status", "provider", "status"),
    )

    workspace_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
    )
    pool: Mapped[str] = mapped_column(String(240), nullable=False, default="default")
    capacity_owner_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    provider: Mapped[str] = mapped_column(String(120), nullable=False, default="local")
    status: Mapped[str] = mapped_column(String(80), nullable=False)
    address: Mapped[str | None] = mapped_column(String(512), nullable=True)


class WorkerTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "workers"
    __table_args__: tuple[SchemaItem, ...] = (
        Index("ix_workers_workspace_created", "workspace_id", "created_at"),
        Index("ix_workers_pool_status", "pool", "status"),
        Index("ix_workers_machine", "machine_id"),
    )

    workspace_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
    )
    machine_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("machines.id", ondelete="SET NULL"),
        nullable=True,
    )
    pool: Mapped[str] = mapped_column(String(240), nullable=False, default="default")
    status: Mapped[str] = mapped_column(String(80), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ContainerTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "containers"
    __table_args__: tuple[SchemaItem, ...] = (
        Index("ix_containers_workspace_created", "workspace_id", "created_at"),
        # Concurrency is counted on the path that starts every container, so the
        # cost of asking has to be bounded by the answer rather than by how much
        # the workspace has ever run. Partial, so the index holds only what is
        # live: on the full workspace index the same count scans the workspace's
        # entire history and gets slower every day it is used.
        Index(
            "ix_containers_workspace_live",
            "workspace_id",
            postgresql_where=text("status IN ('pending', 'running')"),
            sqlite_where=text("status IN ('pending', 'running')"),
        ),
        Index("ix_containers_status_created", "status", "created_at", "id"),
        Index("ix_containers_stub", "stub_id"),
        Index(
            "ix_containers_stub_live",
            "stub_id",
            "created_at",
            "id",
            postgresql_where=text("status IN ('pending', 'running')"),
            sqlite_where=text("status IN ('pending', 'running')"),
        ),
        Index(
            "ix_containers_stub_failed_created",
            "stub_id",
            "created_at",
            "id",
            postgresql_where=text("status = 'failed'"),
            sqlite_where=text("status = 'failed'"),
        ),
        Index(
            "ix_containers_stub_failed_finished",
            "stub_id",
            "finished_at",
            "id",
            postgresql_where=text("status = 'failed' AND finished_at IS NOT NULL"),
            sqlite_where=text("status = 'failed' AND finished_at IS NOT NULL"),
        ),
        Index("ix_containers_worker_status", "worker_id", "status"),
        Index("ix_containers_machine_status", "machine_id", "status"),
        Index(
            "ix_containers_live_expiry",
            "expires_at",
            "id",
            postgresql_where=text("expires_at IS NOT NULL AND status IN ('pending', 'running')"),
            sqlite_where=text("expires_at IS NOT NULL AND status IN ('pending', 'running')"),
        ),
        Index(
            "ix_containers_unsettled_preemption",
            "finished_at",
            postgresql_where=text(
                "termination_reason = 'PREEMPTED' AND preemption_settled_at IS NULL"
            ),
            sqlite_where=text("termination_reason = 'PREEMPTED' AND preemption_settled_at IS NULL"),
        ),
        CheckConstraint(
            "termination_reason IN "
            "('TTL', 'USER', 'SCHEDULER', 'PREEMPTED', 'ADMIN', 'UNFUNDED', "
            "'MEMORY_EVICTED', 'UNKNOWN')",
            name="ck_containers_termination_reason",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    stub_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("stubs.id", ondelete="SET NULL"),
        nullable=True,
    )
    app_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("apps.id", ondelete="SET NULL"),
        nullable=True,
    )
    machine_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("machines.id", ondelete="SET NULL"),
        nullable=True,
    )
    worker_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("workers.id", ondelete="SET NULL"),
        nullable=True,
    )
    task_id: Mapped[str | None] = mapped_column(
        uuid_type,
        # `use_alter` for the same reason as `apps.stub_id`: containers and
        # tasks point at each other, and this is the nullable pointer at
        # whatever the container is running rather than the task's attribution.
        ForeignKey("tasks.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    image: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(80), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    termination_reason: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        default="UNKNOWN",
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    preemption_settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    gpu_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class RouteTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "routes"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("route_id", name="uq_routes_route_id"),
        Index("ix_routes_workspace_machine", "workspace_id", "machine_id"),
        Index("ix_routes_container", "container_id"),
    )

    workspace_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
    )
    pool: Mapped[str | None] = mapped_column(String(240), nullable=True)
    machine_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("machines.id", ondelete="SET NULL"),
        nullable=True,
    )
    container_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("containers.id", ondelete="SET NULL"),
        nullable=True,
    )
    route_id: Mapped[str] = mapped_column(String(512), nullable=False)
    state: Mapped[str] = mapped_column(String(80), nullable=False, default="opening")


class AgentTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "agents"
    __table_args__: tuple[SchemaItem, ...] = (
        Index("ix_agents_workspace_created", "workspace_id", "created_at"),
        Index("ix_agents_pool_status", "pool", "status"),
    )

    workspace_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    pool: Mapped[str] = mapped_column(String(240), nullable=False, default="default")
    status: Mapped[str] = mapped_column(String(80), nullable=False)
    version: Mapped[str] = mapped_column(String(120), nullable=False, default="local")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentLeaseTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "agent_leases"
    __table_args__: tuple[SchemaItem, ...] = (
        Index("ix_agent_leases_agent_status", "agent_id", "status"),
        Index("ix_agent_leases_resource", "resource_type", "resource_id"),
    )

    agent_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    resource_type: Mapped[str] = mapped_column(String(120), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(80), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
