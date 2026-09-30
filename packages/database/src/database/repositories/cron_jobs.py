from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from database.mappers.apps import (
    cron_job_from_table,
    stub_from_table,
)
from database.records.apps import StubRecord
from database.repositories.deployments import DeploymentRepository
from database.repositories.identity import WorkspaceRepository
from database.tables.apps import (
    CronJobTable,
    DeploymentTable,
    StubTable,
)
from shared.cron import CronJobRecord
from shared.errors import ConflictError
from sqlalchemy import (
    delete,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert
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
        values = cron_job.model_dump() | {"revision": str(uuid4())}
        statement = insert(CronJobTable).values(**values)
        row = self.session.scalar(
            statement.on_conflict_do_update(
                index_elements=[CronJobTable.workspace_id, CronJobTable.name],
                set_={key: value for key, value in values.items() if key != "workspace_id"},
            ).returning(CronJobTable)
        )
        if row is None:
            raise RuntimeError("schedule upsert returned no row")
        return cron_job_from_table(row)

    def advance(self, cron_job: CronJobRecord, *, now: datetime, next_run_at: datetime) -> bool:
        return (
            self.session.scalar(
                update(CronJobTable)
                .where(
                    CronJobTable.workspace_id == cron_job.workspace_id,
                    CronJobTable.name == cron_job.name,
                    CronJobTable.revision == cron_job.revision,
                    CronJobTable.enabled.is_(True),
                    CronJobTable.next_run_at == cron_job.next_run_at,
                    CronJobTable.next_run_at <= now,
                )
                .values(
                    last_run_at=now,
                    next_run_at=next_run_at,
                    updated_at=now,
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

    def set_enabled(
        self, job: CronJobRecord, *, enabled: bool, next_run_at: datetime | None, now: datetime
    ) -> None:
        self.session.execute(
            update(CronJobTable)
            .where(
                CronJobTable.workspace_id == job.workspace_id,
                CronJobTable.name == job.name,
                CronJobTable.revision == job.revision,
                CronJobTable.enabled != enabled,
            )
            .values(enabled=enabled, next_run_at=next_run_at, updated_at=now, revision=str(uuid4()))
        )

    def list(
        self, *, workspace_id: str, deployment_ids: Collection[str] | None = None
    ) -> list[CronJobRecord]:
        statement = select(CronJobTable).where(CronJobTable.workspace_id == workspace_id)
        if deployment_ids is not None:
            statement = statement.where(CronJobTable.deployment_id.in_(deployment_ids))
        return [
            cron_job_from_table(row)
            for row in self.session.scalars(statement.order_by(CronJobTable.name))
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
    ) -> list[tuple[CronJobRecord, StubRecord | None]]:
        if limit <= 0:
            return []
        statement = (
            select(CronJobTable, StubTable)
            .join(DeploymentTable, DeploymentTable.id == CronJobTable.deployment_id)
            .outerjoin(StubTable, StubTable.id == DeploymentTable.stub_id)
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
        return [
            (cron_job_from_table(job), stub_from_table(stub) if stub is not None else None)
            for job, stub in self.session.execute(statement)
        ]
