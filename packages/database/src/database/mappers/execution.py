from __future__ import annotations

from shared.timestamps import to_utc

from database.records.execution import PodUrlRecord
from database.tables.execution import PodUrlTable


def pod_url_record_from_table(row: PodUrlTable) -> PodUrlRecord:
    return PodUrlRecord(
        id=str(row.id),
        container_id=str(row.container_id),
        port=int(row.port),
        url=row.url,
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
    )


__all__ = ["pod_url_record_from_table"]
