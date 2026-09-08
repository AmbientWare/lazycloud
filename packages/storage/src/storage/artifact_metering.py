from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal

from database.repositories.artifacts import ArtifactRepository
from database.repositories.identity import WorkspaceRepository
from database.repositories.observability import UsageRepository
from database.types import DatabaseSession
from observability.usage_pricing import MeteredUsagePricer
from shared.artifacts import ARTIFACT_STORAGE_SUBJECT
from shared.timestamps import to_utc, utc_now
from shared.usage import (
    METERING_WINDOW_ENDED_AT_METADATA_KEY,
    METERING_WINDOW_STARTED_AT_METADATA_KEY,
    UsageMetric,
    UsageRecord,
    UsageUnit,
    usage_record_id,
)

from storage.context import StorageContext

LOGGER = logging.getLogger(__name__)


def meter_artifact(
    session: DatabaseSession,
    *,
    workspace_id: str,
    artifact_id: str,
    now: datetime,
) -> None:
    WorkspaceRepository(session).lock_storage_accounting_owner(workspace_id)
    repository = ArtifactRepository(session)
    record = repository.get(artifact_id, workspace_id=workspace_id, lock=True)
    if record is None or record.artifact_metered_at is None:
        return
    start = to_utc(record.artifact_metered_at)
    end = to_utc(now)
    elapsed_ms = (end - start) // timedelta(milliseconds=1)
    if elapsed_ms <= 0:
        return
    end = start + timedelta(milliseconds=elapsed_ms)
    usage = UsageRecord(
        id=usage_record_id(
            UsageMetric.ArtifactStorageByteSeconds.value,
            workspace_id,
            artifact_id,
            start.isoformat(),
            end.isoformat(),
        ),
        workspace_id=workspace_id,
        resource_type=ARTIFACT_STORAGE_SUBJECT,
        resource_id=artifact_id,
        metric=UsageMetric.ArtifactStorageByteSeconds,
        quantity=float(Decimal(record.size * elapsed_ms) / 1000),
        unit=UsageUnit.ByteSeconds,
        labels={
            "stub_id": ARTIFACT_STORAGE_SUBJECT,
            "storage_name": "Artifacts",
            "app_id": record.artifact_app_id or "",
            "app_name": record.artifact_app_name,
            "task_id": record.artifact_task_id or "",
        },
        metadata={
            METERING_WINDOW_STARTED_AT_METADATA_KEY: start.isoformat(),
            METERING_WINDOW_ENDED_AT_METADATA_KEY: end.isoformat(),
        },
    )
    usage_repository = UsageRepository(session)
    usage = usage_repository.append_storage(usage)
    MeteredUsagePricer(session).price(usage)
    record.artifact_metered_at = end
    repository.update(record)


def meter_due_artifacts(
    context: StorageContext, *, now: datetime | None = None, limit: int = 100
) -> tuple[int, int]:
    current = to_utc(now or utc_now())
    with context.database.session() as session:
        targets = ArtifactRepository(session).due(
            before=current - timedelta(seconds=60), limit=limit
        )
    succeeded = failed = 0
    for workspace_id, artifact_id in targets:
        try:
            with context.database.session() as session:
                meter_artifact(
                    session, workspace_id=workspace_id, artifact_id=artifact_id, now=current
                )
            succeeded += 1
        except Exception:
            failed += 1
            LOGGER.exception(
                "artifact storage metering failed",
                extra={"workspace_id": workspace_id, "artifact_id": artifact_id},
            )
    return succeeded, failed
