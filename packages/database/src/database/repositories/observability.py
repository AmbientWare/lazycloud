from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from database.repositories.common import (
    GlobalTableRepository,
    TableRepositoryConfig,
    WorkspaceTableRepository,
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
from sqlalchemy import CursorResult, Float, String, and_, delete, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
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

    @property
    def records(self) -> GlobalTableRepository[WorkerEventRecord]:
        return GlobalTableRepository(
            self.session,
            TableRepositoryConfig(WorkerEventTable, WorkerEventRecord),
        )

    def append(self, record: WorkerEventRecord) -> WorkerEventRecord:
        return self.records.upsert(record)

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
        return [
            WorkerEventRecord.model_validate(row.payload) for row in self.session.scalars(statement)
        ]

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

    @property
    def records(self) -> WorkspaceTableRepository[UsageRecord]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(UsageRecordTable, UsageRecord),
        )

    def append(self, record: UsageRecord) -> UsageRecord:
        return self.records.upsert(
            record,
            workspace_id=record.workspace_id,
        )

    def append_storage(self, record: UsageRecord) -> UsageRecord:
        if record.metric not in (
            UsageMetric.PersistentVolumeByteSeconds,
            UsageMetric.ArtifactStorageByteSeconds,
        ):
            raise ConflictError("storage accounting requires a storage metric")
        WorkspaceRepository(self.session).lock_storage_accounting_owner(record.workspace_id)
        row = self.session.get(UsageRecordTable, record.id)
        if row is None:
            row = UsageRecordTable(
                id=record.id,
                workspace_id=record.workspace_id,
                resource_type=record.resource_type,
                resource_id=record.resource_id,
                metric=record.metric.value,
                quantity=record.quantity,
                payload=record.model_dump(mode="json"),
                created_at=record.created_at,
                updated_at=record.created_at,
            )
            self.session.add(row)
        else:
            row.resource_type = record.resource_type
            row.resource_id = record.resource_id
            row.metric = record.metric.value
            row.quantity = record.quantity
            row.payload = record.model_dump(mode="json")
            flag_modified(row, "payload")
        self.session.flush()
        return record

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
        if id:
            return self.append(
                UsageRecord(
                    id=id,
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
        json_labels: dict[str, JsonValue] = {}
        json_labels.update(labels or {})
        payload: dict[str, JsonValue] = {
            "workspace_id": workspace_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "metric": metric,
            "quantity": quantity,
            "unit": unit,
            "labels": json_labels,
            "metadata": metadata or {},
        }
        return self.records.create(
            payload,
            workspace_id=workspace_id,
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
                statement = statement.where(_usage_json_text(self.session, "labels", key) == value)
        statement = statement.order_by(
            UsageRecordTable.created_at.desc(),
            UsageRecordTable.id.asc(),
        )
        return [UsageRecord.model_validate(row.payload) for row in self.session.scalars(statement)]

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
            data=tuple(UsageRecord.model_validate(row.payload) for row in page_rows),
            next=next_cursor,
        )

    def aggregate(
        self,
        *,
        query: UsageQuery | None = None,
        metric: UsageMetric | None = None,
        group_by: tuple[UsageGroupKey, ...] = (),
    ) -> list[UsageAggregation]:
        unit = _usage_json_text(self.session, "unit").label("unit")
        group_values = tuple(_usage_group_expression(self.session, key) for key in group_by)
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
                statement = statement.where(_usage_json_text(self.session, "labels", key) == value)
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


def _usage_group_expression(session: Session, group_by: UsageGroupKey) -> ColumnElement[str]:
    label = _usage_json_text(session, "labels", group_by.value)
    metadata = _usage_json_text(session, "metadata", group_by.value)
    return func.coalesce(func.nullif(label, ""), metadata, "").label(group_by.value)


def _usage_json_text(session: Session, *path: str) -> ColumnElement[str]:
    if session.get_bind().dialect.name == "postgresql":
        return func.jsonb_extract_path_text(UsageRecordTable.payload, *path, type_=String)
    json_path = "$." + ".".join(path)
    return func.json_extract(UsageRecordTable.payload, json_path, type_=String)


def _optional_text(value: JsonValue) -> str:
    return value if isinstance(value, str) else ""
