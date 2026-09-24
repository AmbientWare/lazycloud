"""How to connect to a devbox, whether it is running, and starting or stopping it now."""

from __future__ import annotations

import asyncio
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Protocol

from control.deployment_resources import DeploymentResource
from control.service import StubRecord
from database.repositories.apps import DeploymentRepository
from database.repositories.disks import DiskRepository
from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import (
    AutoscalingTargetRepository,
    ContainerRepository,
    LiveContainer,
)
from observability.stream_state import RedisEventStreamRepository
from shared.autoscaler_state import AutoscalerTargetKind
from shared.container_requests import StopContainerReason
from shared.containers import ContainerExecutionPhase, ContainerRecord, ContainerStatus
from shared.deployment_records import Deployment, resolve_pod_role
from shared.deployments import DeploymentKind, DevboxPhase, DevboxState, PodRole
from shared.disks import DiskStatus
from shared.errors import InvalidInputError, NotFoundError, UpstreamTimeoutError
from shared.http.deployments import DevboxDiskResponse, DevboxResponse
from shared.realtime.contracts import EventRecordType
from shared.realtime.streams import EventHistoryQuery
from shared.ssh import ssh_host_alias
from shared.timestamps import to_utc, utc_now
from storage.disk_volumes import DiskWorkerAbsence
from storage.disks import disk_record

from database import DatabaseClient
from execution.containers.runtime_state import PodKeepAliveReader
from execution.pods.proxy import PodProxyConnectionRepository

DEVBOX_START_WAIT_SECONDS = 30.0
"""How long a start holds its demand for the autoscaler to ask for a container."""


class DeploymentSwitch(Protocol):
    def set_deployment_active(
        self, workspace: str, deployment_id_or_name: str, *, active: bool
    ) -> Deployment: ...


class DevboxContainers(Protocol):
    def stop(
        self, container_id: str, *, reason: StopContainerReason | None = None
    ) -> ContainerRecord: ...


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
    containers: DevboxContainers
    connections: PodProxyConnectionRepository | None
    clock: Callable[[], datetime] = field(default=utc_now)
    start_wait_seconds: float = DEVBOX_START_WAIT_SECONDS
    poll_interval_seconds: float = 0.5

    def describe(self, resource: DeploymentResource) -> DevboxResponse | None:
        """The devbox block for this deployment, or None when it is not a devbox."""
        if not _is_devbox(resource):
            return None
        return self._describe(resource)

    async def start(
        self, resource: DeploymentResource, *, deployments: DeploymentSwitch
    ) -> DevboxResponse:
        """Boot a stopped devbox now, the way a connection would, and answer its status.

        A deployment that is off is switched on first, since a connection
        refuses one. The wake is the demand an SSH connection records: one on
        the stub's connection total, held until the autoscaler has asked for a
        container. The autoscaler never stops a pending container and spares a
        running one for its first keep-warm window, so letting the demand go
        leaves the devbox up for the idle time a connection that just closed
        would have.
        """
        stub = _devbox_stub(resource)
        if not resource.deployment.active:
            deployment = await asyncio.to_thread(
                deployments.set_deployment_active,
                stub.workspace_id,
                resource.deployment.id,
                active=True,
            )
            resource = replace(resource, deployment=deployment)
        if await asyncio.to_thread(self._live_container_id, stub.id) is None:
            await self._wake(stub, resource.deployment.name)
        return await asyncio.to_thread(self._describe, resource)

    def stop(self, resource: DeploymentResource) -> DevboxResponse:
        """Stop the devbox's container now, as its idle time running out would.

        The deployment stays on, so the next connection or start boots it again,
        and the disk saves as it does after any stop.
        """
        stub = _devbox_stub(resource)
        with self.database.session() as session:
            container_ids = DeploymentRepository(session).live_container_ids(
                workspace_id=stub.workspace_id, deployment_id=resource.deployment.id
            )
        for container_id in container_ids:
            self.containers.stop(container_id, reason=StopContainerReason.Scheduler)
        return self._describe(resource)

    async def _wake(self, stub: StubRecord, name: str) -> None:
        if self.connections is None:
            raise RuntimeError("pod connection counters are not configured")
        connections = self.connections
        await connections.increment_total_connections(stub.workspace_id, stub.id)
        try:
            await asyncio.to_thread(self._activate_autoscaling, stub)
            deadline = time.monotonic() + self.start_wait_seconds
            while await asyncio.to_thread(self._live_container_id, stub.id) is None:
                if time.monotonic() >= deadline:
                    raise UpstreamTimeoutError(
                        f"devbox {name} was not given a container within "
                        f"{self.start_wait_seconds:g} seconds"
                    )
                await asyncio.sleep(self.poll_interval_seconds)
        finally:
            # Shielded: a caller that gives up cancels this, and demand left
            # behind would keep the devbox running with nobody connected.
            await asyncio.shield(
                connections.decrement_total_connections(stub.workspace_id, stub.id)
            )

    def _activate_autoscaling(self, stub: StubRecord) -> None:
        with self.database.session() as session:
            AutoscalingTargetRepository(session).activate(
                stub_id=stub.id,
                workspace_id=stub.workspace_id,
                target_kind=AutoscalerTargetKind.Pod,
            )

    def _live_container_id(self, stub_id: str) -> str | None:
        with self.database.session() as session:
            container = ContainerRepository(session).newest_live_for_stub(stub_id)
        return container.id if container is not None else None

    def _describe(self, resource: DeploymentResource) -> DevboxResponse:
        deployment = resource.deployment
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
            reading = (
                DiskRepository(session).get(root.name, workspace_id=workspace_id)
                if root is not None
                else None
            )
            disk = None if reading is None else disk_record(reading, self.worker_absence)
            saving_disk = (
                container is None and disk is not None and disk.status is DiskStatus.Saving
            )
            failure = (
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
            recent_failure=failure.reason if failure is not None else None,
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
            container_id=container.id if container is not None else None,
            failed_container_id=(
                failure.container_id
                if failure is not None and phase is DevboxPhase.Failed
                else None
            ),
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


def _is_devbox(resource: DeploymentResource) -> bool:
    deployment = resource.deployment
    return resolve_pod_role(deployment.kind, deployment.spec.role) is PodRole.Devbox


def _devbox_stub(resource: DeploymentResource) -> StubRecord:
    if not _is_devbox(resource):
        raise InvalidInputError(f"deployment {resource.deployment.name} is not a devbox")
    return resource.stub


def _state(container: LiveContainer | None) -> DevboxState:
    if container is None:
        return DevboxState.Stopped
    if container.status is ContainerStatus.Running:
        return DevboxState.Running
    return DevboxState.Starting


__all__ = [
    "DEVBOX_START_WAIT_SECONDS",
    "ContainerStartupReader",
    "DeploymentSwitch",
    "DevboxContainers",
    "DevboxService",
    "StreamContainerStartupReader",
    "devbox_phase",
]
