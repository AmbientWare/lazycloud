from __future__ import annotations

from shared.timestamps import to_utc
from shared.usage import (
    METERING_WINDOW_ENDED_AT_METADATA_KEY,
    METERING_WINDOW_STARTED_AT_METADATA_KEY,
    UsageMetric,
    UsageRecord,
    UsageUnit,
    metering_instant,
)
from shared.worker_events import WorkerEventRecord

from database.tables.observability import UsageRecordTable, WorkerEventTable


def worker_event_from_table(row: WorkerEventTable) -> WorkerEventRecord:
    return WorkerEventRecord(
        id=str(row.id),
        worker_id=row.worker_id,
        event_type=row.event_type,
        resource_id=row.resource_id,
        payload=dict(row.event_data),
        created_at=to_utc(row.created_at),
    )


def usage_record_from_table(row: UsageRecordTable) -> UsageRecord:
    labels = dict(row.labels)
    for key, value in (
        ("app_id", row.app_id),
        ("stub_id", row.stub_id),
        ("deployment_id", row.deployment_id),
        ("gpu", row.gpu),
        ("task_id", row.task_id),
        ("worker_id", row.worker_id),
        ("container_id", row.container_id),
    ):
        if value is not None:
            labels[key] = value
    metadata = dict(row.metadata_json)
    if row.metering_started_at is not None:
        metadata[METERING_WINDOW_STARTED_AT_METADATA_KEY] = to_utc(
            row.metering_started_at
        ).isoformat()
    if row.metering_ended_at is not None:
        metadata[METERING_WINDOW_ENDED_AT_METADATA_KEY] = to_utc(row.metering_ended_at).isoformat()
    return UsageRecord(
        id=str(row.id),
        workspace_id=str(row.workspace_id),
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        metric=UsageMetric(row.metric),
        quantity=row.quantity,
        unit=UsageUnit(row.unit),
        labels=labels,
        metadata=metadata,
        created_at=to_utc(row.created_at),
    )


def write_usage_row(row: UsageRecordTable, record: UsageRecord) -> None:
    row.workspace_id = record.workspace_id
    row.resource_type = record.resource_type
    row.resource_id = record.resource_id
    row.metric = record.metric.value
    row.quantity = record.quantity
    row.unit = record.unit.value
    row.created_at = record.created_at
    row.labels = dict(record.labels)
    row.app_id = row.labels.pop("app_id", None)
    row.stub_id = row.labels.pop("stub_id", None)
    row.deployment_id = row.labels.pop("deployment_id", None)
    row.gpu = row.labels.pop("gpu", None)
    row.task_id = row.labels.pop("task_id", None)
    row.worker_id = row.labels.pop("worker_id", None)
    row.container_id = row.labels.pop("container_id", None)
    row.metadata_json = dict(record.metadata)
    row.metering_started_at = metering_instant(
        row.metadata_json.get(METERING_WINDOW_STARTED_AT_METADATA_KEY)
    )
    row.metering_ended_at = metering_instant(
        row.metadata_json.get(METERING_WINDOW_ENDED_AT_METADATA_KEY)
    )
    if row.metering_started_at is not None:
        del row.metadata_json[METERING_WINDOW_STARTED_AT_METADATA_KEY]
    if row.metering_ended_at is not None:
        del row.metadata_json[METERING_WINDOW_ENDED_AT_METADATA_KEY]
