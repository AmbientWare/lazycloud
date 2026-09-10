from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from database.context import ServiceContext
from database.repositories.artifacts import ArtifactRepository
from database.repositories.billing_rates import PlatformRateRepository
from database.repositories.execution import TaskRepository
from database.tables.billing_ledger import BillingLedgerSegmentTable
from execution.artifacts.service import ArtifactStorageService
from shared.tasks import Task
from shared.timestamps import to_utc, utc_now
from sqlalchemy import select
from storage.artifact_metering import meter_artifact
from storage.service import ObjectStorage
from tests.fakes import FakeObjectClient

from database import DatabaseClient


def test_short_lived_artifact_deletion_settles_storage_once(
    workspace_database: DatabaseClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = ServiceContext.create(workspace_database, root=tmp_path, create_schema=False)
    artifacts = ArtifactStorageService(
        context,
        object_storage=ObjectStorage(
            context, object_client=FakeObjectClient(), default_bucket="objects"
        ),
    )
    with context.database.session() as session:
        workspace_id = context.default_workspace_id(session)
        task_id = str(uuid4())
        TaskRepository(session).upsert(Task(id=task_id, name="produce", workspace_id=workspace_id))
        PlatformRateRepository(session).publish(
            pricing_version="artifact-test",
            effective_at=utc_now() - timedelta(days=1),
            nanos_per_egress_byte=Decimal(0),
            nanos_per_volume_byte_second=Decimal(1),
        )
    saved = artifacts.save(
        workspace_id=workspace_id,
        task_id=task_id,
        filename="result.txt",
        content=b"1234567890",
        retention_seconds=None,
    )
    with context.database.session() as session:
        record = ArtifactRepository(session).get(saved.id, workspace_id=workspace_id)
        assert record is not None and record.artifact_metered_at is not None
        start = to_utc(record.artifact_metered_at)
        meter_artifact(
            session,
            workspace_id=workspace_id,
            artifact_id=saved.id,
            now=start + timedelta(seconds=2),
        )
    monkeypatch.setattr("storage.service.utc_now", lambda: start + timedelta(seconds=3))
    artifacts.delete(workspace_id=workspace_id, artifact_id=saved.id)
    artifacts.delete(workspace_id=workspace_id, artifact_id=saved.id)
    with context.database.session() as session:
        costs = list(
            session.scalars(
                select(BillingLedgerSegmentTable).where(
                    BillingLedgerSegmentTable.subject_id == saved.id
                )
            )
        )
        assert sum(row.cost_nanos for row in costs) == 30
        assert {row.dimension for row in costs} == {"volume_storage"}
        assert ArtifactRepository(session).get(saved.id, workspace_id=workspace_id) is None
