from __future__ import annotations

import base64
import binascii
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from database.repositories.cron_jobs import CronJobRepository
from database.repositories.deployments import DeploymentRepository
from database.repositories.execution import CronJobRunCursor, CronJobRunRepository
from observability.workspace_changes import WorkspaceChangePublisher
from shared.contracts import ContractModel
from shared.cron import CronJobRecord, CronJobRun, next_cron_run, normalize_cron_expression
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

    def list(
        self, *, workspace: str = "default", deployment_id: str | None = None
    ) -> list[CronJobRecord]:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            return CronJobRepository(session).list(
                workspace_id=workspace_id,
                deployment_ids=[deployment_id] if deployment_id is not None else None,
            )

    def delete(self, name: str, *, workspace: str = "default") -> None:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            repository = CronJobRepository(session)
            record = repository.get(name, workspace_id=workspace_id)
            repository.delete(name, workspace_id=workspace_id)
        if record is not None:
            self.publish_change(record, WorkspaceChangeType.Deleted)

    def apply_deployments_in_session(
        self, session: Session, deployments: Sequence[Deployment], *, workspace_id: str
    ) -> None:
        now = utc_now()
        repository = CronJobRepository(session)
        deleted = {item.id for item in deployments if item.deleted_at is not None}
        if deleted:
            repository.delete_for_deployments(deleted, workspace_id=workspace_id)
        active = {item.id: item.active for item in deployments if item.id not in deleted}
        if not active:
            return
        for record in repository.list(workspace_id=workspace_id, deployment_ids=active):
            enabled = active[record.deployment_id]
            if record.enabled != enabled:
                repository.set_enabled(
                    record,
                    enabled=enabled,
                    next_run_at=next_cron_run(record.cron, now) if enabled else record.next_run_at,
                    now=now,
                )

    def list_cron_job_runs(
        self,
        *,
        workspace_id: str,
        limit: int = 100,
        cursor: str | None = None,
    ) -> CronJobRunPage:
        with self.context.database.session() as session:
            page = CronJobRunRepository(session).page(
                workspace_id=workspace_id,
                cursor=_decode_cron_job_run_cursor(cursor),
                limit=min(max(limit, 1), 1_000),
            )
        return CronJobRunPage(
            data=page.data,
            next=_encode_cron_job_run_cursor(page.next),
        )

    def advance_in_session(self, session: Session, job: CronJobRecord, *, now: datetime) -> bool:
        return CronJobRepository(session).advance(
            job, now=now, next_run_at=next_cron_run(job.cron, now)
        )

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


class CronJobRunCursorPayload(ContractModel):
    created_at: datetime
    id: str


@dataclass(frozen=True, slots=True)
class CronJobRunPage:
    data: tuple[CronJobRun, ...]
    next: str = ""


def _encode_cron_job_run_cursor(cursor: CronJobRunCursor | None) -> str:
    if cursor is None:
        return ""
    payload = CronJobRunCursorPayload(
        created_at=cursor.created_at,
        id=cursor.id,
    ).model_dump_json()
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_cron_job_run_cursor(value: str | None) -> CronJobRunCursor | None:
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = CronJobRunCursorPayload.model_validate_json(
            base64.urlsafe_b64decode(padded.encode())
        )
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise InvalidInputError("invalid cron job run cursor") from exc
    return CronJobRunCursor(created_at=payload.created_at, id=payload.id)
