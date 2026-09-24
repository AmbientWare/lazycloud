"""What the dashboard shows about a devbox: how to connect and whether it is running."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from control.deployment_resources import DeploymentResource
from database.repositories.apps import DeploymentRepository
from database.repositories.disks import DiskRepository
from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import ContainerRepository, LiveContainer
from shared.containers import ContainerStatus
from shared.deployment_records import resolve_pod_role
from shared.deployments import DeploymentKind, DevboxState, PodRole
from shared.errors import NotFoundError
from shared.http.deployments import DevboxDiskResponse, DevboxResponse
from shared.ssh import ssh_host_alias
from shared.timestamps import to_utc, utc_now

from database import DatabaseClient
from execution.containers.runtime_state import PodKeepAliveReader


@dataclass(frozen=True, slots=True)
class DevboxService:
    database: DatabaseClient
    keep_alive: PodKeepAliveReader
    clock: Callable[[], datetime] = field(default=utc_now)

    def describe(self, resource: DeploymentResource) -> DevboxResponse | None:
        """The devbox block for this deployment, or None when it is not a devbox."""
        deployment = resource.deployment
        if resolve_pod_role(deployment.kind, deployment.spec.role) is not PodRole.Devbox:
            return None
        stub = resource.stub
        workspace_id = stub.workspace_id
        root = next((disk for disk in stub.config.disks if disk.is_root), None)
        with self.database.session() as session:
            workspace_name = (
                WorkspaceRepository(session).names_for_ids([workspace_id]).get(workspace_id)
            )
            if workspace_name is None:
                raise NotFoundError(f"workspace not found: {workspace_id}")
            ambiguous = DeploymentRepository(session).name_live_in_other_app(
                deployment.name,
                kind=DeploymentKind.Pod,
                app_id=resource.app.id,
                workspace_id=workspace_id,
            )
            container = (
                ContainerRepository(session).newest_live_for_stub(stub.id)
                if deployment.active
                else None
            )
            disk = (
                DiskRepository(session).get(root.name, workspace_id=workspace_id)
                if root is not None
                else None
            )
        command = ["lazycloud", "ssh", deployment.name]
        if ambiguous:
            command += ["--app", resource.app.name]
        connections, idle_deadline = self._keep_alive(
            container,
            workspace_id=workspace_id,
            stub_id=stub.id,
            keep_warm_seconds=stub.config.runtime.keep_warm,
        )
        return DevboxResponse(
            ssh_command=shlex.join(command),
            ssh_host=ssh_host_alias(workspace_name, resource.app.name, deployment.name),
            state=_state(container),
            open_connections=connections,
            idle_deadline=idle_deadline,
            disk=(
                DevboxDiskResponse(
                    name=disk.name,
                    size_bytes=disk.size_bytes,
                    stored_bytes=disk.stored_bytes,
                    generation=disk.generation,
                    status=disk.status,
                )
                if disk is not None
                else None
            ),
        )

    def _keep_alive(
        self,
        container: LiveContainer | None,
        *,
        workspace_id: str,
        stub_id: str,
        keep_warm_seconds: int,
    ) -> tuple[int, datetime | None]:
        if container is None or container.status is not ContainerStatus.Running:
            return 0, None
        state = self.keep_alive.keep_alive(
            workspace_id=workspace_id, stub_id=stub_id, container_id=container.id
        )
        if state.connections > 0 or keep_warm_seconds < 0:
            return state.connections, None
        now = self.clock()
        # The autoscaler spares a container for its first keep-warm window and
        # for as long as the marker lives, so it stops at the later of the two.
        candidates = [now + timedelta(seconds=state.idle_seconds)] if state.idle_seconds else []
        if container.started_at is not None:
            candidates.append(to_utc(container.started_at) + timedelta(seconds=keep_warm_seconds))
        return 0, max(candidates) if candidates else None


def _state(container: LiveContainer | None) -> DevboxState:
    if container is None:
        return DevboxState.Stopped
    if container.status is ContainerStatus.Running:
        return DevboxState.Running
    return DevboxState.Starting


__all__ = ["DevboxService"]
