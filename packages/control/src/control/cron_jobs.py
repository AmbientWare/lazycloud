from __future__ import annotations

from dataclasses import dataclass

from database.repositories.cron_jobs import CronJobRepository
from database.repositories.deployments import DeploymentRepository
from observability.workspace_changes import WorkspaceChangePublisher
from shared.cron import CronJobRecord, next_cron_run, normalize_cron_expression
from shared.deployment_records import (
    Deployment,
)
from shared.errors import ConflictError, InvalidInputError
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.timestamps import utc_now
from sqlalchemy.orm import Session

from control.context import ControlContext


@dataclass(slots=True)
class CronJobService:
    context: ControlContext
    workspace_changes: WorkspaceChangePublisher | None = None

    def set_in_session(
        self,
        session: Session,
        deployment: Deployment,
        *,
        cron: str | None,
        workspace_id: str,
    ) -> CronJobRecord | None:
        try:
            normalized = normalize_cron_expression(cron) if cron else None
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        repository = CronJobRepository(session)
        if not DeploymentRepository(session).lock_live(deployment.id, workspace_id=workspace_id):
            raise ConflictError("cannot change the schedule of a deleted deployment")
        if normalized is None:
            repository.delete(deployment.subdomain, workspace_id=workspace_id)
            return None
        return repository.upsert(
            CronJobRecord(
                workspace_id=workspace_id,
                name=deployment.subdomain,
                cron=normalized,
                deployment_id=deployment.id,
                enabled=deployment.active,
                next_run_at=next_cron_run(normalized),
            ),
            workspace_id=workspace_id,
        )

    def list(self, *, workspace: str = "default") -> list[CronJobRecord]:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            records = CronJobRepository(session).list(workspace_id=workspace_id)
        records.sort(key=lambda item: item.name)
        return records

    def delete(self, name: str, *, workspace: str = "default") -> None:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            repository = CronJobRepository(session)
            record = repository.get(name, workspace_id=workspace_id)
            repository.delete(name, workspace_id=workspace_id)
        if record is not None:
            self.publish_change(record, WorkspaceChangeType.Deleted)

    def set_enabled_in_session(
        self, session: Session, deployment_id: str, *, workspace_id: str, enabled: bool
    ) -> list[CronJobRecord]:
        now = utc_now()
        repository = CronJobRepository(session)
        updated: list[CronJobRecord] = []
        for record in repository.list(workspace_id=workspace_id, deployment_ids=[deployment_id]):
            record.enabled = enabled
            if enabled:
                record.next_run_at = next_cron_run(record.cron, now)
            record.updated_at = now
            updated.append(repository.upsert(record, workspace_id=workspace_id))
        return updated

    def publish_change(
        self,
        record: CronJobRecord,
        change: WorkspaceChangeType,
    ) -> None:
        if self.workspace_changes is None:
            return
        self.workspace_changes.emit_change(
            workspace_id=record.workspace_id,
            topic=WorkspaceChangeTopic.Deployments,
            change=change,
            resource_id=record.name,
            deployment_id=record.deployment_id,
        )
