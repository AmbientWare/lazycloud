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
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import DatabaseBase, IdPayloadTable, IdTable, uuid_type


class TaskTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "tasks"
    __table_args__: tuple[SchemaItem, ...] = (
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


class TaskAttemptTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "task_attempts"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("task_id", "attempt_number", name="uq_task_attempts_task_attempt"),
        Index("ix_task_attempts_task", "task_id", "attempt_number"),
        Index("ix_task_attempts_workspace_created", "workspace_id", "created_at"),
        Index("ix_task_attempts_status_created", "status", "created_at"),
        Index("ix_task_attempts_container", "container_id"),
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
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(80), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskDependencyTable(IdPayloadTable, DatabaseBase):
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


class LogTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "logs"
    __table_args__: tuple[SchemaItem, ...] = (
        Index("ix_logs_task_created", "task_id", "created_at"),
        Index("ix_logs_workspace_created", "workspace_id", "created_at"),
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
    stream: Mapped[str] = mapped_column(String(40), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)


class EventTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "events"
    __table_args__: tuple[SchemaItem, ...] = (
        Index("ix_events_resource", "resource_type", "resource_id", "created_at"),
        Index("ix_events_workspace_created", "workspace_id", "created_at"),
        Index("ix_events_created", "created_at"),
        Index("ix_events_action_created", "action", "created_at"),
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


class QueueMessageTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "queue_messages"
    __table_args__: tuple[SchemaItem, ...] = (
        Index("ix_queue_messages_queue_available", "queue", "available_at"),
        Index("ix_queue_messages_workspace_queue", "workspace_id", "queue"),
        Index(
            "ix_queue_messages_workspace_queue_claim",
            "workspace_id",
            "queue",
            "available_at",
            "created_at",
        ),
        Index(
            "ix_queue_messages_workspace_queue_expires",
            "workspace_id",
            "queue",
            "expires_at",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    queue: Mapped[str] = mapped_column(String(240), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PodProcessTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "pod_processes"
    __table_args__: tuple[SchemaItem, ...] = (Index("ix_pod_processes_container", "container_id"),)

    container_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("containers.id", ondelete="CASCADE"),
        nullable=False,
    )
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(80), nullable=False, default="running")


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


class CronJobRunTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "cron_job_runs"
    __table_args__: tuple[SchemaItem, ...] = (
        Index("ix_cron_job_runs_cron_job_created", "cron_job", "created_at"),
        Index("ix_cron_job_runs_workspace_created", "workspace_id", "created_at"),
    )

    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )

    cron_job: Mapped[str] = mapped_column(String(240), nullable=False)
    enqueued: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    message_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("queue_messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    task_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
    )
