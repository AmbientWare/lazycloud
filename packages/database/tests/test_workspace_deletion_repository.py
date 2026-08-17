from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4

import pytest
from database.repositories.execution import TaskRepository
from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import AutoscalerStateRepository
from database.repositories.storage import ObjectRepository, VolumeRepository
from database.tables.apps import StubTable
from database.tables.orchestration import ContainerTable
from database.tables.identity import WorkspaceTable
from shared.autoscaler_state import (
    AutoscalerStateRecord,
    AutoscalerTargetKind,
    autoscaler_state_name,
)
from shared.errors import NotFoundError
from shared.identity import WorkspaceStatus
from shared.objects import ObjectWriteCommand
from shared.tasks import Task, TaskStatus
from shared.timestamps import utc_now
from sqlalchemy import delete
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError

from database import (
    DatabaseApplicationName,
    DatabaseClient,
    DatabaseSettings,
    WorkspaceDeletionFence,
)


def test_postgresql_workspace_name_belongs_to_one_live_workspace(
    postgres_database_url: URL,
) -> None:
    """A name is held while a workspace exists and released when it stops existing.

    Both halves are one partial unique index, so both are proven against the
    database that enforces it rather than against a mapping in Python: the row
    stays for the ledger and the audit trail that point at it, and the name does
    not stay with it.
    """
    database = DatabaseClient.from_settings(
        DatabaseSettings(
            url=postgres_database_url.render_as_string(hide_password=False),
            application_name=DatabaseApplicationName.Test,
        )
    )
    database.create_schema()
    try:
        with database.session() as session:
            original = WorkspaceRepository(session).create(name="tenant")

        with (
            pytest.raises(IntegrityError, match="uq_workspaces_name"),
            database.session() as session,
        ):
            WorkspaceRepository(session).create(name="tenant")

        with database.session() as session:
            workspaces = WorkspaceRepository(session)
            workspaces.tombstone(workspaces.mark_deleting(original))

        with database.session() as session:
            replacement = WorkspaceRepository(session).create(name="tenant")

        assert replacement.id != original.id
        with database.session() as session:
            workspaces = WorkspaceRepository(session)
            assert workspaces.by_name("tenant") == replacement
            tombstone = workspaces.get(original.id)
        assert tombstone is not None and tombstone.status is WorkspaceStatus.Deleted
    finally:
        database.dispose()


def test_postgresql_workspace_deletion_serializes_complete_attempts() -> None:
    database = _postgres_database()
    first_id = _create_workspace(database, "attempt-first")
    second_id = _create_workspace(database, "attempt-second")
    release_first = Event()
    first_locked = Event()
    same_started = Event()
    other_locked = Event()

    def hold_first() -> None:
        with WorkspaceDeletionFence(database).acquire(first_id):
            first_locked.set()
            assert release_first.wait(timeout=10)

    def acquire_same() -> None:
        same_started.set()
        with WorkspaceDeletionFence(database).acquire(first_id):
            return

    def acquire_other() -> None:
        with WorkspaceDeletionFence(database).acquire(second_id):
            other_locked.set()

    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            first = executor.submit(hold_first)
            assert first_locked.wait(timeout=10)
            same = executor.submit(acquire_same)
            assert same_started.wait(timeout=10)
            assert not same.done()
            other = executor.submit(acquire_other)
            assert other_locked.wait(timeout=10)
            other.result(timeout=10)
            release_first.set()
            first.result(timeout=10)
            same.result(timeout=10)
    finally:
        release_first.set()
        _remove_test_workspace(database, first_id)
        _remove_test_workspace(database, second_id)
        database.dispose()


def test_postgresql_workspace_deletion_fences_owned_write_races() -> None:
    database = _postgres_database()
    writer_first_workspace_id = _create_workspace(database, "writer-first")
    deletion_first_workspace_id = _create_workspace(database, "deletion-first")
    release_writer = Event()
    release_deletion = Event()

    try:
        _prove_writer_before_deletion(
            database,
            workspace_id=writer_first_workspace_id,
            release_writer=release_writer,
        )
        _prove_deletion_before_writer(
            database,
            workspace_id=deletion_first_workspace_id,
            release_deletion=release_deletion,
        )
    finally:
        release_writer.set()
        release_deletion.set()
        _remove_test_workspace(database, writer_first_workspace_id)
        _remove_test_workspace(database, deletion_first_workspace_id)
        database.dispose()


def test_postgresql_same_object_location_in_sibling_workspaces_does_not_serialize() -> None:
    database = _postgres_database()
    first_id = _create_workspace(database, "object-first")
    second_id = _create_workspace(database, "object-second")
    first_claimed = Event()
    second_claimed = Event()
    release_first = Event()
    command = ObjectWriteCommand(
        bucket="default",
        key="models/shared.bin",
        path="s3://physical/shared.bin",
        size=4,
        sha256="0" * 64,
    )

    def hold_first_claim() -> None:
        with database.session() as session:
            claim = ObjectRepository(session).begin_write(
                command,
                workspace_id=first_id,
                overwrite=False,
            )
            first_claimed.set()
            assert release_first.wait(timeout=10)
            ObjectRepository(session).abort_write(claim, workspace_id=first_id)

    def claim_sibling() -> None:
        with database.session() as session:
            claim = ObjectRepository(session).begin_write(
                command,
                workspace_id=second_id,
                overwrite=False,
            )
            second_claimed.set()
            ObjectRepository(session).abort_write(claim, workspace_id=second_id)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(hold_first_claim)
            assert first_claimed.wait(timeout=10)
            second = executor.submit(claim_sibling)
            assert second_claimed.wait(timeout=10)
            second.result(timeout=10)
            release_first.set()
            first.result(timeout=10)
    finally:
        release_first.set()
        _remove_test_workspace(database, first_id)
        _remove_test_workspace(database, second_id)
        database.dispose()


def _prove_writer_before_deletion(
    database: DatabaseClient,
    *,
    workspace_id: str,
    release_writer: Event,
) -> None:
    writer_locked = Event()
    deletion_started = Event()
    state = _autoscaler_state(workspace_id, "writer-first")

    def write() -> None:
        with database.session() as session:
            WorkspaceRepository(session).lock_active_owner(workspace_id)
            writer_locked.set()
            assert release_writer.wait(timeout=10)
            AutoscalerStateRepository(session).upsert(state)

    def delete_workspace() -> None:
        deletion_started.set()
        with database.session() as session:
            workspaces = WorkspaceRepository(session)
            workspace = workspaces.lock_for_deletion(workspace_id)
            workspaces.mark_deleting(workspace)
            workspaces.purge_owned_records(workspace_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(write)
        assert writer_locked.wait(timeout=10)
        deletion = executor.submit(delete_workspace)
        assert deletion_started.wait(timeout=10)
        assert not deletion.done()
        release_writer.set()
        writer.result(timeout=10)
        deletion.result(timeout=10)

    with database.session() as session:
        assert AutoscalerStateRepository(session).list(workspace_id=workspace_id) == []


def _prove_deletion_before_writer(
    database: DatabaseClient,
    *,
    workspace_id: str,
    release_deletion: Event,
) -> None:
    deletion_locked = Event()
    writer_started = Event()
    state = _autoscaler_state(workspace_id, "deletion-first")

    def delete_workspace() -> None:
        with database.session() as session:
            workspaces = WorkspaceRepository(session)
            workspace = workspaces.lock_for_deletion(workspace_id)
            workspaces.mark_deleting(workspace)
            deletion_locked.set()
            assert release_deletion.wait(timeout=10)
            workspaces.purge_owned_records(workspace_id)

    def write() -> None:
        writer_started.set()
        with database.session() as session:
            AutoscalerStateRepository(session).records.upsert_across_workspaces(state)

    with ThreadPoolExecutor(max_workers=2) as executor:
        deletion = executor.submit(delete_workspace)
        assert deletion_locked.wait(timeout=10)
        writer = executor.submit(write)
        assert writer_started.wait(timeout=10)
        assert not writer.done()
        release_deletion.set()
        deletion.result(timeout=10)
        with pytest.raises(NotFoundError, match="workspace not found"):
            writer.result(timeout=10)

    with database.session() as session:
        assert AutoscalerStateRepository(session).list(workspace_id=workspace_id) == []


def _postgres_database() -> DatabaseClient:
    database_url = os.environ.get("LAZYCLOUD_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("LAZYCLOUD_TEST_POSTGRES_URL is required for PostgreSQL concurrency proof")
    database = DatabaseClient.from_settings(
        DatabaseSettings(
            url=database_url,
            pool_size=6,
            max_overflow=0,
            application_name=DatabaseApplicationName.Test,
        )
    )
    database.create_schema()
    return database


def _create_workspace(database: DatabaseClient, suffix: str) -> str:
    with database.session() as session:
        workspace = WorkspaceRepository(session).create(name=f"workspace-lock-{suffix}-{uuid4()}")
    return workspace.id


def _remove_test_workspace(database: DatabaseClient, workspace_id: str) -> None:
    with database.session() as session:
        workspaces = WorkspaceRepository(session)
        workspace = workspaces.get(workspace_id)
        if workspace is None:
            return
        if workspace.status.value == "active":
            workspaces.mark_deleting(workspace)
        workspaces.purge_owned_records(workspace_id)
        workspaces.delete_identity_records(workspace_id)
        session.execute(delete(WorkspaceTable).where(WorkspaceTable.id == workspace_id))


def _autoscaler_state(workspace_id: str, target_id: str) -> AutoscalerStateRecord:
    target_kind = AutoscalerTargetKind.Endpoint
    return AutoscalerStateRecord(
        name=autoscaler_state_name(target_kind, target_id),
        workspace_id=workspace_id,
        source="endpoint",
        target_kind=target_kind,
        target_id=target_id,
        decision="hold",
    )


def test_postgresql_contended_volume_name_settles_on_the_constraint() -> None:
    """Two containers mounting one new volume name both get the volume.

    The lookup a caller does before creating is not a lock — workspace scoping
    takes `FOR KEY SHARE`, which does not serialize writers — so an autoscaler
    ramp starting several replicas at once has both read absence and both insert.
    Whichever loses violates `uq_volumes_workspace_name`, and before the savepoint
    that failure surfaced as an unmapped `IntegrityError` on a container start.

    Proven here rather than beside the volume service because it cannot be proven
    there: the shared fixture runs SQLite in memory behind a process-wide lock, so
    two writers never race and SAVEPOINT is not PostgreSQL's. Exactly one caller
    must report creating the row, since that answer is what decides whether a
    workspace change is announced and whether billing is asked for a new volume.
    """

    database = _postgres_database()
    workspace_id = _create_workspace(database, "volume-race")
    started = Event()

    def create() -> tuple[str, bool]:
        with database.session() as session:
            started.wait(timeout=10)
            record, created = VolumeRepository(session).create(
                "contended",
                workspace_id=workspace_id,
            )
        return record.id, created

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            writers = [pool.submit(create) for _ in range(2)]
            started.set()
            results = [writer.result(timeout=30) for writer in writers]

        assert sorted(created for _, created in results) == [False, True]
        assert len({volume_id for volume_id, _ in results}) == 1
        with database.session() as session:
            names = [row.name for row in VolumeRepository(session).list(workspace_id=workspace_id)]
        assert names == ["contended"]
    finally:
        _remove_test_workspace(database, workspace_id)
        database.dispose()


def test_postgresql_claimable_task_is_taken_by_exactly_one_container() -> None:
    """A runnable task goes to one container, however many ask at once.

    Pooled containers poll for their own work, so several ask for the same stub's
    tasks at the same instant. `SKIP LOCKED` is what makes that safe — the loser
    steps over a row the winner holds rather than blocking on it or taking it
    twice — and taking one twice would run a customer's function two times and
    bill for both.

    Proven against PostgreSQL because that is the guarantee: SQLite ignores
    `FOR UPDATE SKIP LOCKED` entirely, so the shared in-memory fixture would
    report this passing whether or not the clause were there.
    """

    database = _postgres_database()
    workspace_id = _create_workspace(database, "task-claim")
    stub_id = str(uuid4())
    contenders = 8
    container_ids = [str(uuid4()) for _ in range(contenders)]
    with database.session() as session:
        session.add(
            StubTable(
                id=stub_id,
                external_id=str(uuid4()),
                workspace_id=workspace_id,
                name="claimable",
                type="function",
                payload={},
            )
        )
        for container_id in container_ids:
            session.add(
                ContainerTable(
                    id=container_id,
                    workspace_id=workspace_id,
                    stub_id=stub_id,
                    name=f"container-{container_id}",
                    image="python:3.12-slim",
                    status="running",
                    payload={},
                )
            )
        session.flush()
        tasks = TaskRepository(session)
        wanted = {
            tasks.upsert(
                Task(
                    id=str(uuid4()),
                    name=f"claimable-{index}",
                    workspace_id=workspace_id,
                    stub_id=stub_id,
                    status=TaskStatus.Pending,
                    claimable_at=utc_now(),
                ),
                workspace_id=workspace_id,
            ).id
            for index in range(4)
        }
        # Waiting on an upstream: eligible in every respect except the one that
        # decides it, so a claim that ignored `claimable_at` would take it.
        blocked = tasks.upsert(
            Task(
                id=str(uuid4()),
                name="waiting-on-upstream",
                workspace_id=workspace_id,
                stub_id=stub_id,
                status=TaskStatus.Pending,
                claimable_at=None,
            ),
            workspace_id=workspace_id,
        ).id

    start = Barrier(contenders)

    def claim(container_index: int) -> list[str]:
        with database.session() as session:
            start.wait(timeout=10)
            return [
                task.id
                for task in TaskRepository(session).claim_for_stub(
                    stub_id,
                    container_id=container_ids[container_index],
                    limit=4,
                )
            ]

    try:
        with ThreadPoolExecutor(max_workers=contenders) as pool:
            claimed = [id for result in pool.map(claim, range(contenders)) for id in result]

        assert len(claimed) == len(set(claimed)), "a task was claimed more than once"
        assert set(claimed) == wanted
        assert blocked not in claimed
        with database.session() as session:
            still_waiting = TaskRepository(session).get_across_workspaces(blocked)
        assert still_waiting is not None
        assert still_waiting.container_id is None
    finally:
        _remove_test_workspace(database, workspace_id)
        database.dispose()
