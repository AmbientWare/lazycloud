from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
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
    json_type,
    uuid_type,
)


class PoolTable(NamedWorkspacePayloadTable, DatabaseBase):
    __tablename__ = "pools"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("workspace_id", "name", name="uq_pools_workspace_name"),
        UniqueConstraint("capacity_owner_id", name="uq_pools_capacity_owner_id"),
        CheckConstraint(
            "min_workers >= 0 AND initial_workers >= min_workers "
            "AND max_workers >= initial_workers",
            name="ck_pools_worker_bounds",
        ),
        CheckConstraint(
            "min_free_cpu_millicores >= 0 AND min_free_memory_mib >= 0 "
            "AND min_free_gpu_count >= 0 AND worker_cpu_millicores >= 0 "
            "AND worker_memory_mib >= 0 AND worker_gpu_count >= 0",
            name="ck_pools_worker_shape",
        ),
        CheckConstraint(
            "(worker_gpu_type = '' AND worker_gpu_count = 0) "
            "OR (worker_gpu_type <> '' AND worker_gpu_count > 0)",
            name="ck_pools_worker_gpu",
        ),
        CheckConstraint(
            "idle_drain_timeout_seconds BETWEEN 60 AND 86400 "
            "AND scale_up_cooldown_seconds BETWEEN 0 AND 86400 "
            "AND scale_down_cooldown_seconds BETWEEN 0 AND 86400 "
            "AND registration_timeout_seconds BETWEEN 30 AND 3600",
            name="ck_pools_lifecycle_timeouts",
        ),
        CheckConstraint(
            "sizing_revision >= 0 AND sizing_target_units >= 0 "
            "AND sizing_consecutive_failures >= 0",
            name="ck_pools_sizing_state",
        ),
    )

    provider: Mapped[str] = mapped_column(String(120), nullable=False, default="local")
    capacity_owner_id: Mapped[str] = mapped_column(uuid_type, nullable=False)
    capacity_owner_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    capacity_owner_source: Mapped[str] = mapped_column(String(32), nullable=False)
    initial_workers: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    min_workers: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_workers: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    scaling_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    default_eligible: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    min_free_cpu_millicores: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    min_free_memory_mib: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    min_free_gpu_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    worker_cpu_millicores: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    worker_memory_mib: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    worker_gpu_type: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    worker_gpu_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    worker_runtimes: Mapped[list[str]] = mapped_column(json_type, nullable=False)
    worker_preemptible: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    idle_drain_timeout_seconds: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    scale_up_cooldown_seconds: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    scale_down_cooldown_seconds: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    registration_timeout_seconds: Mapped[int] = mapped_column(Integer, default=600, nullable=False)
    sizing_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sizing_initial_target_reached: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    sizing_operation_id: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    sizing_target_units: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sizing_operation_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sizing_last_scale_up_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sizing_last_scale_down_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sizing_retry_after_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sizing_consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sizing_terminal_reason: Mapped[str] = mapped_column(String(500), default="", nullable=False)


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


class MachineTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "machines"
    __table_args__: tuple[SchemaItem, ...] = (
        Index("ix_machines_workspace_created", "workspace_id", "created_at"),
        Index("ix_machines_pool_status", "pool", "status"),
        Index("ix_machines_provider_status", "provider", "status"),
    )

    workspace_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
    )
    pool: Mapped[str] = mapped_column(String(240), nullable=False, default="default")
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
    version: Mapped[str] = mapped_column(String(120), nullable=False, default="local")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ContainerTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "containers"
    __table_args__: tuple[SchemaItem, ...] = (
        Index("ix_containers_workspace_created", "workspace_id", "created_at"),
        Index("ix_containers_status_created", "status", "created_at", "id"),
        Index("ix_containers_stub", "stub_id"),
        Index("ix_containers_worker_status", "worker_id", "status"),
        Index("ix_containers_machine_status", "machine_id", "status"),
        Index(
            "ix_containers_unsettled_preemption",
            "finished_at",
            postgresql_where=text(
                "termination_reason = 'PREEMPTED' AND preemption_settled_at IS NULL"
            ),
            sqlite_where=text("termination_reason = 'PREEMPTED' AND preemption_settled_at IS NULL"),
        ),
        CheckConstraint(
            "termination_reason IN ('TTL', 'USER', 'SCHEDULER', 'PREEMPTED', 'ADMIN', 'UNKNOWN')",
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
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    image: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(80), nullable=False)
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
    pool_name: Mapped[str | None] = mapped_column(String(240), nullable=True)
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


class ProviderTable(NamedWorkspacePayloadTable, DatabaseBase):
    __tablename__ = "providers"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("workspace_id", "name", name="uq_providers_workspace_name"),
        CheckConstraint("kind = 'aws'", name="ck_providers_kind_aws"),
        CheckConstraint(
            "(jsonb_typeof(payload) = 'object' "
            "AND jsonb_typeof(payload -> 'kind') = 'string' "
            "AND payload ->> 'kind' = kind "
            "AND payload ->> 'kind' = 'aws' "
            "AND NOT coalesce(payload -> 'config' ? 'provider_kind', false)) IS TRUE",
            name="ck_providers_payload_kind_aws",
        ).ddl_if(dialect="postgresql"),
    )

    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)


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
