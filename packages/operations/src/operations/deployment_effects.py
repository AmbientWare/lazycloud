from __future__ import annotations

import logging
from dataclasses import dataclass
from time import monotonic
from uuid import UUID, uuid5

from control.context import ControlContext
from control.deployment_effects import (
    DeploymentExecutionEffects,
    DeploymentPlacementResourceManager,
)
from database.repositories.deployment_effects import (
    DeploymentAction,
    DeploymentEffect,
    DeploymentEffectRepository,
)
from database.repositories.deployments import DeploymentRepository
from observability.workspace_changes import WorkspaceChangePublisher
from shared.errors import UpstreamUnavailableError
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.timestamps import utc_now

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeploymentEffects:
    context: ControlContext
    containers: DeploymentExecutionEffects
    workspace_changes: WorkspaceChangePublisher | None = None
    placement_resources: DeploymentPlacementResourceManager | None = None

    def reconcile_pending(self, *, limit: int = 25) -> None:
        with self.context.database.session() as session:
            abandoned = DeploymentEffectRepository(session).expired_preparations(
                now=utc_now(), limit=limit
            )
        if self.placement_resources is not None:
            for workspace_id in {workspace_id for _, workspace_id in abandoned}:
                self.placement_resources.reconcile_deployments(
                    workspace=workspace_id, required=False
                )
        if abandoned:
            with self.context.database.session() as session:
                DeploymentEffectRepository(session).delete_preparations(
                    [identifier for identifier, _ in abandoned]
                )
        deadline = monotonic() + 30
        for _ in range(limit):
            if monotonic() >= deadline:
                break
            with self.context.database.session() as session:
                effects = DeploymentEffectRepository(session).due(now=utc_now(), limit=1)
            if not effects:
                break
            try:
                self.finish(effects)
            except Exception:
                LOGGER.exception("deployment effects remain pending: %s", effects[0].id)

    def finish(self, effects: list[DeploymentEffect]) -> None:
        for effect in effects:
            with self.context.database.session() as session:
                targets = DeploymentEffectRepository(session).targets(effect.id)
                deployment = DeploymentRepository(session).get(
                    effect.deployment_id, workspace_id=effect.workspace_id, include_deleted=True
                )
            self.containers.stop_containers(targets, confirm=False)
            if effect.app_revision is None and effect.action is DeploymentAction.Deleted:
                self.containers.delete_deployment_execution(
                    workspace_id=effect.workspace_id, deployment_ids=[effect.deployment_id]
                )
            if effect.app_revision is None and self.placement_resources is not None:
                self.placement_resources.reconcile_deployments(
                    workspace=effect.workspace_id,
                    required=effect.action in {DeploymentAction.Created, DeploymentAction.Started},
                )
            if self.workspace_changes is not None:
                notices = [
                    (
                        WorkspaceChangeTopic.Deployments,
                        WorkspaceChangeType.Created
                        if effect.action is DeploymentAction.Created
                        else WorkspaceChangeType.Deleted
                        if effect.action is DeploymentAction.Deleted
                        else WorkspaceChangeType.Updated,
                        effect.deployment_id,
                    )
                ]
                if deployment is not None:
                    if effect.action is DeploymentAction.Created and deployment.app_id:
                        notices.append(
                            (
                                WorkspaceChangeTopic.Apps,
                                WorkspaceChangeType.Created
                                if effect.app_created
                                else WorkspaceChangeType.Updated,
                                deployment.app_id,
                            )
                        )
                    if (
                        effect.action in {DeploymentAction.Created, DeploymentAction.Scaled}
                        and deployment.stub_id
                    ):
                        notices.append(
                            (
                                WorkspaceChangeTopic.Workloads,
                                WorkspaceChangeType.Created
                                if effect.action is DeploymentAction.Created
                                else WorkspaceChangeType.Updated,
                                deployment.stub_id,
                            )
                        )
                if effect.source_stub_id:
                    notices.append(
                        (
                            WorkspaceChangeTopic.Workloads,
                            WorkspaceChangeType.Deleted
                            if effect.source_deleted
                            else WorkspaceChangeType.Updated,
                            effect.source_stub_id,
                        )
                    )
                for topic, change, resource_id in notices:
                    published = self.workspace_changes.emit_change(
                        workspace_id=effect.workspace_id,
                        topic=topic,
                        change=change,
                        resource_id=resource_id,
                        app_id=deployment.app_id if deployment else None,
                        deployment_id=effect.deployment_id,
                        stub_id=resource_id
                        if topic is WorkspaceChangeTopic.Workloads
                        else deployment.stub_id
                        if topic is WorkspaceChangeTopic.Deployments and deployment
                        else None,
                        occurred_at=effect.created_at,
                        event_id=effect.id
                        if topic is WorkspaceChangeTopic.Deployments
                        else str(uuid5(UUID(effect.id), f"{topic.value}:{resource_id}")),
                    )
                    if published is None:
                        raise UpstreamUnavailableError("deployment publication remains pending")
            with self.context.database.session() as session:
                DeploymentEffectRepository(session).complete(effect, now=utc_now())
