from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from database.repositories.observability import (
    UsageRecordCursor,
    UsageRepository,
    WorkerEventRepository,
)
from database.types import DatabaseSession
from pydantic import JsonValue
from shared.contracts import ContractModel
from shared.errors import InvalidInputError
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.timestamps import utc_now
from shared.usage import (
    UsageAggregation,
    UsageGroupKey,
    UsageMetric,
    UsageRecord,
    UsageUnit,
    usage_record_id,
)
from shared.usage_query import UsageQuery
from shared.worker_events import WorkerEventFilter, WorkerEventRecord

from database import AsyncDatabaseClient
from observability.context import ObservabilityContext
from observability.usage_pricing import MeteredUsagePricer
from observability.workspace_changes import AsyncWorkspaceChangeService, WorkspaceChangePublisher

WORKER_EVENT_RETENTION = timedelta(days=30)
USAGE_CHANGE_BOUNDARY_METRICS = frozenset(
    {
        UsageMetric.TaskCount,
        UsageMetric.PersistentVolumeByteSeconds,
    }
)


class UsageRecordCursorPayload(ContractModel):
    created_at: datetime
    id: UUID


@dataclass(frozen=True, slots=True)
class UsageRecordPageResult:
    data: tuple[UsageRecord, ...]
    next: str = ""


@dataclass(slots=True)
class WorkerEventService:
    context: ObservabilityContext

    def append(self, record: WorkerEventRecord) -> WorkerEventRecord:
        with self.context.database.session() as session:
            return WorkerEventRepository(session).append(record)

    def list(
        self,
        *,
        filter: WorkerEventFilter | None = None,
        limit: int | None = None,
    ) -> list[WorkerEventRecord]:
        with self.context.database.session() as session:
            return WorkerEventRepository(session).list(filter=filter, limit=limit)

    def prune(self, *, retention: timedelta = WORKER_EVENT_RETENTION) -> int:
        with self.context.database.session() as session:
            return WorkerEventRepository(session).prune(older_than=utc_now() - retention)


@dataclass(slots=True)
class UsageService:
    context: ObservabilityContext
    workspace_changes: WorkspaceChangePublisher | None = None
    async_database: AsyncDatabaseClient | None = None
    async_workspace_changes: AsyncWorkspaceChangeService | None = None

    def record(
        self,
        *,
        id: str | None = None,
        workspace_id: str,
        resource_type: str,
        resource_id: str,
        metric: UsageMetric,
        quantity: float,
        unit: UsageUnit,
        labels: dict[str, str] | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> UsageRecord:
        with self.context.database.session() as session:
            record = self._record_in_session(
                session,
                id=id,
                workspace_id=workspace_id,
                resource_type=resource_type,
                resource_id=resource_id,
                metric=metric,
                quantity=quantity,
                unit=unit,
                labels=labels,
                metadata=dict(metadata) if metadata is not None else None,
            )
        self._publish_change(record)
        return record

    def _record_in_session(
        self,
        session: DatabaseSession,
        *,
        id: str | None = None,
        workspace_id: str,
        resource_type: str,
        resource_id: str,
        metric: UsageMetric,
        quantity: float,
        unit: UsageUnit,
        labels: dict[str, str] | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> UsageRecord:
        record = UsageRepository(session).record(
            id=id,
            workspace_id=workspace_id,
            resource_type=resource_type,
            resource_id=resource_id,
            metric=metric,
            quantity=quantity,
            unit=unit,
            labels=labels,
            metadata=dict(metadata) if metadata is not None else None,
        )
        MeteredUsagePricer(session).price(record)
        return record

    async def record_async(
        self,
        *,
        id: str | None = None,
        workspace_id: str,
        resource_type: str,
        resource_id: str,
        metric: UsageMetric,
        quantity: float,
        unit: UsageUnit,
        labels: dict[str, str] | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> UsageRecord:
        database = self.async_database
        if database is None:
            raise RuntimeError("asynchronous usage database is not configured")
        record = await database.run_transaction(
            lambda session: self._record_in_session(
                session,
                id=id,
                workspace_id=workspace_id,
                resource_type=resource_type,
                resource_id=resource_id,
                metric=metric,
                quantity=quantity,
                unit=unit,
                labels=labels,
                metadata=metadata,
            )
        )
        await self._publish_change_async(record)
        return record

    def append(self, record: UsageRecord) -> UsageRecord:
        with self.context.database.session() as session:
            saved = UsageRepository(session).append(record)
            MeteredUsagePricer(session).price(saved)
        self._publish_change(saved)
        return saved

    async def append_async(self, record: UsageRecord) -> UsageRecord:
        database = self.async_database
        if database is None:
            raise RuntimeError("asynchronous usage database is not configured")

        def persist(session: DatabaseSession) -> UsageRecord:
            saved = UsageRepository(session).append(record)
            MeteredUsagePricer(session).price(saved)
            return saved

        saved = await database.run_transaction(persist)
        await self._publish_change_async(saved)
        return saved

    def list(
        self,
        query: UsageQuery | None = None,
        *,
        workspace_id: str | None = None,
    ) -> list[UsageRecord]:
        with self.context.database.session() as session:
            repository = UsageRepository(session)
            if workspace_id is None:
                return repository.list_across_workspaces(query)
            return repository.list(query, workspace_id=workspace_id)

    def list_page(
        self,
        query: UsageQuery,
        *,
        workspace_id: str,
        limit: int,
        metric: UsageMetric | None = None,
        cursor: str | None = None,
    ) -> UsageRecordPageResult:
        if limit < 1 or limit > 1000:
            raise InvalidInputError("usage record limit must be between 1 and 1000")
        decoded = _decode_usage_record_cursor(cursor)
        with self.context.database.session() as session:
            page = UsageRepository(session).page(
                query,
                workspace_id=workspace_id,
                limit=limit,
                metric=metric,
                cursor=decoded,
            )
        return UsageRecordPageResult(
            data=page.data,
            next=_encode_usage_record_cursor(page.next) if page.next is not None else "",
        )

    def aggregate(
        self,
        *,
        query: UsageQuery | None = None,
        metric: UsageMetric | None = None,
        group_by: tuple[UsageGroupKey, ...] = (),
    ) -> list[UsageAggregation]:
        with self.context.database.session() as session:
            return UsageRepository(session).aggregate(query=query, metric=metric, group_by=group_by)

    def record_task_count(
        self,
        *,
        workspace_id: str,
        resource_type: str,
        resource_id: str,
        task_id: str,
        kind: str,
        app_id: str = "",
        deployment_id: str = "",
    ) -> UsageRecord:
        labels, metadata = _task_count_labels(
            resource_id=resource_id,
            task_id=task_id,
            kind=kind,
            app_id=app_id,
            deployment_id=deployment_id,
        )
        return self.record(
            id=usage_record_id(UsageMetric.TaskCount.value, workspace_id, task_id),
            workspace_id=workspace_id,
            resource_type=resource_type,
            resource_id=resource_id,
            metric=UsageMetric.TaskCount,
            quantity=1,
            unit=UsageUnit.Count,
            labels=labels,
            metadata=metadata,
        )

    async def record_task_count_async(
        self,
        *,
        workspace_id: str,
        resource_type: str,
        resource_id: str,
        task_id: str,
        kind: str,
        app_id: str = "",
        deployment_id: str = "",
    ) -> UsageRecord:
        labels, metadata = _task_count_labels(
            resource_id=resource_id,
            task_id=task_id,
            kind=kind,
            app_id=app_id,
            deployment_id=deployment_id,
        )
        return await self.record_async(
            id=usage_record_id(UsageMetric.TaskCount.value, workspace_id, task_id),
            workspace_id=workspace_id,
            resource_type=resource_type,
            resource_id=resource_id,
            metric=UsageMetric.TaskCount,
            quantity=1,
            unit=UsageUnit.Count,
            labels=labels,
            metadata=metadata,
        )

    def _publish_change(self, record: UsageRecord) -> None:
        identity = _usage_change_identity(record)
        if self.workspace_changes is None or identity is None:
            return
        self.workspace_changes.emit_change(
            workspace_id=record.workspace_id,
            topic=WorkspaceChangeTopic.Usage,
            change=WorkspaceChangeType.Updated,
            resource_id=record.id,
            app_id=identity.app_id,
            deployment_id=identity.deployment_id,
            stub_id=identity.stub_id,
            task_id=identity.task_id,
            container_id=identity.container_id,
        )

    async def _publish_change_async(self, record: UsageRecord) -> None:
        identity = _usage_change_identity(record)
        if self.async_workspace_changes is None or identity is None:
            return
        await self.async_workspace_changes.emit_change(
            workspace_id=record.workspace_id,
            topic=WorkspaceChangeTopic.Usage,
            change=WorkspaceChangeType.Updated,
            resource_id=record.id,
            app_id=identity.app_id,
            deployment_id=identity.deployment_id,
            stub_id=identity.stub_id,
            task_id=identity.task_id,
            container_id=identity.container_id,
        )


@dataclass(frozen=True, slots=True)
class _UsageChangeIdentity:
    app_id: str | None
    deployment_id: str | None
    stub_id: str | None
    task_id: str | None
    container_id: str | None


def _usage_change_identity(record: UsageRecord) -> _UsageChangeIdentity | None:
    """Where a record belongs in the workspace feed, or `None` to keep it out.

    Raw resource samples stay out. The records that publish are durable summary
    boundaries: a task's contribution, or a persisted volume or managed-capacity
    billing window.
    """
    if record.metric not in USAGE_CHANGE_BOUNDARY_METRICS:
        return None
    return _UsageChangeIdentity(
        app_id=_usage_identity(record, "app_id"),
        deployment_id=_usage_identity(record, UsageGroupKey.Version.value),
        stub_id=_usage_identity(record, UsageGroupKey.Workload.value),
        task_id=_usage_identity(record, "task_id"),
        container_id=_usage_identity(record, "container_id"),
    )


def _task_count_labels(
    *,
    resource_id: str,
    task_id: str,
    kind: str,
    app_id: str,
    deployment_id: str,
) -> tuple[dict[str, str], dict[str, JsonValue]]:
    metadata: dict[str, JsonValue] = {"task_id": task_id}
    labels = {"kind": kind, UsageGroupKey.Workload.value: resource_id}
    if app_id:
        metadata["app_id"] = app_id
        labels["app_id"] = app_id
    if deployment_id:
        metadata[UsageGroupKey.Version.value] = deployment_id
        labels[UsageGroupKey.Version.value] = deployment_id
    return labels, metadata


def _encode_usage_record_cursor(cursor: UsageRecordCursor) -> str:
    payload = UsageRecordCursorPayload(
        created_at=cursor.created_at,
        id=UUID(cursor.id),
    ).model_dump_json()
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _usage_identity(record: UsageRecord, key: str) -> str | None:
    value = record.metadata.get(key, record.labels.get(key))
    return value if isinstance(value, str) and value else None


def _decode_usage_record_cursor(value: str | None) -> UsageRecordCursor | None:
    if value is None or value == "":
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = UsageRecordCursorPayload.model_validate_json(
            base64.urlsafe_b64decode(padded.encode())
        )
        if payload.created_at.tzinfo is None or payload.created_at.utcoffset() is None:
            raise ValueError("cursor timestamp must include a timezone")
        return UsageRecordCursor(
            created_at=payload.created_at.astimezone(UTC),
            id=str(payload.id),
        )
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidInputError("invalid usage record cursor") from exc
