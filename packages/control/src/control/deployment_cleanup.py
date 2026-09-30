from __future__ import annotations

from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from database.repositories.apps import AppDeploymentIntentRepository, AppRepository
from database.repositories.cron_jobs import CronJobRepository
from database.repositories.deployment_effects import (
    DeploymentAction,
    DeploymentEffect,
    DeploymentEffectRepository,
)
from database.repositories.deployments import DeploymentRepository
from shared.app_lifecycle import AppDeploymentIntentTarget
from shared.errors import ConflictError
from shared.timestamps import utc_now

from control.context import ControlContext
from control.deployment_effects import DeploymentEffects


@dataclass(frozen=True, slots=True)
class AppDeploymentLifecycleService:
    context: ControlContext
    effects: DeploymentEffects

    def apply_intents(
        self, *, app_id: str, workspace_id: str, revision: int, claim_id: str
    ) -> bool:
        now = utc_now()
        with self.context.database.session() as session:
            app = AppRepository(session).get_for_update(
                app_id, workspace_id=workspace_id, include_deleted=True
            )
            if (
                app is None
                or app.lifecycle_revision != revision
                or app.reconcile_claim_id != claim_id
            ):
                return False
            deployments = DeploymentRepository(session)
            publications = AppDeploymentIntentRepository(session)
            intents = publications.list(app_id=app_id)
            schedules = CronJobRepository(session)
            cron_by_deployment = {
                record.deployment_id: record
                for record in schedules.list(
                    workspace_id=workspace_id,
                    deployment_ids=[intent.deployment_id for intent in intents],
                )
            }
            pending: list[DeploymentEffect] = []
            for intent in intents:
                if intent.workspace_change_published_at is not None:
                    continue
                deployment = deployments.get(
                    intent.deployment_id, workspace_id=workspace_id, include_deleted=True
                )
                if deployment is None or deployment.app_id != app_id:
                    raise ConflictError("app lifecycle deployment ownership changed")
                deleted = intent.target is AppDeploymentIntentTarget.Deleted
                active = intent.target is AppDeploymentIntentTarget.Active
                if deployment.deleted_at is None and (deleted or deployment.active != active):
                    deployment = deployments.upsert(
                        deployment.model_copy(
                            update={
                                "active": active,
                                "deleted_at": now if deleted else None,
                                "updated_at": now,
                            }
                        ),
                        workspace_id=workspace_id,
                    )
                schedule = cron_by_deployment.get(deployment.id)
                if schedule is not None:
                    if deleted:
                        schedules.delete(schedule.name, workspace_id=workspace_id)
                    elif schedule.enabled != active:
                        schedules.upsert(
                            schedule.model_copy(update={"enabled": active, "updated_at": now}),
                            workspace_id=workspace_id,
                        )
                effect = DeploymentEffectRepository(session).record(
                    deployment,
                    workspace_id=workspace_id,
                    action=DeploymentAction.Deleted
                    if deleted
                    else DeploymentAction.Started
                    if active
                    else DeploymentAction.Stopped,
                    app_revision=revision,
                    event_id=intent.event_id
                    or str(
                        uuid5(
                            NAMESPACE_URL,
                            f"lazycloud:app-lifecycle:deployment:{app_id}:{revision}:"
                            f"{deployment.id}:{intent.target.value}",
                        )
                    ),
                    created_at=intent.event_created_at or now,
                )
                publications.update_publication(
                    intent.model_copy(
                        update={
                            "event_id": effect.id,
                            "event_created_at": effect.created_at,
                            "updated_at": now,
                        }
                    )
                )
                pending.append(effect)
        self.effects.finish(pending)
        return True

    def reconcile_placement_and_routes(self, *, workspace_id: str, required: bool) -> None:
        if self.effects.placement_resources is not None:
            self.effects.placement_resources.reconcile_deployments(
                workspace=workspace_id, required=required
            )
