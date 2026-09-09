from __future__ import annotations

from dataclasses import dataclass

from database.tables.storage_access import StorageAccessTable
from shared.storage_access import StorageAccessObservation
from shared.usage import usage_record_id
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session


@dataclass(slots=True)
class StorageAccessRepository:
    session: Session

    def append(
        self,
        observation: StorageAccessObservation,
        *,
        workspace_id: str | None,
    ) -> bool:
        identity = usage_record_id(
            "storage-access",
            observation.provider,
            observation.bucket,
            observation.request_id,
            observation.operation,
        )
        return (
            self.session.scalar(
                insert(StorageAccessTable)
                .values(
                    id=identity,
                    provider=observation.provider,
                    bucket=observation.bucket,
                    request_id=observation.request_id,
                    operation=observation.operation,
                    request_class=observation.request_class.value,
                    occurred_at=observation.occurred_at,
                    status_code=observation.status_code,
                    response_bytes=observation.response_bytes,
                    source_region=observation.source_region,
                    transfer_evidence=observation.transfer_evidence.value,
                    workspace_id=workspace_id,
                )
                .on_conflict_do_nothing(index_elements=[StorageAccessTable.id])
                .returning(StorageAccessTable.id)
            )
            is not None
        )
