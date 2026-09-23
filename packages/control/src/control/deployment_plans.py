from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from database.records.apps import AppRecord
from database.repositories.apps import AppRepository, CronJobRepository
from database.repositories.deployment_plans import (
    DeploymentPlanRepository,
    PruneOperation,
    WorkloadSummary,
)
from database.repositories.execution import EventRepository
from observability.workspace_changes import WorkspaceChangePublisher
from pydantic import JsonValue
from shared.app_lifecycle import UNFINISHED_APP_LIFECYCLE_STATES
from shared.errors import ConflictError, UpstreamUnavailableError
from shared.events import Event
from shared.http.deployment_plans import (
    DeploymentPlanAction,
    DeploymentPlanItem,
    DeploymentPlanRequest,
    DeploymentPlanResponse,
    DeploymentPruneRequest,
    DeploymentPruneResponse,
)
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.timestamps import utc_now

from control.context import ControlContext
from control.deployment_cleanup import DeploymentPlacementResourceManager

LOGGER = logging.getLogger(__name__)


class DeploymentCleanupEffects(Protocol):
    def delete_deployment_execution(
        self, *, workspace_id: str, deployment_ids: list[str]
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class DeploymentPlanService:
    context: ControlContext
    execution: DeploymentCleanupEffects
    workspace_changes: WorkspaceChangePublisher | None = None
    placement_resources: DeploymentPlacementResourceManager | None = None

    def plan(self, request: DeploymentPlanRequest, *, workspace: str) -> DeploymentPlanResponse:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            app = AppRepository(session).get_by_name(request.app, workspace_id=workspace_id)
            summaries = (
                DeploymentPlanRepository(session).summaries(
                    workspace_id=workspace_id,
                    app_id=app.id,
                )
                if app is not None
                else []
            )
        current = {(item.kind, item.name): item for item in summaries}
        desired = {(item.kind, item.name) for item in request.workloads}
        data = [
            DeploymentPlanItem(
                kind=kind,
                name=name,
                action=(
                    DeploymentPlanAction.Redeploy
                    if (kind, name) in current
                    else DeploymentPlanAction.Add
                ),
                versions=current[kind, name].versions if (kind, name) in current else 0,
            )
            for kind, name in sorted(desired)
        ]
        data.extend(
            DeploymentPlanItem(
                kind=item.kind,
                name=item.name,
                versions=item.versions,
                action=DeploymentPlanAction.Remove
                if request.prune
                else DeploymentPlanAction.Retain,
            )
            for item in summaries
            if (item.kind, item.name) not in desired
        )
        return DeploymentPlanResponse(
            app=request.app,
            app_id=app.id if app else None,
            prune=request.prune,
            snapshot=_snapshot(workspace_id, app, summaries),
            data=data,
        )

    def prune(self, request: DeploymentPruneRequest, *, workspace: str) -> DeploymentPruneResponse:
        digest = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        operation_id = str(request.operation_id)
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            app = AppRepository(session).get_by_name(
                request.app,
                workspace_id=workspace_id,
                for_update=True,
            )
            repository = DeploymentPlanRepository(session)
            operation = repository.operation(operation_id, workspace_id=workspace_id)
            if operation is not None:
                if operation.request_digest != digest:
                    raise ConflictError("prune operation was already used with another request")
            elif app is None:
                if request.app_id is not None or request.workloads or request.deployment_ids:
                    raise ConflictError("app changed since deployment planning; run deploy again")
                return DeploymentPruneResponse(
                    operation_id=request.operation_id,
                    app=request.app,
                    removed_versions=0,
                    complete=True,
                )
            else:
                if app.lifecycle_state in UNFINISHED_APP_LIFECYCLE_STATES:
                    raise ConflictError("app lifecycle operation is in progress; pruning refused")
                if request.app_id is not None and request.app_id != app.id:
                    raise ConflictError("app changed since deployment planning; run deploy again")
                submitted_ids = [str(item) for item in request.deployment_ids]
                submitted = repository.submitted_workloads(
                    workspace_id=workspace_id,
                    app_id=app.id,
                    deployment_ids=submitted_ids,
                )
                desired = {(item.kind, item.name) for item in request.workloads}
                if len(submitted) != len(desired) or set(submitted) != desired:
                    raise ConflictError(
                        "pruning requires one successful deployment for every workload"
                    )
                baseline = repository.summaries(
                    workspace_id=workspace_id,
                    app_id=app.id,
                    exclude_ids=submitted_ids,
                )
                if (
                    _snapshot(workspace_id, app if request.app_id else None, baseline)
                    != request.snapshot
                ):
                    raise ConflictError(
                        "app changed since deployment planning; no workloads were pruned"
                    )
                now = utc_now()
                retired = repository.retire(
                    workspace_id=workspace_id,
                    app_id=app.id,
                    workloads={(item.kind, item.name) for item in baseline} - desired,
                    now=now,
                )
                target_ids = [deployment_id for deployment_id, _ in retired]
                CronJobRepository(session).delete_for_deployments(
                    set(target_ids),
                    workspace_id=workspace_id,
                )
                operation = repository.create_operation(
                    operation_id=operation_id,
                    workspace_id=workspace_id,
                    app_id=app.id,
                    request_digest=digest,
                    deployment_ids=target_ids,
                    now=now,
                )
                for deployment_id, name in retired:
                    EventRepository(session).append(
                        Event(
                            id=_event_id(operation_id, deployment_id),
                            action="deployment.deleted",
                            resource_type="deployment",
                            resource_id=deployment_id,
                            message=f"deleted deployment {name}",
                            created_at=now,
                        ),
                        workspace_id=workspace_id,
                    )
        self._finish(operation)
        with self.context.database.session() as session:
            count = len(DeploymentPlanRepository(session).targets(operation_id))
        return DeploymentPruneResponse(
            operation_id=request.operation_id,
            app=request.app,
            removed_versions=count,
            complete=True,
        )

    def reconcile_pending(self, *, limit: int = 25) -> None:
        now = utc_now()
        with self.context.database.session() as session:
            pending = DeploymentPlanRepository(session).due(
                now=now,
                retry_at=now + timedelta(seconds=60),
                limit=limit,
            )
        for operation in pending:
            try:
                self._finish(operation)
            except Exception:
                LOGGER.exception("deployment prune cleanup failed: %s", operation.id)

    def _finish(self, operation: PruneOperation) -> None:
        if operation.complete:
            return
        with self.context.database.session() as session:
            targets = DeploymentPlanRepository(session).targets(operation.id)
        if targets:
            self.execution.delete_deployment_execution(
                workspace_id=operation.workspace_id,
                deployment_ids=targets,
            )
            if self.placement_resources is not None:
                self.placement_resources.reconcile_deployments(
                    workspace=operation.workspace_id,
                    required=False,
                )
            if self.workspace_changes is not None:
                for deployment_id in targets:
                    published = self.workspace_changes.emit_change(
                        workspace_id=operation.workspace_id,
                        topic=WorkspaceChangeTopic.Deployments,
                        change=WorkspaceChangeType.Deleted,
                        resource_id=deployment_id,
                        app_id=operation.app_id,
                        deployment_id=deployment_id,
                        occurred_at=operation.created_at,
                        event_id=_event_id(operation.id, deployment_id),
                    )
                    if published is None:
                        raise UpstreamUnavailableError(
                            "deployment prune publication is unavailable"
                        )
        with self.context.database.session() as session:
            DeploymentPlanRepository(session).complete(
                operation.id,
                workspace_id=operation.workspace_id,
                now=utc_now(),
            )


def _snapshot(workspace_id: str, app: AppRecord | None, summaries: list[WorkloadSummary]) -> str:
    value: list[JsonValue] = [
        workspace_id,
        app.id if app else None,
        app.lifecycle_revision if app else 0,
        [[item.kind.value, item.name, item.versions, item.fingerprint] for item in summaries],
    ]
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def _event_id(operation_id: str, deployment_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"deployment-prune:{operation_id}:{deployment_id}"))
