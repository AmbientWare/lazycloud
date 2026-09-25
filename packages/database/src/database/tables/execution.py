from __future__ import annotations

from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.json_documents import JsonDocument
from database.tables.base import DatabaseBase, IdTable, json_type, uuid_type

# Bound dependency results may contain base64 envelopes for multiple invocations.
task_data_type = JsonDocument(max_bytes=128 * 1024 * 1024)


class TaskTable(IdTable, DatabaseBase):
    __tablename__ = "tasks"
    handler: Mapped[str | None] = mapped_column(Text, nullable=True)
    command: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    args: Mapped[list[JsonValue]] = mapped_column(task_data_type, default=list)
    kwargs: Mapped[dict[str, JsonValue]] = mapped_column(task_data_type, default=dict)
    input_container_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    invocation: Mapped[dict[str, JsonValue] | None] = mapped_column(task_data_type, nullable=True)
    dependency_bindings: Mapped[list[dict[str, JsonValue]]] = mapped_column(
        task_data_type, default=list
    )
    function_result: Mapped[dict[str, JsonValue] | None] = mapped_column(
        task_data_type, nullable=True
    )
    result: Mapped[JsonValue] = mapped_column(task_data_type, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_backoff: Mapped[str | None] = mapped_column(String(20), nullable=True)
    retry_delay_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    retry_max_delay_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    retry_on_statuses: Mapped[list[str] | None] = mapped_column(ARRAY(String(80)), nullable=True)
    __table_args__: tuple[SchemaItem, ...] = (
        CheckConstraint("attempt_number >= 0 AND max_attempts >= 1", name="ck_tasks_attempts"),
        CheckConstraint(
            "retry_backoff IS NULL OR retry_backoff IN ('fixed', 'exponential')",
            name="ck_tasks_retry_backoff",
        ),
        CheckConstraint(
            "retry_delay_seconds >= 0 AND (retry_max_delay_seconds IS NULL OR "
            "retry_max_delay_seconds >= retry_delay_seconds)",
            name="ck_tasks_retry_delay",
        ),
        CheckConstraint(
            "(retry_backoff IS NULL AND retry_delay_seconds IS NULL AND "
            "retry_max_delay_seconds IS NULL AND retry_on_statuses IS NULL) OR "
            "(retry_backoff IS NOT NULL AND retry_delay_seconds IS NOT NULL AND "
            "retry_on_statuses IS NOT NULL)",
            name="ck_tasks_retry_policy",
        ),
        Index("ix_tasks_input_container", "input_container_id"),
        Index("ix_tasks_workspace_created", "workspace_id", "created_at", "id"),
        Index("ix_tasks_stub_created", "stub_id", "created_at", "id"),
        Index("ix_tasks_container", "container_id"),
        Index("ix_tasks_parent_created", "parent_task_id", "created_at", "id"),
        Index("ix_tasks_root_created", "root_task_id", "created_at", "id"),
        Index("ix_tasks_status_created", "status", "created_at"),
        Index("ix_tasks_retry_due", "status", "next_retry_at"),
        # The claim reads exactly these three, in this order: work for one stub,
        # not yet finished, whose inputs have resolved. Oldest first.
        Index("ix_tasks_stub_claimable", "stub_id", "status", "claimable_at"),
    )

    workspace_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
    )
    app_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("apps.id", ondelete="SET NULL"),
        nullable=True,
    )
    stub_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("stubs.id", ondelete="SET NULL"),
        nullable=True,
    )
    deployment_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("deployments.id", ondelete="SET NULL"),
        nullable=True,
    )
    container_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("containers.id", ondelete="SET NULL"),
        nullable=True,
    )
    parent_task_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
    )
    root_task_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    status: Mapped[str] = mapped_column(String(80), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When this task's inputs resolved and it became eligible to run.
    #
    # Null while a dependency is still outstanding. Set once, never cleared, and
    # never moved: it is the durable record that readiness was established, which
    # `container_id` only stood in for while one container served one task. A
    # claim reads it; nothing else may write it twice.
    claimable_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskAttemptTable(IdTable, DatabaseBase):
    __tablename__ = "task_attempts"
    result: Mapped[JsonValue] = mapped_column(task_data_type, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    __table_args__: tuple[SchemaItem, ...] = (
        CheckConstraint("attempt_number >= 1", name="ck_task_attempts_number"),
        UniqueConstraint("task_id", "attempt_number", name="uq_task_attempts_task_attempt"),
        Index("ix_task_attempts_task", "task_id", "attempt_number"),
        Index("ix_task_attempts_workspace_created", "workspace_id", "created_at"),
        Index("ix_task_attempts_status_created", "status", "created_at"),
        Index("ix_task_attempts_container", "container_id"),
        Index(
            "uq_task_attempts_container_claim",
            "container_id",
            "claim_id",
            unique=True,
            postgresql_where=text("claim_id IS NOT NULL"),
        ),
    )

    workspace_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
    )
    task_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    container_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("containers.id", ondelete="SET NULL"),
        nullable=True,
    )
    claim_id: Mapped[str | None] = mapped_column(uuid_type, nullable=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(80), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskDependencyTable(IdTable, DatabaseBase):
    __tablename__ = "task_dependencies"
    __table_args__: tuple[SchemaItem, ...] = (
        Index("ix_task_dependencies_task", "task_id", "upstream_task_id"),
        Index("ix_task_dependencies_upstream", "upstream_task_id", "task_id"),
        Index("ix_task_dependencies_workspace_root", "workspace_id", "root_task_id"),
    )

    workspace_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,
    )
    task_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    upstream_task_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_task_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
    )
    root_task_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
    )
    edge_type: Mapped[str] = mapped_column(String(80), nullable=False)


class LogTable(IdTable, DatabaseBase):
    __tablename__ = "logs"
    __table_args__: tuple[SchemaItem, ...] = (
        Index("ix_logs_created", "created_at", "id"),
        Index("ix_logs_task_created", "task_id", "created_at", "id"),
        Index("ix_logs_container_created", "container_id", "created_at", "id"),
        Index("ix_logs_workspace_created", "workspace_id", "created_at", "id"),
    )

    workspace_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
    )
    task_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=True,
    )
    # Attribution survives resource deletion and reassignment until log retention expires.
    container_id: Mapped[str | None] = mapped_column(uuid_type, nullable=True)
    app_id: Mapped[str | None] = mapped_column(uuid_type, nullable=True)
    deployment_id: Mapped[str | None] = mapped_column(uuid_type, nullable=True)
    stub_id: Mapped[str | None] = mapped_column(uuid_type, nullable=True)
    machine_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    stream: Mapped[str] = mapped_column(String(40), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)


class EventTable(IdTable, DatabaseBase):
    __tablename__ = "events"
    data: Mapped[dict[str, JsonValue]] = mapped_column(json_type, default=dict)
    container_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    __table_args__: tuple[SchemaItem, ...] = (
        Index("ix_events_resource", "resource_type", "resource_id", "created_at"),
        Index("ix_events_workspace_created", "workspace_id", "created_at"),
        Index("ix_events_created", "created_at"),
        Index("ix_events_action_created", "action", "created_at"),
        Index("ix_events_container_created", "container_id", "created_at", "id"),
    )

    workspace_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
    )
    action: Mapped[str] = mapped_column(String(160), nullable=False)
    level: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(120), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(160), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)


class PodUrlTable(IdTable, DatabaseBase):
    __tablename__ = "pod_urls"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("container_id", "port", name="uq_pod_urls_container_port"),
        Index("ix_pod_urls_container", "container_id"),
        CheckConstraint("port >= 1 AND port <= 65535", name="ck_pod_urls_port_range"),
    )

    container_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("containers.id", ondelete="CASCADE"),
        nullable=False,
    )
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)


class CronJobRunTable(IdTable, DatabaseBase):
    __tablename__ = "cron_job_runs"
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__: tuple[SchemaItem, ...] = (
        Index("ix_cron_job_runs_cron_job_created", "cron_job", "created_at", "id"),
        Index("ix_cron_job_runs_workspace_created", "workspace_id", "created_at", "id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )

    cron_job: Mapped[str] = mapped_column(String(240), nullable=False)
    enqueued: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    task_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
    )
