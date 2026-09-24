"""What the dashboard shows about a devbox: how to connect and whether it is running."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from control.deployment_resources import DeploymentResource
from database.repositories.apps import DeploymentRepository
from database.repositories.disks import DiskRepository
from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import ContainerRepository, LiveContainer
from observability.stream_state import RedisEventStreamRepository
from shared.containers import ContainerExecutionPhase, ContainerStatus
from shared.deployment_records import resolve_pod_role
from shared.deployments import DeploymentKind, DevboxPhase, DevboxState, PodRole
from shared.errors import NotFoundError
from shared.http.deployments import DevboxDiskResponse, DevboxResponse
from shared.realtime.contracts import EventRecordType
from shared.realtime.streams import EventHistoryQuery
from shared.ssh import ssh_host_alias
from shared.timestamps import to_utc, utc_now
from storage.disk_volumes import DiskWorkerAbsence, holder_keeps_disk

from database import DatabaseClient
from execution.containers.runtime_state import PodKeepAliveReader


class ContainerStartupReader(Protocol):
    def finished_steps(
        self, *, workspace_id: str, stub_id: str, container_id: str
    ) -> frozenset[ContainerExecutionPhase]: ...


@dataclass(frozen=True, slots=True)
class StreamContainerStartupReader:
    """The start steps a worker reported finishing, from the container's event stream."""

    events: RedisEventStreamRepository

    def finished_steps(
        self, *, workspace_id: str, stub_id: str, container_id: str
    ) -> frozenset[ContainerExecutionPhase]:
        records = self.events.read_event_history(
            EventHistoryQuery(
                workspace_id=workspace_id,
                stub_id=stub_id,
                container_id=container_id,
                event_types=(EventRecordType.ContainerLifecycle,),
            )
        )
        steps: set[ContainerExecutionPhase] = set()
        for record in records:
            data = record.body.get("data")
            if not isinstance(data, dict) or data.get("success") is not True:
                continue
            try:
                steps.add(ContainerExecutionPhase(str(data.get("id"))))
            except ValueError:
                continue
        return frozenset(steps)


def devbox_phase(
    container: LiveContainer | None,
    *,
    finished_steps: frozenset[ContainerExecutionPhase],
    saving_disk: bool,
    recent_failure: str | None,
) -> tuple[DevboxPhase, str]:
    """What the devbox is doing, and why when its last start failed.

    A live container answers first: a start in progress is what someone waiting
    wants to follow. The image and the root disk are prepared one after the
    other, so the first of the two steps not yet reported is the current one.
    """
    if container is not None:
        if container.status is ContainerStatus.Running:
            return DevboxPhase.Running, ""
        if not container.placed:
            return DevboxPhase.Queued, ""
        if ContainerExecutionPhase.LoadImage not in finished_steps:
            return DevboxPhase.PullingImage, ""
        if ContainerExecutionPhase.PrepareRootfs not in finished_steps:
            return DevboxPhase.RestoringDisk, ""
        return DevboxPhase.Starting, ""
    if saving_disk:
        return DevboxPhase.Stopping, ""
    if recent_failure is not None:
        return DevboxPhase.Failed, recent_failure
    return DevboxPhase.Stopped, ""


@dataclass(frozen=True, slots=True)
class DevboxService:
    database: DatabaseClient
    keep_alive: PodKeepAliveReader
    startup: ContainerStartupReader
    worker_absence: DiskWorkerAbsence
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
            containers = ContainerRepository(session)
            container = containers.newest_live_for_stub(stub.id) if deployment.active else None
            disks = DiskRepository(session)
            disk = disks.get(root.name, workspace_id=workspace_id) if root is not None else None
            saving_disk = False
            if disk is not None and container is None:
                # The lease names its last holder until the next acquire, so
                # the storage owner's rule says whether that holder still saves.
                lease = disks.lease(disk.id)
                if lease is not None and lease[0]:
                    saving_disk = holder_keeps_disk(disks.holder(lease[0]), self.worker_absence)
            recent_failure = (
                containers.recent_startup_failure(
                    stub.id,
                    since=self.clock()
                    - timedelta(seconds=stub.config.autoscaler.failed_container_window),
                )
                if container is None and not saving_disk
                else None
            )
        command = ["lazycloud", "ssh", deployment.name]
        if ambiguous:
            command += ["--app", resource.app.name]
        phase, reason = devbox_phase(
            container,
            finished_steps=(
                self.startup.finished_steps(
                    workspace_id=workspace_id, stub_id=stub.id, container_id=container.id
                )
                if container is not None
                and container.placed
                and container.status is ContainerStatus.Pending
                else frozenset()
            ),
            saving_disk=saving_disk,
            recent_failure=recent_failure,
        )
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
            phase=phase,
            phase_reason=reason,
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
        # A deadline already past says only that the next pass may stop it.
        candidates = [now + timedelta(seconds=state.idle_seconds)] if state.idle_seconds else []
        if container.started_at is not None:
            candidates.append(to_utc(container.started_at) + timedelta(seconds=keep_warm_seconds))
        upcoming = [deadline for deadline in candidates if deadline > now]
        return 0, max(upcoming) if upcoming else None


def _state(container: LiveContainer | None) -> DevboxState:
    if container is None:
        return DevboxState.Stopped
    if container.status is ContainerStatus.Running:
        return DevboxState.Running
    return DevboxState.Starting


__all__ = [
    "ContainerStartupReader",
    "DevboxService",
    "StreamContainerStartupReader",
    "devbox_phase",
]
