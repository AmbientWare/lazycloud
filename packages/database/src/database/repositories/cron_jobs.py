from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from database.mappers.apps import (
    cron_job_from_table,
    write_cron_job_row,
)
from database.repositories.deployments import DeploymentRepository
from database.repositories.identity import WorkspaceRepository
from database.tables.apps import (
    CronJobTable,
)
from shared.cron import CronJobRecord
from shared.errors import ConflictError
from sqlalchemy import (
    delete,
    select,
    text,
    update,
)
from sqlalchemy.orm import Session


@dataclass(slots=True)
class CronJobRepository:
    session: Session

    def delete_for_deployments(
        self, deployment_ids: set[str], *, workspace_id: str | None = None
    ) -> None:
        statement = delete(CronJobTable).where(CronJobTable.deployment_id.in_(deployment_ids))
        if workspace_id is not None:
            statement = statement.where(CronJobTable.workspace_id == workspace_id)
        self.session.execute(statement)

    def upsert(self, cron_job: CronJobRecord, *, workspace_id: str) -> CronJobRecord:
        WorkspaceRepository(self.session).lock_active_owner(workspace_id)
        if cron_job.workspace_id != workspace_id:
            raise ConflictError("cron job ownership cannot change")
        if not DeploymentRepository(self.session).lock_live(
            cron_job.deployment_id, workspace_id=workspace_id
        ):
            raise ConflictError("cannot change the schedule of a deleted deployment")
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"cron-job:{workspace_id}:{cron_job.name}"},
        )
        row = self.session.scalar(
            select(CronJobTable).where(
                CronJobTable.workspace_id == workspace_id,
                CronJobTable.name == cron_job.name,
            )
        )
        if row is None:
            row = CronJobTable(workspace_id=workspace_id)
            self.session.add(row)
        write_cron_job_row(row, cron_job)
        self.session.flush()
        return cron_job_from_table(row)

    def record_run(self, cron_job: CronJobRecord, *, workspace_id: str) -> bool:
        if cron_job.workspace_id != workspace_id:
            raise ConflictError("cron job ownership cannot change")
        return (
            self.session.scalar(
                update(CronJobTable)
                .where(
                    CronJobTable.workspace_id == workspace_id,
                    CronJobTable.name == cron_job.name,
                    CronJobTable.deployment_id == cron_job.deployment_id,
                    CronJobTable.cron == cron_job.cron,
                )
                .values(
                    last_run_at=cron_job.last_run_at,
                    next_run_at=cron_job.next_run_at,
                    updated_at=cron_job.updated_at,
                )
                .returning(CronJobTable.name)
            )
            is not None
        )

    def get(self, name: str, *, workspace_id: str) -> CronJobRecord | None:
        row = self.session.scalar(
            select(CronJobTable).where(
                CronJobTable.workspace_id == workspace_id,
                CronJobTable.name == name,
            )
        )
        return cron_job_from_table(row) if row is not None else None

    def list(
        self, *, workspace_id: str, deployment_ids: Collection[str] | None = None
    ) -> list[CronJobRecord]:
        statement = select(CronJobTable).where(CronJobTable.workspace_id == workspace_id)
        if deployment_ids is not None:
            statement = statement.where(CronJobTable.deployment_id.in_(deployment_ids))
        return [
            cron_job_from_table(row)
            for row in self.session.scalars(
                statement.order_by(CronJobTable.created_at.desc(), CronJobTable.id)
            )
        ]

    def list_across_workspaces(self) -> list[CronJobRecord]:
        """Scheduler-owned listing over every workspace's cron jobs."""
        return [
            cron_job_from_table(row)
            for row in self.session.scalars(
                select(CronJobTable).order_by(CronJobTable.created_at.desc(), CronJobTable.id)
            )
        ]

    def delete(self, name: str, *, workspace_id: str) -> None:
        self.session.execute(
            delete(CronJobTable).where(
                CronJobTable.workspace_id == workspace_id,
                CronJobTable.name == name,
            )
        )

    def due_across_workspaces(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> list[CronJobRecord]:
        if limit <= 0:
            return []
        statement = (
            select(CronJobTable)
            .where(
                CronJobTable.enabled.is_(True),
                CronJobTable.next_run_at.is_not(None),
                CronJobTable.next_run_at <= now,
            )
            .order_by(
                CronJobTable.next_run_at.asc().nulls_first(),
                CronJobTable.id.asc(),
            )
            .limit(limit)
        )
        return [cron_job_from_table(row) for row in self.session.scalars(statement)]
