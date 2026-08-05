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
