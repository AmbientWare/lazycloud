from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from database.mappers.observability import (
    usage_record_from_table,
    worker_event_from_table,
    write_usage_row,
)
from database.repositories.identity import WorkspaceRepository
from database.tables.observability import (
    UsageRecordTable,
    WorkerEventTable,
)
from pydantic import BaseModel, JsonValue, TypeAdapter
from shared.errors import ConflictError
from shared.usage import (
    UsageAggregation,
    UsageGroupKey,
    UsageMetric,
    UsageRecord,
    UsageUnit,
)
from shared.usage_query import UsageQuery
from shared.worker_events import WorkerEventFilter, WorkerEventRecord
from sqlalchemy import CursorResult, Float, and_, delete, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


class UsageAggregationResult(BaseModel):
    workspace_id: str
    metric: UsageMetric
    unit: UsageUnit
    quantity: float


@dataclass(slots=True)
class WorkerEventRepository:
    session: Session

    def append(self, record: WorkerEventRecord) -> WorkerEventRecord:
        row = self.session.get(WorkerEventTable, record.id)
        if row is None:
            row = WorkerEventTable(id=record.id)
            self.session.add(row)
        row.worker_id = record.worker_id
        row.event_type = record.event_type
        row.resource_id = record.resource_id
        row.event_data = dict(record.payload)
        row.created_at = record.created_at
        self.session.flush()
        return worker_event_from_table(row)

    def list(
        self,
        *,
        filter: WorkerEventFilter | None = None,
        limit: int | None = None,
    ) -> list[WorkerEventRecord]:
        """SQL-filtered worker events in descending `created_at` order."""
        if limit is not None and limit <= 0:
            return []
        current_filter = filter or WorkerEventFilter()
        statement = select(WorkerEventTable)
        if current_filter.worker_id is not None:
            statement = statement.where(WorkerEventTable.worker_id == current_filter.worker_id)
        if current_filter.event_type is not None:
            statement = statement.where(WorkerEventTable.event_type == current_filter.event_type)
        if current_filter.resource_id is not None:
            statement = statement.where(WorkerEventTable.resource_id == current_filter.resource_id)
        if current_filter.since is not None:
            statement = statement.where(WorkerEventTable.created_at >= current_filter.since)
        statement = statement.order_by(
            WorkerEventTable.created_at.desc(),
            WorkerEventTable.id.asc(),
        )
        if limit is not None:
            statement = statement.limit(limit)
        return [worker_event_from_table(row) for row in self.session.scalars(statement)]

    def prune(self, *, older_than: datetime) -> int:
        result = self.session.execute(
            delete(WorkerEventTable).where(WorkerEventTable.created_at < older_than)
        )
        self.session.flush()
        return int(result.rowcount) if isinstance(result, CursorResult) else 0


@dataclass(frozen=True, slots=True)
class UsageRecordCursor:
    created_at: datetime
    id: str


@dataclass(frozen=True, slots=True)
class UsageRecordPage:
    data: tuple[UsageRecord, ...]
    next: UsageRecordCursor | None = None


@dataclass(slots=True)
class UsageRepository:
    session: Session

    def append(self, record: UsageRecord) -> UsageRecord:
        WorkspaceRepository(self.session).lock_active_owner(record.workspace_id)
        return self._save(record)

    def append_storage(self, record: UsageRecord) -> UsageRecord:
        if record.metric not in (
            UsageMetric.PersistentVolumeByteSeconds,
            UsageMetric.ArtifactStorageByteSeconds,
        ):
            raise ConflictError("storage accounting requires a storage metric")
        WorkspaceRepository(self.session).lock_storage_accounting_owner(record.workspace_id)
        return self._save(record)

    def _save(self, record: UsageRecord) -> UsageRecord:
        record = UsageRecord.model_validate(dict(record))
        row = self.session.get(UsageRecordTable, record.id)
        if row is None:
            row = UsageRecordTable(id=record.id)
            self.session.add(row)
        elif row.workspace_id != record.workspace_id:
            raise ConflictError("usage ownership cannot change")
        write_usage_row(row, record)
        self.session.flush()
        return usage_record_from_table(row)

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
        metadata: dict[str, JsonValue] | None = None,
    ) -> UsageRecord:
        return self.append(
            UsageRecord(
                id=id or str(uuid4()),
                workspace_id=workspace_id,
                resource_type=resource_type,
                resource_id=resource_id,
                metric=metric,
                quantity=quantity,
                unit=unit,
                labels=labels or {},
                metadata=metadata or {},
            )
        )

    def list(
        self,
        query: UsageQuery | None = None,
        *,
        workspace_id: str,
    ) -> list[UsageRecord]:
        return self._list(query, workspace_id=workspace_id)

    def list_across_workspaces(
        self,
        query: UsageQuery | None = None,
    ) -> list[UsageRecord]:
        """System/billing listing over every workspace's usage records."""
        return self._list(query, workspace_id=None)

    def _list(
        self,
        query: UsageQuery | None,
        *,
        workspace_id: str | None,
    ) -> list[UsageRecord]:
        effective_workspace = workspace_id or (query.workspace_id if query is not None else None)
        statement = select(UsageRecordTable)
        if effective_workspace is not None:
            statement = statement.where(UsageRecordTable.workspace_id == effective_workspace)
        if query is not None:
            if query.resource_type is not None:
                statement = statement.where(UsageRecordTable.resource_type == query.resource_type)
            if query.resource_id is not None:
                statement = statement.where(UsageRecordTable.resource_id == query.resource_id)
            if query.created_after is not None:
                statement = statement.where(UsageRecordTable.created_at >= query.created_after)
            if query.created_before is not None:
                statement = statement.where(UsageRecordTable.created_at < query.created_before)
            for key, value in query.labels.items():
                statement = statement.where(_usage_label_expression(key) == value)
        statement = statement.order_by(
            UsageRecordTable.created_at.desc(),
            UsageRecordTable.id.asc(),
        )
        return [usage_record_from_table(row) for row in self.session.scalars(statement)]

    def page(
        self,
        query: UsageQuery,
        *,
        workspace_id: str,
        limit: int,
        metric: UsageMetric | None = None,
        cursor: UsageRecordCursor | None = None,
    ) -> UsageRecordPage:
        statement = select(UsageRecordTable).where(UsageRecordTable.workspace_id == workspace_id)
        for key, value in query.labels.items():
            statement = statement.where(_usage_label_expression(key) == value)
        if query.resource_type is not None:
            statement = statement.where(UsageRecordTable.resource_type == query.resource_type)
        if query.resource_id is not None:
            statement = statement.where(UsageRecordTable.resource_id == query.resource_id)
        if query.created_after is not None:
            statement = statement.where(UsageRecordTable.created_at >= query.created_after)
        if query.created_before is not None:
            statement = statement.where(UsageRecordTable.created_at < query.created_before)
        if metric is not None:
            statement = statement.where(UsageRecordTable.metric == metric.value)
        if cursor is not None:
            statement = statement.where(
                or_(
                    UsageRecordTable.created_at < cursor.created_at,
                    and_(
                        UsageRecordTable.created_at == cursor.created_at,
                        UsageRecordTable.id > cursor.id,
                    ),
                )
            )
        rows = list(
            self.session.scalars(
                statement.order_by(
                    UsageRecordTable.created_at.desc(),
                    UsageRecordTable.id.asc(),
                ).limit(limit + 1)
            )
        )
        page_rows = rows[:limit]
        next_cursor = None
        if len(rows) > limit and page_rows:
            last = page_rows[-1]
            created_at = (
                last.created_at
                if last.created_at.tzinfo is not None
                else last.created_at.replace(tzinfo=UTC)
            )
            next_cursor = UsageRecordCursor(created_at=created_at, id=str(last.id))
        return UsageRecordPage(
            data=tuple(usage_record_from_table(row) for row in page_rows),
            next=next_cursor,
        )

    def aggregate(
        self,
        *,
        query: UsageQuery | None = None,
        metric: UsageMetric | None = None,
        group_by: tuple[UsageGroupKey, ...] = (),
    ) -> list[UsageAggregation]:
        unit = UsageRecordTable.unit.label("unit")
        group_values = tuple(_usage_group_expression(key) for key in group_by)
        statement = select(
            UsageRecordTable.workspace_id,
            UsageRecordTable.metric,
            unit,
            *group_values,
            func.sum(UsageRecordTable.quantity, type_=Float).label("quantity"),
        )
        if query is not None:
            if query.workspace_id is not None:
                statement = statement.where(UsageRecordTable.workspace_id == query.workspace_id)
            if query.resource_type is not None:
                statement = statement.where(UsageRecordTable.resource_type == query.resource_type)
            if query.resource_id is not None:
                statement = statement.where(UsageRecordTable.resource_id == query.resource_id)
            if query.created_after is not None:
                statement = statement.where(UsageRecordTable.created_at >= query.created_after)
            if query.created_before is not None:
                statement = statement.where(UsageRecordTable.created_at < query.created_before)
            for key, value in query.labels.items():
                statement = statement.where(_usage_label_expression(key) == value)
        if metric is not None:
            statement = statement.where(UsageRecordTable.metric == metric.value)
        statement = statement.group_by(
            UsageRecordTable.workspace_id,
            UsageRecordTable.metric,
            unit,
            *group_values,
        ).order_by(
            UsageRecordTable.workspace_id,
            UsageRecordTable.metric,
            unit,
            *group_values,
        )
        results: list[UsageAggregation] = []
        for raw in self.session.execute(statement).mappings():
            raw_json = _JSON_OBJECT_ADAPTER.validate_python(raw)
            row = UsageAggregationResult.model_validate(raw_json)
            results.append(
                UsageAggregation(
                    workspace_id=row.workspace_id,
                    metric=row.metric,
                    quantity=row.quantity,
                    unit=row.unit,
                    labels={key.value: _optional_text(raw_json.get(key.value)) for key in group_by},
                )
            )
        return results


def _usage_label_expression(key: str) -> ColumnElement[str | None]:
    columns = {
        "app_id": UsageRecordTable.app_id,
        "stub_id": UsageRecordTable.stub_id,
        "deployment_id": UsageRecordTable.deployment_id,
        "gpu": UsageRecordTable.gpu,
        "task_id": UsageRecordTable.task_id,
        "worker_id": UsageRecordTable.worker_id,
        "container_id": UsageRecordTable.container_id,
    }
    column = columns.get(key)
    return (
        column.__clause_element__()
        if column is not None
        else UsageRecordTable.labels[key].as_string()
    )


def _usage_group_expression(group_by: UsageGroupKey) -> ColumnElement[str | None]:
    return func.coalesce(_usage_label_expression(group_by.value), "").label(group_by.value)


def _optional_text(value: JsonValue) -> str:
    return value if isinstance(value, str) else ""
