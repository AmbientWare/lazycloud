from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from database.tables.billing import BillingAccountTable
from database.tables.identity import WorkspaceMemberTable, WorkspaceTable
from database.tables.storage import ObjectTable, VolumeTable
from database.tables.storage_retention import StorageRetentionPeriodTable
from shared.identity import WorkspaceRecord, WorkspaceRole, WorkspaceStatus
from shared.timestamps import to_utc
from sqlalchemy import or_, select
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class StorageRetentionRepository:
    session: Session

    def lock_workspace(self, workspace_id: str) -> WorkspaceRecord | None:
        row = self.session.scalar(
            select(WorkspaceTable)
            .where(
                WorkspaceTable.id == workspace_id,
                WorkspaceTable.status.in_(
                    (WorkspaceStatus.Active.value, WorkspaceStatus.Disabled.value)
                ),
            )
            .with_for_update(read=True, key_share=True, skip_locked=True)
            .execution_options(populate_existing=True)
        )
        return WorkspaceRecord.model_validate(row.payload) if row is not None else None

    def lock_object(self, object_id: str, *, workspace_id: str) -> bool:
        return (
            self.session.scalar(
                select(ObjectTable.id)
                .where(ObjectTable.id == object_id, ObjectTable.workspace_id == workspace_id)
                .with_for_update(skip_locked=True)
            )
            is not None
        )

    def lock_volume(self, volume_id: str, *, workspace_id: str) -> bool:
        return (
            self.session.scalar(
                select(VolumeTable.id)
                .where(VolumeTable.id == volume_id, VolumeTable.workspace_id == workspace_id)
                .with_for_update(skip_locked=True)
            )
            is not None
        )

    def account_ids(self) -> tuple[str, ...]:
        return tuple(
            self.session.scalars(
                select(BillingAccountTable.user_id).order_by(BillingAccountTable.user_id)
            )
        )

    def workspace_ids(self, *, user_id: str) -> tuple[str, ...]:
        return tuple(
            self.session.scalars(
                select(WorkspaceTable.id)
                .join(WorkspaceMemberTable, WorkspaceMemberTable.workspace_id == WorkspaceTable.id)
                .where(
                    WorkspaceMemberTable.user_id == user_id,
                    WorkspaceMemberTable.role == WorkspaceRole.Owner.value,
                    WorkspaceTable.status.in_(
                        (WorkspaceStatus.Active.value, WorkspaceStatus.Disabled.value)
                    ),
                )
                .order_by(WorkspaceTable.id)
            )
        )

    def active(self, *, user_id: str) -> StorageRetentionPeriodTable | None:
        return self.session.scalar(
            select(StorageRetentionPeriodTable)
            .where(
                StorageRetentionPeriodTable.user_id == user_id,
                StorageRetentionPeriodTable.ended_at.is_(None),
            )
            .execution_options(populate_existing=True)
        )

    def start(self, *, user_id: str, at: datetime) -> StorageRetentionPeriodTable:
        row = StorageRetentionPeriodTable(
            id=str(uuid4()), user_id=user_id, started_at=to_utc(at), notification_message_id=""
        )
        self.session.add(row)
        self.session.flush()
        return row

    def close(self, period: StorageRetentionPeriodTable, *, at: datetime) -> None:
        period.ended_at = to_utc(at)
        self.session.flush()

    def intervals(
        self, *, user_id: str, started_at: datetime, ended_at: datetime
    ) -> tuple[tuple[datetime, datetime], ...]:
        periods = self.session.scalars(
            select(StorageRetentionPeriodTable)
            .where(
                StorageRetentionPeriodTable.user_id == user_id,
                StorageRetentionPeriodTable.started_at < ended_at,
                or_(
                    StorageRetentionPeriodTable.ended_at.is_(None),
                    StorageRetentionPeriodTable.ended_at > started_at,
                ),
            )
            .order_by(StorageRetentionPeriodTable.started_at)
        )
        result: list[tuple[datetime, datetime]] = []
        for period in periods:
            start = max(to_utc(started_at), to_utc(period.started_at))
            end = min(to_utc(ended_at), to_utc(period.ended_at or ended_at))
            if start < end:
                result.append((start, end))
        return tuple(result)
