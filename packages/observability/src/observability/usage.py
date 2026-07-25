from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from database.repositories.apps import AppRepository, StubRepository
from database.repositories.observability import (
    UsageRecordCursor,
    UsageRepository,
    WorkerEventRepository,
)
from database.repositories.usage_billing import UsageBillingRepository
from database.types import DatabaseSession
from pydantic import JsonValue, TypeAdapter
from shared.contracts import ContractModel
from shared.errors import InvalidInputError
from shared.http.usage import UsageBillingPeriod
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

from observability.billing import (
    UsageBillingOverview,
    UsageBillingReport,
    UsageBillingWorkloads,
    UsagePriceCatalog,
    build_usage_billing_overview,
    build_usage_billing_report,
    build_usage_billing_workloads,
    default_usage_price_catalog,
)
from observability.context import ObservabilityContext
from observability.usage_exporter import UsageMetricsExporter
from observability.workspace_changes import WorkspaceChangePublisher

WORKER_EVENT_RETENTION = timedelta(days=30)
MAX_BILLING_WINDOW = timedelta(days=90)
USAGE_CHANGE_BOUNDARY_METRICS = frozenset(
    {
        UsageMetric.TaskCount,
        UsageMetric.PersistentVolumeByteSeconds,
        UsageMetric.ManagedComputeReservationCostCents,
        UsageMetric.CustomerCloudManagementCostCents,
    }
)
_USAGE_METADATA_ADAPTER = TypeAdapter(dict[str, JsonValue])


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
    exporter: UsageMetricsExporter | None = None
    price_catalog: UsagePriceCatalog = field(default_factory=default_usage_price_catalog)
    workspace_changes: WorkspaceChangePublisher | None = None

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
        export: bool = True,
    ) -> UsageRecord:
        with self.context.database.session() as session:
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
            if export:
                record = self._export_record(session, record)
        self._publish_change(record)
        return record

    def append(self, record: UsageRecord, *, export: bool = True) -> UsageRecord:
        with self.context.database.session() as session:
            saved = UsageRepository(session).append(record)
            if export:
                saved = self._export_record(session, saved)
        self._publish_change(saved)
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

    def billing_report(
        self,
        *,
        workspace_id: str,
        start: datetime,
        end: datetime,
        bucket_seconds: int,
    ) -> UsageBillingReport:
        start, end = _validated_billing_window(start, end, bucket_seconds)
        with self.context.database.session() as session:
            records = UsageBillingRepository(session).evidence(
                workspace_id=workspace_id,
                start=start,
                end=end,
            )
            return build_usage_billing_report(
                workspace_id=workspace_id,
                records=records,
                apps=AppRepository(session).list(workspace_id=workspace_id),
                stubs=StubRepository(session).list(workspace_id=workspace_id),
                start=start,
                end=end,
                bucket_seconds=bucket_seconds,
                price_catalog=self.price_catalog,
            )

    def billing_overview(
        self,
        *,
        workspace_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
        period: UsageBillingPeriod | None = None,
        bucket_seconds: int,
    ) -> UsageBillingOverview:
        start, end = _resolved_billing_window(
            start=start,
            end=end,
            period=period,
            bucket_seconds=bucket_seconds,
        )
        with self.context.database.session() as session:
            return build_usage_billing_overview(
                workspace_id=workspace_id,
                records=UsageBillingRepository(session).aggregates(
                    workspace_id=workspace_id,
                    start=start,
                    end=end,
                    bucket_seconds=bucket_seconds,
                    group_by_workload=False,
                ),
                apps=AppRepository(session).list(workspace_id=workspace_id),
                start=start,
                end=end,
                bucket_seconds=bucket_seconds,
                price_catalog=self.price_catalog,
            )

    def billing_workloads(
        self,
        *,
        workspace_id: str,
        app_id: str,
        start: datetime,
        end: datetime,
        bucket_seconds: int,
    ) -> UsageBillingWorkloads:
        start, end = _validated_billing_window(start, end, bucket_seconds)
        with self.context.database.session() as session:
            return build_usage_billing_workloads(
                workspace_id=workspace_id,
                app_id=app_id,
                records=UsageBillingRepository(session).aggregates(
                    workspace_id=workspace_id,
                    start=start,
                    end=end,
                    bucket_seconds=None,
                    group_by_workload=True,
                    app_id=app_id,
                ),
                apps=AppRepository(session).list(workspace_id=workspace_id),
                stubs=StubRepository(session).list_for_app(
                    workspace_id=workspace_id,
                    app_id=app_id,
                ),
                start=start,
                end=end,
                price_catalog=self.price_catalog,
            )

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
        metadata: dict[str, JsonValue] = {"task_id": task_id}
        labels = {"kind": kind, UsageGroupKey.Workload.value: resource_id}
        if app_id:
            metadata["app_id"] = app_id
            labels["app_id"] = app_id
        if deployment_id:
            metadata[UsageGroupKey.Version.value] = deployment_id
            labels[UsageGroupKey.Version.value] = deployment_id
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

    def _export_record(self, session: DatabaseSession, record: UsageRecord) -> UsageRecord:
        if self.exporter is None:
            return record
        metadata: dict[str, JsonValue] = {
            **record.labels,
            **_USAGE_METADATA_ADAPTER.validate_python(record.metadata),
            "workspace_id": record.workspace_id,
            "resource_type": record.resource_type,
            "resource_id": record.resource_id,
            "usage_record_id": record.id,
        }
        try:
            self.exporter.emit(
                name=record.metric.value,
                metadata=metadata,
                value=record.quantity,
            )
        except Exception as exc:
            updated = record.model_copy(
                update={
                    "metadata": {
                        **record.metadata,
                        "usage_export_error": f"{type(exc).__name__}: {exc}",
                    }
                }
            )
            return UsageRepository(session).append(updated)
        if "usage_export_error" not in record.metadata:
            return record
        updated = record.model_copy(
            update={
                "metadata": {
                    key: value
                    for key, value in record.metadata.items()
                    if key != "usage_export_error"
                }
            }
        )
        return UsageRepository(session).append(updated)

    def _publish_change(self, record: UsageRecord) -> None:
        # Raw resource samples remain out of the workspace feed. These records
        # are durable summary boundaries: task contribution or a persisted
        # volume/managed-capacity billing window.
        if self.workspace_changes is None or record.metric not in USAGE_CHANGE_BOUNDARY_METRICS:
            return
        self.workspace_changes.emit_change(
            workspace_id=record.workspace_id,
            topic=WorkspaceChangeTopic.Usage,
            change=WorkspaceChangeType.Updated,
            resource_id=record.id,
            app_id=_usage_identity(record, "app_id"),
            deployment_id=_usage_identity(record, UsageGroupKey.Version.value),
            stub_id=_usage_identity(record, UsageGroupKey.Workload.value),
            task_id=_usage_identity(record, "task_id"),
            container_id=_usage_identity(record, "container_id"),
        )


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


def _utc_billing_window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    if start.tzinfo is None or start.utcoffset() is None:
        raise InvalidInputError("usage billing start must include a timezone")
    if end.tzinfo is None or end.utcoffset() is None:
        raise InvalidInputError("usage billing end must include a timezone")
    return start.astimezone(UTC), end.astimezone(UTC)


def _resolved_billing_window(
    *,
    start: datetime | None,
    end: datetime | None,
    period: UsageBillingPeriod | None,
    bucket_seconds: int,
) -> tuple[datetime, datetime]:
    if period is not None:
        if start is not None or end is not None:
            raise InvalidInputError(
                "usage billing period cannot be combined with explicit start or end"
            )
        resolved_end = utc_now().astimezone(UTC)
        resolved_start = resolved_end.replace(
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        return _validated_billing_window(resolved_start, resolved_end, bucket_seconds)

    if start is None or end is None:
        raise InvalidInputError("usage billing requires either a period or explicit start and end")
    return _validated_billing_window(start, end, bucket_seconds)


def _validated_billing_window(
    start: datetime,
    end: datetime,
    bucket_seconds: int,
) -> tuple[datetime, datetime]:
    start, end = _utc_billing_window(start, end)
    if end <= start:
        raise InvalidInputError("usage billing end must be after start")
    if end - start > MAX_BILLING_WINDOW:
        raise InvalidInputError("usage billing window cannot exceed 90 days")
    if bucket_seconds < 300 or bucket_seconds > 86_400:
        raise InvalidInputError("usage billing bucket must be between 5 minutes and 1 day")
    return start, end
