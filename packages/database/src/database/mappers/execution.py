from __future__ import annotations

from datetime import UTC, datetime

from database.records.execution import PodUrlRecord
from database.tables.execution import PodUrlTable


def pod_url_record_from_table(row: PodUrlTable) -> PodUrlRecord:
    return PodUrlRecord(
        id=str(row.id),
        container_id=str(row.container_id),
        port=int(row.port),
        url=row.url,
        created_at=_utc_datetime(row.created_at),
        updated_at=_utc_datetime(row.updated_at),
    )


def _utc_datetime(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


__all__ = ["pod_url_record_from_table"]
