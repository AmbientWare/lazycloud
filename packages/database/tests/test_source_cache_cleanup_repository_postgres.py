from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from database.repositories.identity import WorkspaceRepository
from database.repositories.source_cache import SourceCacheCleanupRepository
from database.tables.source_cache import SourceCacheCleanupTargetTable
from shared.errors import ConflictError
from shared.source_cache_cleanup import (
    SourceCacheCleanupCompletionReason,
    SourceCacheCleanupErrorCode,
    SourceCacheCleanupStatus,
    WorkerCacheGenerationState,
)
from sqlalchemy import inspect, update
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError

from database import (
    DatabaseApplicationName,
    DatabaseClient,
    DatabaseSettings,
    bootstrap_database,
)


def test_postgresql_source_cache_cleanup_lifecycle_is_fenced_and_restart_safe(
    postgres_database_url: URL,
) -> None:
    database_url = postgres_database_url.render_as_string(hide_password=False)
    bootstrap_database(database_url)
    database = _client(database_url, pool_size=2)
    started_at = datetime(2026, 7, 21, 12, tzinfo=UTC)
    generation_id = str(uuid4())
    source_object_id = str(uuid4())
    try:
        with database.session() as session:
            workspace = WorkspaceRepository(session).create(name="cleanup-lifecycle")
            repository = SourceCacheCleanupRepository(session)
            generation = repository.register_generation(
                generation_id,
                worker_id="worker-a",
                storage_id="storage-a",
                workspace_id=None,
                now=started_at,
            )
            assert generation.state is WorkerCacheGenerationState.Initializing
            assert generation.session_fence == 1
            with pytest.raises(ConflictError, match="storage identity is already active"):
                repository.register_generation(
                    str(uuid4()),
                    worker_id="worker-b",
                    storage_id="storage-a",
                    workspace_id=None,
                    now=started_at,
                )
            with pytest.raises(ConflictError, match="workspace scope changed"):
                repository.register_generation(
                    generation_id,
                    worker_id="worker-a",
                    storage_id="storage-a",
                    workspace_id=workspace.id,
                    now=started_at,
                )
            empty_summary = repository.summarize(workspace_id=workspace.id)
            assert empty_summary.pending_count == 0
            assert empty_summary.claimed_count == 0
            assert empty_summary.completed_count == 0
            assert empty_summary.generations_pending == 0
            assert empty_summary.oldest_pending_at is None
            assert empty_summary.complete
            activated = repository.activate_if_drained(
                generation.id,
                worker_id=generation.worker_id,
                session_fence=generation.session_fence,
                now=started_at,
            )
            assert activated is not None
            assert activated.state is WorkerCacheGenerationState.Available

            assert (
                repository.add_targets(
                    workspace_id=workspace.id,
                    source_object_ids=[source_object_id, source_object_id],
                    now=started_at + timedelta(seconds=1),
                )
                == 1
            )
            assert (
                repository.add_targets(
                    workspace_id=workspace.id,
                    source_object_ids=[source_object_id],
                    now=started_at + timedelta(seconds=2),
                )
                == 0
            )
            first_targets = repository.list_targets(workspace_id=workspace.id)
            assert len(first_targets) == 1
            assert first_targets[0].status is SourceCacheCleanupStatus.Pending
            with pytest.raises(IntegrityError), session.begin_nested():
                session.execute(
                    update(SourceCacheCleanupTargetTable)
                    .where(SourceCacheCleanupTargetTable.id == first_targets[0].id)
                    .values(last_error_code="filesystem:/private/path")
                )
            draining = repository.get_generation(generation_id)
            assert draining is not None
            assert draining.state is WorkerCacheGenerationState.Draining

            old_claim = repository.claim_due(
                generation_id,
                worker_id="worker-a",
                session_fence=1,
                now=started_at + timedelta(seconds=3),
                lease_until=started_at + timedelta(minutes=5),
                limit=10,
            )[0]
            assert old_claim.attempt_count == 1
            claimed_summary = repository.summarize(workspace_id=workspace.id)
            assert claimed_summary.pending_count == 0
            assert claimed_summary.claimed_count == 1
            assert claimed_summary.completed_count == 0
            assert claimed_summary.generations_pending == 1
            assert claimed_summary.oldest_pending_at == started_at + timedelta(seconds=1)
            assert not claimed_summary.complete

        with database.session() as session:
            repository = SourceCacheCleanupRepository(session)
            with pytest.raises(ConflictError, match="storage identity changed"):
                repository.register_generation(
                    generation_id,
                    worker_id="worker-a",
                    storage_id="replacement-storage",
                    workspace_id=None,
                    now=started_at + timedelta(seconds=4),
                )
            requested_restart_generation_id = str(uuid4())
            restarted = repository.register_generation(
                requested_restart_generation_id,
                worker_id="worker-a",
                storage_id="storage-a",
                workspace_id=None,
                now=started_at + timedelta(seconds=4),
            )
            assert restarted.id == generation_id
            assert restarted.state is WorkerCacheGenerationState.Initializing
            assert restarted.session_fence == 2
            assert not repository.complete_claim(
                old_claim.id,
                generation_id=generation_id,
                worker_id="worker-a",
                session_fence=1,
                claim_token=old_claim.claim_token or "",
                now=started_at + timedelta(seconds=5),
            )

            reclaimed = repository.claim_due(
                generation_id,
                worker_id="worker-a",
                session_fence=2,
                now=started_at + timedelta(seconds=5),
                lease_until=started_at + timedelta(minutes=6),
                limit=1,
            )[0]
            assert reclaimed.id == old_claim.id
            assert reclaimed.attempt_count == 2
            assert reclaimed.claim_token != old_claim.claim_token
            assert reclaimed.claim_session_fence == 2
            assert repository.fail_claim(
                reclaimed.id,
                generation_id=generation_id,
                worker_id="worker-a",
                session_fence=2,
                claim_token=reclaimed.claim_token or "",
                next_attempt_at=started_at + timedelta(minutes=7),
                error_code=SourceCacheCleanupErrorCode.PurgeFailed,
                now=started_at + timedelta(seconds=6),
            )
            assert (
                repository.claim_due(
                    generation_id,
                    worker_id="worker-a",
                    session_fence=2,
                    now=started_at + timedelta(minutes=6),
                    lease_until=started_at + timedelta(minutes=8),
                    limit=1,
                )
                == []
            )

            retried = repository.claim_due(
                generation_id,
                worker_id="worker-a",
                session_fence=2,
                now=started_at + timedelta(minutes=7),
                lease_until=started_at + timedelta(minutes=8),
                limit=1,
            )[0]
            assert retried.attempt_count == 3
            assert retried.last_error_code is None
            assert repository.complete_claim(
                retried.id,
                generation_id=generation_id,
                worker_id="worker-a",
                session_fence=2,
                claim_token=retried.claim_token or "",
                now=started_at + timedelta(minutes=7, seconds=1),
            )
            available = repository.activate_if_drained(
                generation_id,
                worker_id="worker-a",
                session_fence=2,
                now=started_at + timedelta(minutes=7, seconds=2),
            )
            assert available is not None
            assert available.state is WorkerCacheGenerationState.Available
            completed_summary = repository.summarize(workspace_id=workspace.id)
            assert completed_summary.pending_count == 0
            assert completed_summary.claimed_count == 0
            assert completed_summary.completed_count == 1
            assert completed_summary.generations_pending == 0
            assert completed_summary.oldest_pending_at is None
            assert completed_summary.complete

            repository.add_targets(
                workspace_id=workspace.id,
                source_object_ids=[str(uuid4())],
                now=started_at + timedelta(minutes=8),
            )
            assert (
                repository.activate_if_drained(
                    generation_id,
                    worker_id="worker-a",
                    session_fence=2,
                    now=started_at + timedelta(minutes=8),
                )
                is None
            )
            mixed_summary = repository.summarize(workspace_id=workspace.id)
            assert mixed_summary.pending_count == 1
            assert mixed_summary.claimed_count == 0
            assert mixed_summary.completed_count == 1
            assert mixed_summary.generations_pending == 1
            assert mixed_summary.oldest_pending_at == started_at + timedelta(minutes=8)
            assert not mixed_summary.complete
            expiring = repository.claim_due(
                generation_id,
                worker_id="worker-a",
                session_fence=2,
                now=started_at + timedelta(minutes=8),
                lease_until=started_at + timedelta(minutes=8, seconds=30),
                limit=1,
            )[0]
            assert (
                repository.claim_due(
                    generation_id,
                    worker_id="worker-a",
                    session_fence=2,
                    now=started_at + timedelta(minutes=8, seconds=20),
                    lease_until=started_at + timedelta(minutes=9),
                    limit=1,
                )
                == []
            )
            reclaimed_after_expiry = repository.claim_due(
                generation_id,
                worker_id="worker-a",
                session_fence=2,
                now=started_at + timedelta(minutes=8, seconds=30),
                lease_until=started_at + timedelta(minutes=9),
                limit=1,
            )[0]
            assert reclaimed_after_expiry.id == expiring.id
            assert reclaimed_after_expiry.attempt_count == 2
            assert not repository.complete_claim(
                reclaimed_after_expiry.id,
                generation_id=generation_id,
                worker_id="worker-a",
                session_fence=2,
                claim_token=expiring.claim_token or "",
                now=started_at + timedelta(minutes=8, seconds=31),
            )
            assert (
                repository.retire_destroyed(
                    generation_id,
                    worker_id="worker-a",
                    storage_id="wrong-storage",
                    session_fence=2,
                    now=started_at + timedelta(minutes=9),
                )
                is None
            )
            retired = repository.retire_destroyed(
                generation_id,
                worker_id="worker-a",
                storage_id="storage-a",
                session_fence=2,
                now=started_at + timedelta(minutes=9),
            )
            assert retired is not None
            assert retired.state is WorkerCacheGenerationState.Retired
            assert retired.storage_destroyed_at == started_at + timedelta(minutes=9)
            assert {
                target.completion_reason
                for target in repository.list_targets(workspace_id=workspace.id)
            } == {
                SourceCacheCleanupCompletionReason.Purged,
                SourceCacheCleanupCompletionReason.StorageDestroyed,
            }
            with pytest.raises(ConflictError, match="is retired"):
                repository.register_generation(
                    generation_id,
                    worker_id="worker-c",
                    storage_id="storage-a",
                    workspace_id=None,
                    now=started_at + timedelta(minutes=10),
                )
    finally:
        database.dispose()


def test_postgresql_cleanup_targets_only_global_and_matching_private_generations(
    postgres_database_url: URL,
) -> None:
    database_url = postgres_database_url.render_as_string(hide_password=False)
    bootstrap_database(database_url)
    database = _client(database_url, pool_size=2)
    now = datetime(2026, 7, 21, 13, tzinfo=UTC)
    source_object_id = str(uuid4())
    try:
        with database.session() as session:
            workspaces = WorkspaceRepository(session)
            owner = workspaces.create(name="cleanup-private-owner")
            sibling = workspaces.create(name="cleanup-private-sibling")
            repository = SourceCacheCleanupRepository(session)
            global_generation = repository.register_generation(
                str(uuid4()),
                worker_id="shared-worker",
                storage_id="shared-storage",
                workspace_id=None,
                now=now,
            )
            owner_generation = repository.register_generation(
                str(uuid4()),
                worker_id="owner-worker",
                storage_id="owner-storage",
                workspace_id=owner.id,
                now=now,
            )
            sibling_generation = repository.register_generation(
                str(uuid4()),
                worker_id="sibling-worker",
                storage_id="sibling-storage",
                workspace_id=sibling.id,
                now=now,
            )

            assert (
                repository.add_targets(
                    workspace_id=owner.id,
                    source_object_ids=[source_object_id],
                    now=now,
                )
                == 2
            )
            targets = repository.list_targets(workspace_id=owner.id)
            assert {target.cache_generation_id for target in targets} == {
                global_generation.id,
                owner_generation.id,
            }
            assert sibling_generation.id not in {target.cache_generation_id for target in targets}
            deleting_owner = workspaces.lock_for_deletion(owner.id)
            workspaces.mark_deleting(deleting_owner)
            workspaces.purge_owned_records(owner.id)
            assert repository.get_generation(owner_generation.id) is not None
            assert len(repository.list_targets(workspace_id=owner.id)) == 2
    finally:
        database.dispose()


def test_postgresql_cleanup_claims_are_disjoint_and_tombstones_survive_workspace_purge(
    postgres_database_url: URL,
) -> None:
    database_url = postgres_database_url.render_as_string(hide_password=False)
    bootstrap_database(database_url)
    database = _client(database_url, pool_size=2)
    started_at = datetime(2026, 7, 21, 14, tzinfo=UTC)
    generation_id = str(uuid4())
    sources = [str(uuid4()) for _ in range(8)]
    try:
        with database.session() as session:
            workspace = WorkspaceRepository(session).create(name="cleanup-concurrency")
            repository = SourceCacheCleanupRepository(session)
            generation = repository.register_generation(
                generation_id,
                worker_id="worker-a",
                storage_id="storage-a",
                workspace_id=None,
                now=started_at,
            )

        rolled_back_source = str(uuid4())
        with database.session() as session:
            SourceCacheCleanupRepository(session).add_targets(
                workspace_id=workspace.id,
                source_object_ids=[rolled_back_source],
                now=started_at,
            )
            session.rollback()

        with database.session() as session:
            repository = SourceCacheCleanupRepository(session)
            assert (
                repository.list_targets(
                    workspace_id=workspace.id,
                    source_object_ids=[rolled_back_source],
                )
                == []
            )
            repository.add_targets(
                workspace_id=workspace.id,
                source_object_ids=sources,
                now=started_at,
            )

        barrier = Barrier(2)

        def claim_batch(_index: int) -> tuple[str, ...]:
            barrier.wait()
            with database.session() as session:
                claimed = SourceCacheCleanupRepository(session).claim_due(
                    generation_id,
                    worker_id="worker-a",
                    session_fence=generation.session_fence,
                    now=started_at,
                    lease_until=started_at + timedelta(minutes=1),
                    limit=4,
                )
                return tuple(target.id for target in claimed)

        with ThreadPoolExecutor(max_workers=2) as executor:
            batches = tuple(executor.map(claim_batch, range(2)))

        assert len(batches[0]) == len(batches[1]) == 4
        assert set(batches[0]).isdisjoint(batches[1])

        with database.session() as session:
            workspace_repository = WorkspaceRepository(session)
            assert "source_cache_cleanup_targets" not in workspace_repository.deletion_blockers(
                workspace.id
            )
            deleting = workspace_repository.mark_deleting(
                workspace_repository.lock_for_deletion(workspace.id)
            )
            workspace_repository.purge_owned_records(workspace.id)
            workspace_repository.tombstone(deleting)
            targets = SourceCacheCleanupRepository(session).list_targets(workspace_id=workspace.id)
            assert len(targets) == len(sources)

        with database.engine.connect() as connection:
            target_foreign_keys = inspect(connection).get_foreign_keys(
                "source_cache_cleanup_targets"
            )
            target_columns = {
                item["name"]
                for item in inspect(connection).get_columns("source_cache_cleanup_targets")
            }
            generation_columns = {
                item["name"] for item in inspect(connection).get_columns("worker_cache_generations")
            }
        assert "payload" not in target_columns | generation_columns
        assert {
            (
                item["referred_table"],
                tuple(item["constrained_columns"]),
                item.get("options", {}).get("ondelete"),
            )
            for item in target_foreign_keys
        } == {
            ("worker_cache_generations", ("cache_generation_id",), "RESTRICT"),
            ("workspaces", ("workspace_id",), "RESTRICT"),
        }
        assert "source_object_id" in target_columns
    finally:
        database.dispose()


def _client(database_url: str, *, pool_size: int) -> DatabaseClient:
    return DatabaseClient.from_settings(
        DatabaseSettings(
            url=database_url,
            pool_size=pool_size,
            max_overflow=0,
            statement_timeout_ms=0,
            application_name=DatabaseApplicationName.Test,
        )
    )
