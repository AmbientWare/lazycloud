from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from database.repositories.identity import WorkspaceRepository
from database.repositories.image_build_dispatch import ImageBuildDispatchRepository
from database.repositories.image_build_logs import ImageBuildLogRepository
from database.repositories.images import ImageBuildRepository, ImageRepository
from database.tables.images import ImageBuildTable
from shared.errors import ConflictError, NotFoundError
from shared.image_building.authoring import ImageSpec
from shared.image_building.records import BuildStatus, ImageBuildPhase, ImageBuildRecord
from shared.timestamps import utc_now
from sqlalchemy import select
from sqlalchemy.engine import URL

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings


def test_build_dispatch_is_atomic_and_expired_owner_cannot_acknowledge(
    migrated_database_url: URL,
) -> None:
    database = DatabaseClient.from_settings(
        DatabaseSettings(
            url=migrated_database_url.render_as_string(hide_password=False),
            application_name=DatabaseApplicationName.Test,
        )
    )
    try:
        with database.session() as session:
            workspace = WorkspaceRepository(session).create(name="dispatch-owner")
        record = ImageBuildRecord(
            id=str(uuid4()),
            image=ImageSpec(base="scratch", ignore_python=True),
            fingerprint="atomic-dispatch",
        )
        now = utc_now()
        with (
            pytest.raises(RuntimeError, match="submission interrupted"),
            database.session() as session,
        ):
            ImageBuildRepository(session).upsert(record, workspace_id=workspace.id)
            ImageBuildDispatchRepository(session).enqueue(record.id, "opaque dispatch", now=now)
            raise RuntimeError("submission interrupted")
        with database.session() as session:
            assert ImageBuildRepository(session).get(record.id, workspace_id=workspace.id) is None
            ImageBuildRepository(session).upsert(record, workspace_id=workspace.id)
            ImageBuildDispatchRepository(session).enqueue(record.id, "opaque dispatch", now=now)
        with database.session() as session:
            [old] = ImageBuildDispatchRepository(session).claim_due(now=now, limit=1)
        with database.session() as session:
            assert ImageBuildDispatchRepository(session).claim_due(now=now, limit=1) == []
        later = now + timedelta(seconds=31)
        with database.session() as session:
            [current] = ImageBuildDispatchRepository(session).claim_due(now=later, limit=1)
            ImageBuildDispatchRepository(session).complete(old, now=later)
        with database.session() as session:
            row = session.scalar(select(ImageBuildTable).where(ImageBuildTable.id == record.id))
            assert row is not None and row.dispatched_at is None
            ImageBuildDispatchRepository(session).complete(current, now=later)
        with database.session() as session:
            assert ImageBuildDispatchRepository(session).claim_due(now=later, limit=1) == []
            assert (
                ImageBuildDispatchRepository(session).payload(record.id, workspace_id=str(uuid4()))
                is None
            )
            assert (
                ImageBuildDispatchRepository(session).payload(record.id, workspace_id=workspace.id)
                == "opaque dispatch"
            )
    finally:
        database.dispose()


def test_build_logs_replay_by_sequence_without_repeating_or_skipping_output(
    migrated_database_url: URL,
) -> None:
    database = DatabaseClient.from_settings(
        DatabaseSettings(
            url=migrated_database_url.render_as_string(hide_password=False),
            application_name=DatabaseApplicationName.Test,
        )
    )
    try:
        with database.session() as session:
            workspace = WorkspaceRepository(session).create(name="log-owner")
            record = ImageBuildRecord(
                id=str(uuid4()),
                image=ImageSpec(base="scratch", ignore_python=True),
                fingerprint="log-replay",
            )
            ImageBuildRepository(session).upsert(record, workspace_id=workspace.id)
            logs = ImageBuildLogRepository(session)
            assert (
                logs.append(
                    record.id, workspace_id=workspace.id, after=0, messages=["same", "same"]
                )
                == 2
            )
            assert (
                logs.append(
                    record.id, workspace_id=workspace.id, after=0, messages=["same", "same"]
                )
                == 2
            )
            assert (
                logs.append(record.id, workspace_id=workspace.id, after=2, messages=["last"]) == 3
            )
            assert logs.page(record.id, after=1) == [(2, "same"), (3, "last")]
        with pytest.raises(ConflictError, match="gap"), database.session() as session:
            ImageBuildLogRepository(session).append(
                record.id, workspace_id=workspace.id, after=4, messages=["gap"]
            )
        with pytest.raises(NotFoundError, match="does not exist"), database.session() as session:
            ImageBuildLogRepository(session).append(
                record.id, workspace_id=str(uuid4()), after=0, messages=["foreign"]
            )
    finally:
        database.dispose()


def test_publication_commits_current_image_metadata_with_terminal_build(
    migrated_database_url: URL,
) -> None:
    database = DatabaseClient.from_settings(
        DatabaseSettings(
            url=migrated_database_url.render_as_string(hide_password=False),
            application_name=DatabaseApplicationName.Test,
        )
    )
    try:
        with database.session() as session:
            workspace = WorkspaceRepository(session).create(name="publication-owner")
            build = ImageBuildRecord(
                id=str(uuid4()),
                image_id="img_publication",
                image=ImageSpec(base="scratch", ignore_python=True),
                fingerprint="publication-transaction",
            )
            builds = ImageBuildRepository(session)
            builds.upsert(build, workspace_id=workspace.id)
            assert builds.claim_publication(
                build.id, workspace_id=workspace.id, claim_id="publisher"
            )
        completed = build.model_copy(
            update={
                "status": BuildStatus.Complete,
                "phase": ImageBuildPhase.Complete,
                "finished_at": utc_now(),
            }
        )
        with pytest.raises(RuntimeError, match="interrupted"), database.session() as session:
            ImageBuildRepository(session).finalize_publication(
                completed,
                workspace_id=workspace.id,
                claim_id="publisher",
                clip_version=2,
                archive_published=True,
            )
            raise RuntimeError("interrupted")
        with database.session() as session:
            unchanged = ImageBuildRepository(session).get(build.id, workspace_id=workspace.id)
            assert unchanged is not None and unchanged.status is BuildStatus.Pending
            assert (
                ImageRepository(session).get("img_publication", workspace_id=workspace.id) is None
            )
            ImageBuildRepository(session).finalize_publication(
                completed,
                workspace_id=workspace.id,
                claim_id="publisher",
                clip_version=2,
                archive_published=True,
            )
        with database.session() as session:
            saved = ImageBuildRepository(session).get(build.id, workspace_id=workspace.id)
            metadata = ImageRepository(session).get("img_publication", workspace_id=workspace.id)
            assert saved is not None and saved.status is BuildStatus.Complete
            assert metadata is not None and metadata.clip_version == 2
    finally:
        database.dispose()
