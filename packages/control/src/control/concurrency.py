from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import uuid4

from database.repositories.identity import ConcurrencyLimitRepository, WorkspaceRepository
from foundation.ids import try_uuid
from observability.workspace_changes import WorkspaceChangePublisher
from pydantic import JsonValue
from shared.app_identity import DEFAULT_RESOURCE_TYPE
from shared.errors import NotFoundError
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.identity import ConcurrencyLimitRecord
from shared.timestamps import utc_now

from control.context import ControlContext
from control.models import ConcurrencyAcquireResult, ConcurrencyAcquireStatus


def _limit_by_id_or_name(
    repository: ConcurrencyLimitRepository, limit_id_or_name: str, *, workspace_id: str
) -> ConcurrencyLimitRecord:
    limit_id = try_uuid(limit_id_or_name)
    if limit_id is not None:
        record = repository.get(limit_id, workspace_id=workspace_id, for_update=True)
        if record is not None:
            return record
    record = repository.by_name(limit_id_or_name, workspace_id=workspace_id, for_update=True)
    if record is None:
        raise NotFoundError(f"concurrency limit not found: {limit_id_or_name}")
    return record


@dataclass(slots=True)
class ConcurrencyService:
    context: ControlContext
    workspace_changes: WorkspaceChangePublisher | None = None

    def upsert_concurrency_limit(
        self,
        name: str,
        *,
        limit: int,
        workspace: str = "default",
        resource_type: str = DEFAULT_RESOURCE_TYPE,
        resource_id: str | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> ConcurrencyLimitRecord:
        with self.context.database.session() as session:
            repository = ConcurrencyLimitRepository(session)
            workspace_record = WorkspaceRepository(session).lock_active_owner(
                self.context.workspace(session, workspace).id, exclusive=True
            )
            existing = repository.by_name(name, workspace_id=workspace_record.id)
            record = ConcurrencyLimitRecord(
                id=existing.id if existing else str(uuid4()),
                workspace_id=workspace_record.id,
                name=name,
                limit=limit,
                in_flight=existing.in_flight if existing else 0,
                resource_type=resource_type,
                resource_id=resource_id,
                metadata={**(existing.metadata if existing else {}), **(metadata or {})},
                created_at=existing.created_at if existing else utc_now(),
            )
            record = repository.upsert(record, workspace_id=workspace_record.id)
            workspace_record.concurrency_limit_id = record.id
            workspace_record.updated_at = record.updated_at
            WorkspaceRepository(session).upsert(workspace_record)
        self._publish_concurrency_change(
            record, WorkspaceChangeType.Created if existing is None else WorkspaceChangeType.Updated
        )
        return record

    def list_concurrency_limits(
        self, *, workspace: str | None = None
    ) -> list[ConcurrencyLimitRecord]:
        with self.context.database.session() as session:
            workspace_id = (
                self.context.workspace(session, workspace).id if workspace is not None else None
            )
            repository = ConcurrencyLimitRepository(session)
            records = (
                repository.list(workspace_id=workspace_id)
                if workspace_id is not None
                else repository.list_across_workspaces()
            )
        records.sort(key=lambda item: (item.workspace_id, item.name, item.created_at))
        return records

    def current_concurrency_limit(self, *, workspace: str = "default") -> ConcurrencyLimitRecord:
        with self.context.database.session() as session:
            workspace_record = self.context.workspace(session, workspace)
            record = (
                None
                if workspace_record.concurrency_limit_id is None
                else ConcurrencyLimitRepository(session).get(
                    workspace_record.concurrency_limit_id, workspace_id=workspace_record.id
                )
            )
        if record is None:
            raise NotFoundError(f"current concurrency limit not found for workspace: {workspace}")
        return record

    def delete_current_concurrency_limit(self, *, workspace: str = "default") -> None:
        with self.context.database.session() as session:
            workspace_record = WorkspaceRepository(session).lock_active_owner(
                self.context.workspace(session, workspace).id, exclusive=True
            )
            previous_limit_id = workspace_record.concurrency_limit_id
            workspace_record.concurrency_limit_id = None
            workspace_record.updated_at = utc_now()
            WorkspaceRepository(session).upsert(workspace_record)
        if self.workspace_changes is not None and previous_limit_id is not None:
            self.workspace_changes.emit_change(
                workspace_id=workspace_record.id,
                topic=WorkspaceChangeTopic.Concurrency,
                change=WorkspaceChangeType.Deleted,
                resource_id=previous_limit_id,
            )

    def revert_concurrency_limit(self, *, workspace: str = "default") -> ConcurrencyLimitRecord:
        with self.context.database.session() as session:
            workspace_record = WorkspaceRepository(session).lock_active_owner(
                self.context.workspace(session, workspace).id, exclusive=True
            )
            chosen = ConcurrencyLimitRepository(session).previous(
                workspace_id=workspace_record.id, current_id=workspace_record.concurrency_limit_id
            )
            if chosen is None:
                raise NotFoundError("no previous non-zero concurrency limit found")
            workspace_record.concurrency_limit_id = chosen.id
            workspace_record.updated_at = utc_now()
            WorkspaceRepository(session).upsert(workspace_record)
        self._publish_concurrency_change(chosen, WorkspaceChangeType.Updated)
        return chosen

    def acquire_concurrency(
        self, limit_id_or_name: str, *, workspace: str = "default"
    ) -> ConcurrencyAcquireResult:
        return self._change_concurrency(limit_id_or_name, workspace=workspace, delta=1)

    def release_concurrency(
        self, limit_id_or_name: str, *, workspace: str = "default"
    ) -> ConcurrencyAcquireResult:
        return self._change_concurrency(limit_id_or_name, workspace=workspace, delta=-1)

    def _publish_concurrency_change(
        self, record: ConcurrencyLimitRecord, change: WorkspaceChangeType
    ) -> None:
        if self.workspace_changes is None:
            return
        self.workspace_changes.emit_change(
            workspace_id=record.workspace_id,
            topic=WorkspaceChangeTopic.Concurrency,
            change=change,
            resource_id=record.id,
        )

    def _change_concurrency(
        self, limit_id_or_name: str, *, workspace: str, delta: int
    ) -> ConcurrencyAcquireResult:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            WorkspaceRepository(session).lock_active_owner(workspace_id)
            repository = ConcurrencyLimitRepository(session)
            record = _limit_by_id_or_name(repository, limit_id_or_name, workspace_id=workspace_id)
            before = record.available
            changed = False
            if delta > 0 and record.saturated:
                status, reason = (
                    ConcurrencyAcquireStatus.Saturated,
                    "concurrency limit is saturated",
                )
            elif delta < 0 and record.in_flight == 0:
                status, reason = ConcurrencyAcquireStatus.Released, "no concurrency slot was held"
            else:
                record.in_flight += delta
                record.updated_at = utc_now()
                record = repository.upsert(record, workspace_id=workspace_id)
                changed = True
                status = (
                    ConcurrencyAcquireStatus.Acquired
                    if delta > 0
                    else ConcurrencyAcquireStatus.Released
                )
                reason = "slot acquired" if delta > 0 else "slot released"
        if changed:
            self._publish_concurrency_change(record, WorkspaceChangeType.Updated)
        return ConcurrencyAcquireResult(
            status=status,
            acquired=status is ConcurrencyAcquireStatus.Acquired,
            record=record,
            available_before=before,
            available_after=record.available,
            reason=reason,
        )
