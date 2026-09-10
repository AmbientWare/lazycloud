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
from database.repositories.identity import WorkspaceMemberRepository
from database.tables.billing import BillingAccountTable
from database.tables.billing_ledger import BillingLedgerSegmentTable
from execution.artifacts.service import ArtifactStorageService
from shared.billing_plans import BillingPlanId
from shared.errors import NotFoundError
from shared.tasks import Task
from shared.timestamps import to_utc, utc_now
from sqlalchemy import select, update
from storage.artifact_metering import meter_artifact
from storage.image_archive import ImageArchiveSettings
from storage.retention import RetentionConfig, RetentionService
from storage.service import CacheStorage, MountedCacheClient, MountedCacheSettings, ObjectStorage
from tests.fakes import FakeObjectClient

from database import DatabaseClient


@pytest.mark.parametrize(
    ("plan", "complimentary", "days"),
    [
        (BillingPlanId.Free, False, 1),
        (BillingPlanId.Team, False, 30),
        (BillingPlanId.Business, False, 90),
        (BillingPlanId.Free, True, 30),
    ],
)
def test_plan_artifacts_expire_and_cleanup_preserves_later_uploads(
    workspace_database: DatabaseClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    plan: BillingPlanId,
    complimentary: bool,
    days: int,
) -> None:
    context = ServiceContext.create(workspace_database, root=tmp_path, create_schema=False)
    objects = ObjectStorage(context, object_client=FakeObjectClient(), default_bucket="objects")
    artifacts = ArtifactStorageService(context, object_storage=objects)
    stored_at = utc_now()
    monkeypatch.setattr("database.repositories.storage.utc_now", lambda: stored_at)
    with context.database.session() as session:
        workspace_id = context.default_workspace_id(session)
        owner = WorkspaceMemberRepository(session).owner_user_id(workspace_id)
        session.execute(
            update(BillingAccountTable)
            .where(BillingAccountTable.user_id == owner)
            .values(
                plan=plan.value,
                subscription_terms_version=None,
                complimentary_since=stored_at if complimentary else None,
            )
        )
        task_id = str(uuid4())
        TaskRepository(session).upsert(Task(id=task_id, name="produce", workspace_id=workspace_id))
    saved = artifacts.save(
        workspace_id=workspace_id, task_id=task_id, filename="report", content=b"report"
    )
    assert saved.expires_at == stored_at + timedelta(days=days)
    assert artifacts.summary(workspace_id=workspace_id).retention_seconds == days * 86400
    stored_at += timedelta(hours=1)
    later = artifacts.save(
        workspace_id=workspace_id, task_id=task_id, filename="later", content=b"later"
    )
    monkeypatch.setattr("execution.artifacts.service.utc_now", lambda: saved.expires_at)
    with pytest.raises(NotFoundError, match="expired"):
        artifacts.read_content(
            workspace_id=workspace_id, task_id=task_id, artifact_id=saved.id, filename="report"
        )
    with pytest.raises(NotFoundError, match="expired"):
        artifacts.public_url(
            workspace_id=workspace_id, task_id=task_id, artifact_id=saved.id, filename="report"
        )
    retention = RetentionService(
        context=context,
        object_storage=objects,
        cache_storage=CacheStorage(
            context, cache_client=MountedCacheClient(MountedCacheSettings(root=tmp_path / "cache"))
        ),
        config=RetentionConfig(checkpoint_bucket="objects"),
        image_archive_settings=ImageArchiveSettings(bucket="image-archives"),
    )
    result = retention.reconcile(active_recent_stub_keys=[], now=saved.expires_at)
    assert result.task_artifacts_removed == 1
    assert artifacts.read_content(
        workspace_id=workspace_id, task_id=task_id, artifact_id=later.id, filename="later"
    )[0] == b"later"
    with context.database.session() as session:
        assert ArtifactRepository(session).get(saved.id, workspace_id=workspace_id) is None


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
