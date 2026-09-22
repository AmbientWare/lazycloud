from __future__ import annotations

from datetime import datetime

from pydantic import JsonValue
from shared.placement import Placement
from sqlalchemy import (
    DDL,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import (
    DatabaseBase,
    IdTable,
    TimestampMixin,
    json_type,
    uuid_type,
)


class AutoscalerStateTable(TimestampMixin, DatabaseBase):
    __tablename__ = "autoscaler_states"
    __table_args__ = (
        Index("ix_autoscaler_states_workspace_source", "workspace_id", "source"),
        Index("ix_autoscaler_states_target", "target_kind", "target_id"),
        CheckConstraint(
            "current_count >= 0 AND desired_count >= 0 AND pending_count >= 0 "
            "AND failed_container_count >= 0",
            name="ck_autoscaler_states_counts",
        ),
        CheckConstraint(
            "target_kind IN ('function', 'endpoint', 'pod')",
            name="ck_autoscaler_states_target_kind",
        ),
    )
    workspace_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    target_kind: Mapped[str] = mapped_column(String(80), primary_key=True)
    target_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    deployment_id: Mapped[str] = mapped_column(Text, nullable=False)
    app_id: Mapped[str] = mapped_column(Text, nullable=False)
    current_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    desired_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    signal_name: Mapped[str] = mapped_column(Text, nullable=False)
    signal_value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    lock_acquired: Mapped[bool] = mapped_column(Boolean, nullable=False)
    owner_lock_key: Mapped[str] = mapped_column(Text, nullable=False)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_container_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pending_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    guardrails: Mapped[dict[str, JsonValue]] = mapped_column(json_type, nullable=False)
    last_actions: Mapped[list[dict[str, JsonValue]]] = mapped_column(json_type, nullable=False)


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


class MachineTable(IdTable, DatabaseBase):
    """One machine, bought by a unit or joined by an account.

    A joined machine carries the account-unique `name` workloads pin to and the
    `owner_user_id` that name is unique within. Both are null for capacity a
    provider launched, which nothing addresses by name.
    """

    __tablename__ = "machines"
    __table_args__: tuple[SchemaItem, ...] = (
        Index("ix_machines_workspace_created", "workspace_id", "created_at"),
        Index("ix_machines_placement_status", "placement", "status"),
        Index("ix_machines_placement_lifecycle", "placement", "lifecycle"),
        CheckConstraint(
            "lifecycle IN ('requested', 'provisioning', 'booting', 'joining', 'ready', "
            "'draining', 'stopping', 'stopped', 'resuming', 'terminating', 'deleted', 'failed')",
            name="ck_machines_lifecycle",
        ),
        Index("ix_machines_workspace_owner", "workspace_id", "capacity_owner_id"),
        Index("ix_machines_provider_status", "provider", "status"),
        Index(
            "uq_machines_owner_name",
            "owner_user_id",
            "name",
            unique=True,
            postgresql_where=text("name IS NOT NULL AND status <> 'deleted'"),
        ),
        CheckConstraint("gpu_count >= 0", name="ck_machines_gpu_count"),
        CheckConstraint("name IS NULL OR owner_user_id IS NOT NULL", name="ck_machines_name_owner"),
    )

    workspace_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
    )
    owner_user_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str | None] = mapped_column(String(63), nullable=True)
    placement: Mapped[str] = mapped_column(
        String(120), nullable=False, default=Placement.platform().key
    )
    capacity_owner_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    provider: Mapped[str] = mapped_column(String(120), nullable=False, default="local")
    status: Mapped[str] = mapped_column(String(80), nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(32), nullable=False)
    lifecycle_message: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    lifecycle_failure: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lifecycle_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    address: Mapped[str | None] = mapped_column(String(512), nullable=True)

    cpu: Mapped[float | None] = mapped_column(Float, nullable=True)
    memory: Mapped[str | None] = mapped_column(Text, nullable=True)
    gpu: Mapped[str | None] = mapped_column(Text, nullable=True)
    gpu_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    labels: Mapped[dict[str, str]] = mapped_column(json_type, nullable=False)
    workspaces: Mapped[list[MachineWorkspaceTable]] = relationship(
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="MachineWorkspaceTable.workspace_id",
    )


class MachineWorkspaceTable(DatabaseBase):
    """A workspace whose workloads may land on one joined machine."""

    __tablename__ = "machine_workspaces"
    __table_args__: tuple[SchemaItem, ...] = (
        Index("ix_machine_workspaces_workspace", "workspace_id"),
    )

    machine_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("machines.id", ondelete="CASCADE"),
        primary_key=True,
    )
    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )


class WorkerTable(IdTable, DatabaseBase):
    __tablename__ = "workers"
    __table_args__: tuple[SchemaItem, ...] = (
        Index("ix_workers_workspace_created", "workspace_id", "created_at"),
        Index("ix_workers_placement_status", "placement", "status"),
        Index("ix_workers_machine", "machine_id"),
        CheckConstraint("admitted_release_generation >= 0", name="ck_workers_release_generation"),
        CheckConstraint("update_generation >= 0", name="ck_workers_update_generation"),
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
    placement: Mapped[str] = mapped_column(
        String(120), nullable=False, default=Placement.platform().key
    )
    status: Mapped[str] = mapped_column(String(80), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    admitted_release_generation: Mapped[int] = mapped_column(BigInteger, server_default="0")
    admitted_runtime_image: Mapped[str] = mapped_column(String(1024), server_default="")
    admitted_agent_sha256: Mapped[str] = mapped_column(String(64), server_default="")
    update_generation: Mapped[int] = mapped_column(BigInteger, server_default="0")
    update_runtime_image: Mapped[str] = mapped_column(String(1024), server_default="")
    update_agent_sha256: Mapped[str] = mapped_column(String(64), server_default="")
    update_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    labels: Mapped[dict[str, str]] = mapped_column(json_type, nullable=False)


class ContainerTable(IdTable, DatabaseBase):
    __tablename__ = "containers"
    workload_ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scheduling_reconcile_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scheduling_assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scheduling_assignment_token: Mapped[str | None] = mapped_column(String(240))
    capacity_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__: tuple[SchemaItem, ...] = (
        Index("ix_containers_workspace_created", "workspace_id", "created_at"),
        Index(
            "ix_containers_capacity_due",
            "capacity_retry_at",
            "id",
            postgresql_where=text("capacity_retry_at IS NOT NULL AND status = 'pending'"),
        ),
        Index(
            "ix_containers_scheduling_due",
            "scheduling_reconcile_at",
            "id",
            postgresql_where=text("scheduling_requested_at IS NOT NULL AND status = 'pending'"),
        ),
        # Concurrency is counted on the path that starts every container, so the
        # cost of asking has to be bounded by the answer rather than by how much
        # the workspace has ever run. Partial, so the index holds only what is
        # live: on the full workspace index the same count scans the workspace's
        # entire history and gets slower every day it is used.
        Index(
            "ix_containers_workspace_live",
            "workspace_id",
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
        Index("ix_containers_status_created", "status", "created_at", "id"),
        Index("ix_containers_stub", "stub_id"),
        Index(
            "ix_containers_stub_live",
            "stub_id",
            "created_at",
            "id",
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
        Index(
            "ix_containers_stub_failed_created",
            "stub_id",
            "created_at",
            "id",
            postgresql_where=text("status = 'failed'"),
        ),
        Index(
            "ix_containers_stub_failed_finished",
            "stub_id",
            "finished_at",
            "id",
            postgresql_where=text("status = 'failed' AND finished_at IS NOT NULL"),
        ),
        Index("ix_containers_worker_status", "worker_id", "status"),
        Index("ix_containers_machine_status", "machine_id", "status"),
        Index(
            "ix_containers_live_expiry",
            "expires_at",
            "id",
            postgresql_where=text("expires_at IS NOT NULL AND status IN ('pending', 'running')"),
        ),
        Index(
            "ix_containers_unsettled_preemption",
            "finished_at",
            postgresql_where=text(
                "termination_reason = 'PREEMPTED' AND preemption_settled_at IS NULL"
            ),
        ),
        CheckConstraint(
            "termination_reason IN "
            "('TTL', 'USER', 'SCHEDULER', 'PREEMPTED', 'ADMIN', 'UNFUNDED', "
            "'MEMORY_EVICTED', 'UNKNOWN')",
            name="ck_containers_termination_reason",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'exited', 'failed', 'stopped')",
            name="ck_containers_status",
        ),
        CheckConstraint("gpu_count >= 0", name="ck_containers_gpu_count"),
        CheckConstraint("timeout_seconds >= -1", name="ck_containers_timeout"),
        CheckConstraint(
            "scheduling_cpu_millicores >= 0 AND scheduling_memory_mib >= 0 "
            "AND scheduling_gpu_count >= 0 AND scheduling_workspace_gpu_quota >= 0 "
            "AND scheduling_workspace_cpu_quota_millicores >= 0 AND scheduling_retry_count >= 0",
            name="ck_containers_scheduling_quantities",
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
    storage_released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    preemption_settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    gpu_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    command: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    runtime_machine_id: Mapped[str] = mapped_column(Text, nullable=False, default="")
    runtime_worker_id: Mapped[str] = mapped_column(Text, nullable=False, default="")
    pid: Mapped[int | None] = mapped_column(BigInteger, nullable=True, default=None)
    startup_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    cwd: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    env: Mapped[dict[str, str]] = mapped_column(json_type, nullable=False, default=dict)
    ports: Mapped[dict[str, int]] = mapped_column(json_type, nullable=False, default=dict)
    network_blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    network_allow_list: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    gpu: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    timeout_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    scheduling_stub_id: Mapped[str] = mapped_column(Text, nullable=False, default="")
    scheduling_deployment_id: Mapped[str] = mapped_column(Text, nullable=False, default="")
    scheduling_cpu_millicores: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    scheduling_required_worker_id: Mapped[str] = mapped_column(Text, nullable=False, default="")
    scheduling_memory_mib: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    scheduling_gpu: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    scheduling_gpu_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    scheduling_placement: Mapped[str | None] = mapped_column(String(120), nullable=True)
    """Where the scheduling request lands; null until a request is recorded."""
    scheduling_architecture: Mapped[str] = mapped_column(Text, nullable=False, default="")
    scheduling_provider_runtime: Mapped[str] = mapped_column(Text, nullable=False, default="")
    scheduling_runtime_class: Mapped[str] = mapped_column(Text, nullable=False, default="")
    scheduling_docker_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    scheduling_preemptible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    scheduling_workspace_gpu_quota: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    scheduling_workspace_cpu_quota_millicores: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    scheduling_retry_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    scheduling_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    scheduling_payload: Mapped[dict[str, JsonValue]] = mapped_column(
        json_type, nullable=False, default=dict, deferred=True, deferred_raiseload=True
    )
    scheduling_backfill: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    scheduling_region: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    scheduling_availability_zone: Mapped[str] = mapped_column(Text, nullable=False, default="")


Index(
    "ix_containers_pending_storage_worker",
    ContainerTable.runtime_worker_id,
    ContainerTable.id,
    postgresql_where=ContainerTable.storage_released_at.is_(None),
)


event.listen(
    ContainerTable.__table__,
    "after_create",
    DDL("""
CREATE OR REPLACE FUNCTION enforce_container_assignment_ownership()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (OLD.status IN ('exited', 'failed', 'stopped') AND NEW.status IN ('pending', 'running'))
        OR (OLD.status = 'running' AND NEW.status = 'pending') THEN
        RAISE EXCEPTION 'container status cannot be reopened'
            USING ERRCODE = '23514';
    END IF;
    IF OLD.runtime_worker_id <> '' AND (
        NEW.runtime_worker_id
            <> OLD.runtime_worker_id
        OR NEW.runtime_machine_id
            <> OLD.runtime_machine_id
    ) AND NOT (
        OLD.status = 'pending' AND NEW.status = 'pending'
        AND NEW.runtime_worker_id = ''
        AND NEW.runtime_machine_id = ''
        AND OLD.scheduling_assignment_token IS NOT NULL
        AND NEW.scheduling_assignment_token IS NULL
    ) THEN
        RAISE EXCEPTION 'container assignment cannot change without its ownership token'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER container_assignment_ownership_fence
BEFORE UPDATE ON containers
FOR EACH ROW EXECUTE FUNCTION enforce_container_assignment_ownership();
""").execute_if(dialect="postgresql"),
)


class AgentTable(IdTable, DatabaseBase):
    __tablename__ = "agents"
    __table_args__: tuple[SchemaItem, ...] = (
        Index("ix_agents_workspace_created", "workspace_id", "created_at"),
        Index("ix_agents_placement_status", "placement", "status"),
    )

    workspace_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    placement: Mapped[str] = mapped_column(
        String(120), nullable=False, default=Placement.platform().key
    )
    status: Mapped[str] = mapped_column(String(80), nullable=False)
    version: Mapped[str] = mapped_column(String(120), nullable=False, default="local")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    capacity: Mapped[dict[str, int | float | str]] = mapped_column(json_type, nullable=False)
    labels: Mapped[dict[str, str]] = mapped_column(json_type, nullable=False)
    install_command: Mapped[str | None] = mapped_column(Text, nullable=True)


class AgentLeaseTable(IdTable, DatabaseBase):
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
