from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from database.records.apps import AppDeploymentIntentRecord
from database.repositories.apps import (
    AppDeploymentIntentRepository,
    CronJobRepository,
    DeploymentRepository,
)
from database.repositories.execution import EventRepository
from observability.workspace_changes import WorkspaceChangePublisher
from shared.app_lifecycle import AppDeploymentIntentTarget
from shared.deployment_records import Deployment
from shared.errors import ConflictError, UpstreamUnavailableError
from shared.events import Event
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.timestamps import utc_now
from sqlalchemy.orm import Session

from control.context import ControlContext


class DeploymentPlacementResourceManager(Protocol):
    def reconcile_deployments(
        self,
        *,
        workspace: str,
        required: bool = True,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class AppDeploymentLifecycleService:
    context: ControlContext
    workspace_changes: WorkspaceChangePublisher | None = None
    placement_resources: DeploymentPlacementResourceManager | None = None

    def apply_intents(
        self,
        *,
        app_id: str,
        workspace_id: str,
        intents: list[AppDeploymentIntentRecord],
    ) -> None:
        now = utc_now()
        with self.context.database.session() as session:
            deployments = DeploymentRepository(session)
            cron_jobs = CronJobRepository(session)
            publications = AppDeploymentIntentRepository(session)
            events = EventRepository(session)
            cron_by_deployment = {
                cron.deployment_id: cron
                for cron in cron_jobs.list(workspace_id=workspace_id)
                if cron.deployment_id
            }
            for intent in intents:
                deployment = deployments.get(
                    intent.deployment_id,
                    workspace_id=workspace_id,
                    include_deleted=True,
                )
                if deployment is None:
                    publications.update_publication(
                        intent.model_copy(
                            update={
                                "workspace_change_published_at": now,
                                "updated_at": now,
                            }
                        )
                    )
                    continue
                if deployment.app_id != app_id:
                    raise ConflictError("app lifecycle intent deployment ownership changed")
                change = _apply_deployment_target(deployment, intent.target, now=now)
                if change is not None:
                    deployment = deployments.records.upsert(
                        deployment,
                        workspace_id=workspace_id,
                        name=deployment.name,
                        status="active" if deployment.active else "inactive",
                    )
                action = _deployment_action(intent.target)
                event_id = intent.event_id or str(
                    uuid5(
                        NAMESPACE_URL,
                        "lazycloud:app-lifecycle:deployment:"
                        f"{app_id}:{intent.operation_revision}:"
                        f"{deployment.id}:{intent.target.value}",
                    )
                )
                event_created_at = intent.event_created_at or now
                events.append(
                    Event(
                        id=event_id,
                        action=action,
                        resource_type="deployment",
                        resource_id=deployment.id,
                        message=(
                            f"{action.removeprefix('deployment.')} deployment {deployment.name}"
                        ),
                        created_at=event_created_at,
                    ),
                    workspace_id=workspace_id,
                )
                publications.update_publication(
                    intent.model_copy(
                        update={
                            "event_id": event_id,
                            "event_created_at": event_created_at,
                            "updated_at": now,
                        }
                    )
                )
                cron_job = cron_by_deployment.get(deployment.id)
                if cron_job is None:
                    continue
                if intent.target is AppDeploymentIntentTarget.Deleted:
                    cron_jobs.records.delete(cron_job.name, workspace_id=workspace_id)
                    continue
                enabled = intent.target is AppDeploymentIntentTarget.Active
                if cron_job.enabled is enabled:
                    continue
                cron_job.enabled = enabled
                cron_job.updated_at = now
                cron_jobs.upsert(cron_job, workspace_id=workspace_id)
        self._publish_pending_changes(app_id=app_id, workspace_id=workspace_id)

    def _publish_pending_changes(self, *, app_id: str, workspace_id: str) -> None:
        with self.context.database.session() as session:
            intents = AppDeploymentIntentRepository(session).list(app_id=app_id)
        for intent in intents:
            if intent.workspace_change_published_at is not None:
                continue
            if intent.event_id is None or intent.event_created_at is None:
                raise ConflictError("app deployment lifecycle publication is incomplete")
            if self.workspace_changes is not None:
                published = self.workspace_changes.emit_change(
                    workspace_id=workspace_id,
                    topic=WorkspaceChangeTopic.Deployments,
                    change=_workspace_change(intent.target),
                    resource_id=intent.deployment_id,
                    app_id=app_id,
                    deployment_id=intent.deployment_id,
                    occurred_at=intent.event_created_at,
                    event_id=intent.event_id,
                )
                if published is None:
                    raise UpstreamUnavailableError(
                        "deployment lifecycle change publication is unavailable"
                    )
            with self.context.database.session() as session:
                repository = AppDeploymentIntentRepository(session)
                current = repository.get_for_update(
                    app_id=app_id,
                    deployment_id=intent.deployment_id,
                )
                if current is None or current.event_id != intent.event_id:
                    continue
                repository.update_publication(
                    current.model_copy(
                        update={
                            "workspace_change_published_at": utc_now(),
                            "updated_at": utc_now(),
                        }
                    )
                )

    def reconcile_placement_and_routes(
        self,
        *,
        workspace_id: str,
        required: bool,
    ) -> None:
        if self.placement_resources is None:
            return
        self.placement_resources.reconcile_deployments(
            workspace=workspace_id,
            required=required,
        )


def _apply_deployment_target(
    deployment: Deployment,
    target: AppDeploymentIntentTarget,
    *,
    now: datetime,
) -> WorkspaceChangeType | None:
    if target is AppDeploymentIntentTarget.Deleted:
        if deployment.deleted_at is not None:
            return None
        deployment.active = False
        deployment.deleted_at = now
        deployment.updated_at = now
        return WorkspaceChangeType.Deleted
    active = target is AppDeploymentIntentTarget.Active
    if deployment.deleted_at is not None or deployment.active is active:
        return None
    deployment.active = active
    deployment.updated_at = now
    return WorkspaceChangeType.Updated


def _deployment_action(target: AppDeploymentIntentTarget) -> str:
    if target is AppDeploymentIntentTarget.Active:
        return "deployment.started"
    if target is AppDeploymentIntentTarget.Inactive:
        return "deployment.stopped"
    return "deployment.deleted"


def _workspace_change(target: AppDeploymentIntentTarget) -> WorkspaceChangeType:
    return (
        WorkspaceChangeType.Deleted
        if target is AppDeploymentIntentTarget.Deleted
        else WorkspaceChangeType.Updated
    )


def delete_deployment_cron_jobs(
    session: Session,
    *,
    deployment_ids: set[str],
    workspace_id: str | None = None,
) -> None:
    if not deployment_ids:
        return
    repository = CronJobRepository(session)
    records = (
        repository.list(workspace_id=workspace_id)
        if workspace_id is not None
        else repository.list_across_workspaces()
    )
    for record in records:
        if record.deployment_id in deployment_ids:
            repository.records.delete(record.name, workspace_id=record.workspace_id)


__all__ = [
    "AppDeploymentLifecycleService",
    "DeploymentPlacementResourceManager",
    "delete_deployment_cron_jobs",
]
