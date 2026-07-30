from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from database.mappers.source_cache import (
    source_cache_cleanup_target_record_from_table,
    worker_cache_generation_record_from_table,
)
from database.tables.source_cache import (
    SourceCacheCleanupTargetTable,
    WorkerCacheGenerationTable,
)
from shared.errors import ConflictError
from shared.source_cache_cleanup import (
    SourceCacheCleanupCompletionReason,
    SourceCacheCleanupErrorCode,
    SourceCacheCleanupStatus,
    SourceCacheCleanupSummary,
    SourceCacheCleanupTargetRecord,
    WorkerCacheGenerationRecord,
    WorkerCacheGenerationState,
)
from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

type SourceCacheInsertValue = str | int | datetime | None

_TARGET_INSERT_BATCH_SIZE = 500


@dataclass(slots=True)
class SourceCacheCleanupRepository:
    session: Session

    def register_generation(
        self,
        generation_id: str,
        *,
        worker_id: str,
        storage_id: str,
        workspace_id: str | None,
        now: datetime,
    ) -> WorkerCacheGenerationRecord:
        """Open a new fenced session for one physical cache marker."""
        normalized_worker_id = _bounded_identifier(worker_id, field="worker id", maximum=240)
        normalized_storage_id = _bounded_identifier(
            storage_id,
            field="storage id",
            maximum=512,
        )
        inserted_id = self._insert_generation(
            generation_id,
            worker_id=normalized_worker_id,
            storage_id=normalized_storage_id,
            workspace_id=workspace_id,
            now=now,
        )
        if inserted_id is not None:
            row = self.session.get(WorkerCacheGenerationTable, inserted_id)
            if row is None:
                raise RuntimeError("inserted worker cache generation was not returned")
            return worker_cache_generation_record_from_table(row)

        row = self.session.scalars(
            select(WorkerCacheGenerationTable)
            .where(WorkerCacheGenerationTable.id == generation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if row is None:
            row = self.session.scalars(
                select(WorkerCacheGenerationTable)
                .where(
                    WorkerCacheGenerationTable.storage_id == normalized_storage_id,
                    WorkerCacheGenerationTable.state != WorkerCacheGenerationState.Retired.value,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            ).first()
            if row is None:
                raise RuntimeError("worker cache generation insert conflict was not found")
        if row.state == WorkerCacheGenerationState.Retired.value:
            raise ConflictError(f"worker cache generation is retired: {generation_id}")
        if row.storage_id != normalized_storage_id:
            raise ConflictError(
                f"worker cache generation storage identity changed: {generation_id}"
            )
        if row.worker_id != normalized_worker_id:
            raise ConflictError(
                f"worker cache storage identity is already active: {normalized_storage_id}"
            )
        if (str(row.workspace_id) if row.workspace_id is not None else None) != workspace_id:
            raise ConflictError(f"worker cache generation workspace scope changed: {generation_id}")
        row.state = WorkerCacheGenerationState.Initializing.value
        row.session_fence += 1
        row.last_seen_at = now
        row.updated_at = now
        self.session.flush()
        return worker_cache_generation_record_from_table(row)

    def get_generation(self, generation_id: str) -> WorkerCacheGenerationRecord | None:
        row = self.session.get(WorkerCacheGenerationTable, generation_id)
        return worker_cache_generation_record_from_table(row) if row is not None else None

    def list_generations(
        self, *, include_retired: bool = False
    ) -> list[WorkerCacheGenerationRecord]:
        statement = select(WorkerCacheGenerationTable)
        if not include_retired:
            statement = statement.where(
                WorkerCacheGenerationTable.state != WorkerCacheGenerationState.Retired.value
            )
        rows = self.session.scalars(
            statement.order_by(
                WorkerCacheGenerationTable.created_at,
                WorkerCacheGenerationTable.id,
            )
        )
        return [worker_cache_generation_record_from_table(row) for row in rows]

    def touch_generation(
        self,
        generation_id: str,
        *,
        worker_id: str,
        session_fence: int,
        now: datetime,
    ) -> bool:
        updated_id = self.session.scalar(
            update(WorkerCacheGenerationTable)
            .where(
                WorkerCacheGenerationTable.id == generation_id,
                WorkerCacheGenerationTable.worker_id == worker_id,
                WorkerCacheGenerationTable.session_fence == session_fence,
                WorkerCacheGenerationTable.state != WorkerCacheGenerationState.Retired.value,
            )
            .values(last_seen_at=now, updated_at=now)
            .returning(WorkerCacheGenerationTable.id)
            .execution_options(synchronize_session=False)
        )
        self.session.flush()
        return updated_id is not None

    def add_targets(
        self,
        *,
        workspace_id: str,
        source_object_ids: list[str],
        now: datetime,
    ) -> int:
        """Idempotently snapshot sources across every non-retired physical cache."""
        normalized_source_ids = sorted(
            {value.strip() for value in source_object_ids if value.strip()}
        )
        if not normalized_source_ids:
            return 0

        generation_rows = list(
            self.session.scalars(
                select(WorkerCacheGenerationTable)
                .where(
                    WorkerCacheGenerationTable.state != WorkerCacheGenerationState.Retired.value,
                    or_(
                        WorkerCacheGenerationTable.workspace_id.is_(None),
                        WorkerCacheGenerationTable.workspace_id == workspace_id,
                    ),
                )
                .order_by(WorkerCacheGenerationTable.id)
                .with_for_update(read=True)
            )
        )
        generation_ids = [str(row.id) for row in generation_rows]
        target_values: list[dict[str, SourceCacheInsertValue]] = []
        for generation_id in generation_ids:
            for source_object_id in normalized_source_ids:
                target_values.append(
                    {
                        "id": str(uuid4()),
                        "workspace_id": workspace_id,
                        "cache_generation_id": generation_id,
                        "source_object_id": source_object_id,
                        "status": SourceCacheCleanupStatus.Pending.value,
                        "attempt_count": 0,
                        "next_attempt_at": now,
                        "last_error_code": None,
                        "created_at": now,
                        "updated_at": now,
                    }
                )

        inserted_count = 0
        for offset in range(0, len(target_values), _TARGET_INSERT_BATCH_SIZE):
            inserted_count += self._insert_target_batch(
                target_values[offset : offset + _TARGET_INSERT_BATCH_SIZE]
            )

        if generation_ids:
            incomplete_generation_ids = select(
                SourceCacheCleanupTargetTable.cache_generation_id
            ).where(
                SourceCacheCleanupTargetTable.cache_generation_id.in_(generation_ids),
                SourceCacheCleanupTargetTable.status != SourceCacheCleanupStatus.Completed.value,
            )
            self.session.execute(
                update(WorkerCacheGenerationTable)
                .where(
                    WorkerCacheGenerationTable.id.in_(incomplete_generation_ids),
                    WorkerCacheGenerationTable.state == WorkerCacheGenerationState.Available.value,
                )
                .values(
                    state=WorkerCacheGenerationState.Draining.value,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
        self.session.flush()
        return inserted_count

    def list_targets(
        self,
        *,
        workspace_id: str | None = None,
        generation_ids: list[str] | None = None,
        source_object_ids: list[str] | None = None,
        include_completed: bool = True,
    ) -> list[SourceCacheCleanupTargetRecord]:
        statement = select(SourceCacheCleanupTargetTable)
        if workspace_id is not None:
            statement = statement.where(SourceCacheCleanupTargetTable.workspace_id == workspace_id)
        if generation_ids is not None:
            if not generation_ids:
                return []
            statement = statement.where(
                SourceCacheCleanupTargetTable.cache_generation_id.in_(generation_ids)
            )
        if source_object_ids is not None:
            if not source_object_ids:
                return []
            statement = statement.where(
                SourceCacheCleanupTargetTable.source_object_id.in_(source_object_ids)
            )
        if not include_completed:
            statement = statement.where(
                SourceCacheCleanupTargetTable.status != SourceCacheCleanupStatus.Completed.value
            )
        rows = self.session.scalars(
            statement.order_by(
                SourceCacheCleanupTargetTable.created_at,
                SourceCacheCleanupTargetTable.id,
            )
        )
        return [source_cache_cleanup_target_record_from_table(row) for row in rows]

    def summarize(self, *, workspace_id: str) -> SourceCacheCleanupSummary:
        counts = {
            SourceCacheCleanupStatus(str(status)): int(count)
            for status, count in self.session.execute(
                select(
                    SourceCacheCleanupTargetTable.status,
                    func.count(SourceCacheCleanupTargetTable.id),
                )
                .where(SourceCacheCleanupTargetTable.workspace_id == workspace_id)
                .group_by(SourceCacheCleanupTargetTable.status)
            )
        }
        incomplete = (
            SourceCacheCleanupTargetTable.status != SourceCacheCleanupStatus.Completed.value
        )
        generations_pending = self.session.scalar(
            select(
                func.count(func.distinct(SourceCacheCleanupTargetTable.cache_generation_id))
            ).where(
                SourceCacheCleanupTargetTable.workspace_id == workspace_id,
                incomplete,
            )
        )
        oldest_pending_at = self.session.scalar(
            select(func.min(SourceCacheCleanupTargetTable.created_at)).where(
                SourceCacheCleanupTargetTable.workspace_id == workspace_id,
                incomplete,
            )
        )
        failing_count = self.session.scalar(
            select(func.count(SourceCacheCleanupTargetTable.id)).where(
                SourceCacheCleanupTargetTable.workspace_id == workspace_id,
                incomplete,
                SourceCacheCleanupTargetTable.last_error_code.is_not(None),
            )
        )
        last_error_code = self.session.scalar(
            select(SourceCacheCleanupTargetTable.last_error_code)
            .where(
                SourceCacheCleanupTargetTable.workspace_id == workspace_id,
                incomplete,
                SourceCacheCleanupTargetTable.last_error_code.is_not(None),
            )
            .order_by(SourceCacheCleanupTargetTable.updated_at.desc())
            .limit(1)
        )
        return SourceCacheCleanupSummary(
            workspace_id=workspace_id,
            pending_count=counts.get(SourceCacheCleanupStatus.Pending, 0),
            claimed_count=counts.get(SourceCacheCleanupStatus.Claimed, 0),
            completed_count=counts.get(SourceCacheCleanupStatus.Completed, 0),
            generations_pending=generations_pending or 0,
            failing_count=failing_count or 0,
            last_error_code=(
                SourceCacheCleanupErrorCode(str(last_error_code))
                if last_error_code is not None
                else None
            ),
            oldest_pending_at=oldest_pending_at,
            complete=(
                counts.get(SourceCacheCleanupStatus.Pending, 0)
                + counts.get(SourceCacheCleanupStatus.Claimed, 0)
                == 0
            ),
        )

    def claim_due(
        self,
        generation_id: str,
        *,
        worker_id: str,
        session_fence: int,
        now: datetime,
        lease_until: datetime,
        limit: int,
    ) -> list[SourceCacheCleanupTargetRecord]:
        if lease_until <= now:
            raise ValueError("source cache cleanup lease must expire after claim time")
        if limit <= 0:
            return []
        generation = self._current_generation_row(
            generation_id,
            worker_id=worker_id,
            session_fence=session_fence,
        )
        if generation is None:
            return []

        statement = (
            select(SourceCacheCleanupTargetTable)
            .where(
                SourceCacheCleanupTargetTable.cache_generation_id == generation_id,
                SourceCacheCleanupTargetTable.status != SourceCacheCleanupStatus.Completed.value,
                or_(
                    (SourceCacheCleanupTargetTable.status == SourceCacheCleanupStatus.Pending.value)
                    & (SourceCacheCleanupTargetTable.next_attempt_at <= now),
                    (SourceCacheCleanupTargetTable.status == SourceCacheCleanupStatus.Claimed.value)
                    & (
                        (SourceCacheCleanupTargetTable.claim_expires_at <= now)
                        | (SourceCacheCleanupTargetTable.claim_session_fence != session_fence)
                    ),
                ),
            )
            .order_by(
                SourceCacheCleanupTargetTable.next_attempt_at,
                SourceCacheCleanupTargetTable.created_at,
                SourceCacheCleanupTargetTable.id,
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        claimed: list[SourceCacheCleanupTargetRecord] = []
        for row in self.session.scalars(statement):
            row.status = SourceCacheCleanupStatus.Claimed.value
            row.attempt_count += 1
            row.claim_token = str(uuid4())
            row.claim_expires_at = lease_until
            row.claim_session_fence = session_fence
            row.last_error_code = None
            row.updated_at = now
            claimed.append(source_cache_cleanup_target_record_from_table(row))
        if claimed and generation.state == WorkerCacheGenerationState.Available.value:
            generation.state = WorkerCacheGenerationState.Draining.value
            generation.updated_at = now
        self.session.flush()
        return claimed

    def complete_claim(
        self,
        target_id: str,
        *,
        generation_id: str,
        worker_id: str,
        session_fence: int,
        claim_token: str,
        now: datetime,
    ) -> bool:
        return self._resolve_claim(
            target_id,
            generation_id=generation_id,
            worker_id=worker_id,
            session_fence=session_fence,
            claim_token=claim_token,
            values={
                "status": SourceCacheCleanupStatus.Completed.value,
                "claim_token": None,
                "claim_expires_at": None,
                "claim_session_fence": None,
                "completed_at": now,
                "completion_reason": SourceCacheCleanupCompletionReason.Purged.value,
                "last_error_code": None,
                "updated_at": now,
            },
        )

    def fail_claim(
        self,
        target_id: str,
        *,
        generation_id: str,
        worker_id: str,
        session_fence: int,
        claim_token: str,
        next_attempt_at: datetime,
        error_code: SourceCacheCleanupErrorCode,
        now: datetime,
    ) -> bool:
        return self._resolve_claim(
            target_id,
            generation_id=generation_id,
            worker_id=worker_id,
            session_fence=session_fence,
            claim_token=claim_token,
            values={
                "status": SourceCacheCleanupStatus.Pending.value,
                "next_attempt_at": next_attempt_at,
                "claim_token": None,
                "claim_expires_at": None,
                "claim_session_fence": None,
                "last_error_code": error_code.value,
                "updated_at": now,
            },
        )

    def activate_if_drained(
        self,
        generation_id: str,
        *,
        worker_id: str,
        session_fence: int,
        now: datetime,
    ) -> WorkerCacheGenerationRecord | None:
        row = self._current_generation_row(
            generation_id,
            worker_id=worker_id,
            session_fence=session_fence,
        )
        if row is None:
            return None
        incomplete = self.session.scalar(
            select(
                exists().where(
                    SourceCacheCleanupTargetTable.cache_generation_id == generation_id,
                    SourceCacheCleanupTargetTable.status
                    != SourceCacheCleanupStatus.Completed.value,
                )
            )
        )
        if incomplete:
            return None
        row.state = WorkerCacheGenerationState.Available.value
        row.last_seen_at = now
        row.updated_at = now
        self.session.flush()
        return worker_cache_generation_record_from_table(row)

    def retire_destroyed(
        self,
        generation_id: str,
        *,
        worker_id: str,
        storage_id: str,
        session_fence: int,
        now: datetime,
    ) -> WorkerCacheGenerationRecord | None:
        """Retire storage after its owning service has proved physical destruction."""
        row = self.session.scalars(
            select(WorkerCacheGenerationTable)
            .where(WorkerCacheGenerationTable.id == generation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if (
            row is None
            or row.worker_id != worker_id
            or row.storage_id != storage_id
            or row.session_fence != session_fence
        ):
            return None
        if row.state != WorkerCacheGenerationState.Retired.value:
            self.session.execute(
                update(SourceCacheCleanupTargetTable)
                .where(
                    SourceCacheCleanupTargetTable.cache_generation_id == generation_id,
                    SourceCacheCleanupTargetTable.status
                    != SourceCacheCleanupStatus.Completed.value,
                )
                .values(
                    status=SourceCacheCleanupStatus.Completed.value,
                    claim_token=None,
                    claim_expires_at=None,
                    claim_session_fence=None,
                    last_error_code=None,
                    completed_at=now,
                    completion_reason=(SourceCacheCleanupCompletionReason.StorageDestroyed.value),
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            row.state = WorkerCacheGenerationState.Retired.value
            row.last_seen_at = now
            row.retired_at = now
            row.storage_destroyed_at = now
            row.updated_at = now
            self.session.flush()
        return worker_cache_generation_record_from_table(row)

    def _insert_generation(
        self,
        generation_id: str,
        *,
        worker_id: str,
        storage_id: str,
        workspace_id: str | None,
        now: datetime,
    ) -> str | None:
        values: dict[str, SourceCacheInsertValue] = {
            "id": generation_id,
            "worker_id": worker_id,
            "storage_id": storage_id,
            "workspace_id": workspace_id,
            "state": WorkerCacheGenerationState.Initializing.value,
            "session_fence": 1,
            "last_seen_at": now,
            "created_at": now,
            "updated_at": now,
        }
        dialect = self.session.get_bind().dialect.name
        if dialect == "postgresql":
            statement = (
                postgresql_insert(WorkerCacheGenerationTable)
                .values(**values)
                .on_conflict_do_nothing()
                .returning(WorkerCacheGenerationTable.id)
            )
        elif dialect == "sqlite":
            statement = (
                sqlite_insert(WorkerCacheGenerationTable)
                .values(**values)
                .on_conflict_do_nothing()
                .returning(WorkerCacheGenerationTable.id)
            )
        else:
            raise RuntimeError(f"unsupported source cache cleanup database dialect: {dialect}")
        inserted_id = self.session.scalar(statement)
        self.session.flush()
        return str(inserted_id) if inserted_id is not None else None

    def _insert_target_batch(
        self,
        values: list[dict[str, SourceCacheInsertValue]],
    ) -> int:
        if not values:
            return 0
        dialect = self.session.get_bind().dialect.name
        if dialect == "postgresql":
            statement = postgresql_insert(SourceCacheCleanupTargetTable).values(values)
            statement = statement.on_conflict_do_nothing(
                constraint="uq_source_cache_cleanup_target"
            ).returning(SourceCacheCleanupTargetTable.id)
        elif dialect == "sqlite":
            statement = sqlite_insert(SourceCacheCleanupTargetTable).values(values)
            statement = statement.on_conflict_do_nothing(
                index_elements=[
                    SourceCacheCleanupTargetTable.workspace_id,
                    SourceCacheCleanupTargetTable.cache_generation_id,
                    SourceCacheCleanupTargetTable.source_object_id,
                ]
            ).returning(SourceCacheCleanupTargetTable.id)
        else:
            raise RuntimeError(f"unsupported source cache cleanup database dialect: {dialect}")
        return len(self.session.scalars(statement).all())

    def _current_generation_row(
        self,
        generation_id: str,
        *,
        worker_id: str,
        session_fence: int,
    ) -> WorkerCacheGenerationTable | None:
        return self.session.scalars(
            select(WorkerCacheGenerationTable)
            .where(
                WorkerCacheGenerationTable.id == generation_id,
                WorkerCacheGenerationTable.worker_id == worker_id,
                WorkerCacheGenerationTable.session_fence == session_fence,
                WorkerCacheGenerationTable.state != WorkerCacheGenerationState.Retired.value,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()

    def _resolve_claim(
        self,
        target_id: str,
        *,
        generation_id: str,
        worker_id: str,
        session_fence: int,
        claim_token: str,
        values: dict[str, str | int | datetime | None],
    ) -> bool:
        current_generation = exists().where(
            WorkerCacheGenerationTable.id == generation_id,
            WorkerCacheGenerationTable.worker_id == worker_id,
            WorkerCacheGenerationTable.session_fence == session_fence,
            WorkerCacheGenerationTable.state != WorkerCacheGenerationState.Retired.value,
        )
        updated_id = self.session.scalar(
            update(SourceCacheCleanupTargetTable)
            .where(
                SourceCacheCleanupTargetTable.id == target_id,
                SourceCacheCleanupTargetTable.cache_generation_id == generation_id,
                SourceCacheCleanupTargetTable.status == SourceCacheCleanupStatus.Claimed.value,
                SourceCacheCleanupTargetTable.claim_token == claim_token,
                SourceCacheCleanupTargetTable.claim_session_fence == session_fence,
                current_generation,
            )
            .values(**values)
            .returning(SourceCacheCleanupTargetTable.id)
            .execution_options(synchronize_session=False)
        )
        self.session.flush()
        return updated_id is not None


def _bounded_identifier(value: str, *, field: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"source cache generation {field} is required")
    if len(normalized) > maximum:
        raise ValueError(f"source cache generation {field} exceeds {maximum} characters")
    return normalized


__all__ = ["SourceCacheCleanupRepository"]
