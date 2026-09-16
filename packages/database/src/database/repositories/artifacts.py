from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from database.mappers.storage import object_from_table
from database.repositories.cleanup import OBJECT_CLEANUP_DELETE, CleanupRepository
from database.repositories.identity import WorkspaceRepository
from database.tables.billing_ledger import BillingLedgerSegmentTable
from database.tables.storage import ObjectTable
from shared.artifacts import ARTIFACT_STORAGE_SUBJECT
from shared.errors import ConflictError
from shared.objects import ObjectRecord
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session


@dataclass(slots=True)
class ArtifactRepository:
    session: Session

    def get(
        self, artifact_id: str, *, workspace_id: str, lock: bool = False
    ) -> ObjectRecord | None:
        query = select(ObjectTable).where(
            ObjectTable.id == artifact_id,
            ObjectTable.workspace_id == workspace_id,
            ObjectTable.artifact_task_id.is_not(None),
        )
        if lock:
            CleanupRepository(self.session).lock_keys({f"object:{artifact_id}"})
            query = query.with_for_update()
        row = self.session.scalar(query.execution_options(populate_existing=True))
        return object_from_table(row) if row is not None else None

    def advance_metering_checkpoint(
        self, artifact_id: str, *, workspace_id: str, metered_at: datetime
    ) -> None:
        updated = self.session.scalar(
            update(ObjectTable)
            .where(
                ObjectTable.id == artifact_id,
                ObjectTable.workspace_id == workspace_id,
                ObjectTable.artifact_task_id.is_not(None),
            )
            .values(artifact_metered_at=metered_at)
            .returning(ObjectTable.id)
        )
        if updated is None:
            raise RuntimeError(f"artifact disappeared while locked: {artifact_id}")

    def mark_deletion_failed(self, artifact_id: str, *, workspace_id: str) -> None:
        updated = self.session.scalar(
            update(ObjectTable)
            .where(
                ObjectTable.id == artifact_id,
                ObjectTable.workspace_id == workspace_id,
                ObjectTable.artifact_task_id.is_not(None),
            )
            .values(artifact_deletion_failed=True)
            .returning(ObjectTable.id)
        )
        if updated is None:
            raise RuntimeError(f"artifact disappeared while locked: {artifact_id}")

    def delete_claimed(self, artifact_id: str, *, workspace_id: str) -> bool:
        WorkspaceRepository(self.session).lock_storage_accounting_owner(workspace_id)
        record = self.get(artifact_id, workspace_id=workspace_id, lock=True)
        if record is None:
            return False
        if record.cleanup_kind != OBJECT_CLEANUP_DELETE:
            raise ConflictError("artifact deletion requires a cleanup claim")
        row = self.session.get(ObjectTable, artifact_id)
        if row is None:
            return False
        self.session.delete(row)
        self.session.flush()
        return True

    def page(
        self,
        *,
        workspace_id: str,
        task_id: str | None = None,
        app_id: str | None = None,
        search: str = "",
        content_type: str = "",
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        cursor: tuple[datetime, str] | None = None,
        limit: int = 50,
    ) -> list[ObjectRecord]:
        query = select(ObjectTable).where(
            ObjectTable.workspace_id == workspace_id,
            ObjectTable.artifact_task_id.is_not(None),
            ObjectTable.write_claimed_at.is_(None),
        )
        if task_id:
            query = query.where(ObjectTable.artifact_task_id == task_id)
        if app_id:
            query = query.where(ObjectTable.artifact_app_id == app_id)
        if search:
            query = query.where(ObjectTable.artifact_filename.icontains(search, autoescape=True))
        if content_type:
            query = query.where(ObjectTable.content_type.startswith(content_type, autoescape=True))
        if created_after:
            query = query.where(ObjectTable.created_at >= created_after)
        if created_before:
            query = query.where(ObjectTable.created_at < created_before)
        if cursor:
            at, identifier = cursor
            query = query.where(
                or_(
                    ObjectTable.created_at < at,
                    (ObjectTable.created_at == at) & (ObjectTable.id < identifier),
                )
            )
        return [
            object_from_table(row)
            for row in self.session.scalars(
                query.order_by(ObjectTable.created_at.desc(), ObjectTable.id.desc()).limit(limit)
            )
        ]

    def totals(self, workspace_id: str) -> tuple[int, int]:
        count, size = self.session.execute(
            select(func.count(), func.coalesce(func.sum(ObjectTable.size), 0)).where(
                ObjectTable.workspace_id == workspace_id,
                ObjectTable.artifact_task_id.is_not(None),
                ObjectTable.write_claimed_at.is_(None),
            )
        ).one()
        return int(count), int(size)

    def accrued_cost(self, workspace_id: str, *, since: datetime) -> int:
        return int(
            self.session.scalar(
                select(func.coalesce(func.sum(BillingLedgerSegmentTable.cost_nanos), 0)).where(
                    BillingLedgerSegmentTable.workspace_id == workspace_id,
                    BillingLedgerSegmentTable.subject_type == ARTIFACT_STORAGE_SUBJECT,
                    BillingLedgerSegmentTable.segment_started_at >= since,
                )
            )
            or 0
        )

    def due(self, *, before: datetime, limit: int) -> list[tuple[str, str]]:
        return [
            (str(workspace), str(identifier))
            for workspace, identifier in self.session.execute(
                select(ObjectTable.workspace_id, ObjectTable.id)
                .where(
                    ObjectTable.artifact_metered_at <= before,
                    ObjectTable.write_claimed_at.is_(None),
                )
                .order_by(ObjectTable.artifact_metered_at, ObjectTable.id)
                .limit(limit)
            )
        ]

    def expired(self, *, now: datetime, limit: int) -> list[tuple[str, str]]:
        return [
            (str(workspace), str(identifier))
            for workspace, identifier in self.session.execute(
                select(ObjectTable.workspace_id, ObjectTable.id)
                .where(
                    ObjectTable.artifact_expires_at <= now, ObjectTable.write_claimed_at.is_(None)
                )
                .order_by(ObjectTable.artifact_expires_at, ObjectTable.id)
                .limit(limit)
            )
        ]
