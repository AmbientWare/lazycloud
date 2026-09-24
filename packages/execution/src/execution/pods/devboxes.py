"""How to connect to a devbox, whether it is running, and starting or stopping it now."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Protocol

from control.deployment_resources import DeploymentResource
from control.service import StubRecord
from database.repositories.apps import DeploymentRepository, StubRepository
from database.repositories.disks import DiskRepository
from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import (
    AutoscalerStateRepository,
    ContainerRepository,
    LiveContainer,
)
from observability.stream_state import RedisEventStreamRepository
from shared.autoscaler_state import (
    SCALE_UP_FAILED_ACTION,
    AutoscalerStateRecord,
    AutoscalerTargetKind,
)
from shared.autoscaling import POD_WAKE_START_SECONDS
from shared.container_requests import StopContainerReason
from shared.containers import ContainerExecutionPhase, ContainerStatus
from shared.deployment_records import (
    DEFAULT_DEVBOX_KEEP_WARM_SECONDS,
    Deployment,
    resolve_pod_role,
)
from shared.deployments import DeploymentKind, DevboxPhase, DevboxState, PodRole
from shared.disks import DiskStatus
from shared.errors import InvalidInputError, NotFoundError
from shared.http.deployments import DevboxDiskResponse, DevboxResponse
from shared.realtime.contracts import EventRecordType
from shared.realtime.streams import EventHistoryQuery
from shared.ssh import ssh_host_alias
from shared.timestamps import to_utc, utc_now
from storage.disk_volumes import DiskWorkerAbsence
from storage.disks import disk_record

from database import DatabaseClient
from execution.containers.runtime_state import PodKeepAliveReader
from execution.pods.service import wake_pod


class DevboxDeployments(Protocol):
    """The deployment owner's switch and stop, which a devbox start and stop go through."""

    def set_deployment_active(
        self, workspace: str, deployment_id_or_name: str, *, active: bool
    ) -> Deployment: ...

    def stop_deployment_containers(
        self, workspace: str, deployment: Deployment, *, reason: StopContainerReason | None
    ) -> None: ...


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
    waking: bool,
    refusal: str | None,
    recent_failure: str | None,
) -> tuple[DevboxPhase, str]:
    """What the devbox is doing, and why when its last start failed.

    A live container answers first: a start in progress is what someone waiting
    wants to follow. The image and the root disk are prepared one after the
    other, so the first of the two steps not yet reported is the current one.
    A start with no container yet waits for the autoscaler, unless the
    autoscaler has said why it will not start one.
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
    if waking and refusal is not None:
        return DevboxPhase.Failed, refusal
    if waking:
        return DevboxPhase.Queued, ""
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
        if not _is_devbox(resource):
            return None
        return self._describe(resource)

    def start(
        self, resource: DeploymentResource, *, deployments: DevboxDeployments
    ) -> DevboxResponse:
        """Ask for the devbox to start and answer its status without waiting for it.

        A start wakes the pod as a connection does and records when it asked,
        which the autoscaler reads as a request for one container while nobody
        is connected. A deployment that is off is switched on as well, and that
        switch is what tells the autoscaler to look.
        """
        stub = _devbox_stub(resource)
        now = self.clock()
        with self.database.session() as session:
            if resource.deployment.active:
                wake_pod(session, stub_id=stub.id, workspace_id=stub.workspace_id, woken_at=now)
            else:
                StubRepository(session).wake(stub.id, workspace_id=stub.workspace_id, woken_at=now)
        if not resource.deployment.active:
            deployment = deployments.set_deployment_active(
                stub.workspace_id, resource.deployment.id, active=True
            )
            resource = replace(resource, deployment=deployment)
        return self._describe(resource)

    def stop(
        self, resource: DeploymentResource, *, deployments: DevboxDeployments
    ) -> DevboxResponse:
        """Park the devbox and stop its container; the deployment stays on.

        Parked, the autoscaler starts nothing for it, whatever its warm floor or
        open connections say, until a start or a new connection wakes it.
        """
        stub = _devbox_stub(resource)
        with self.database.session() as session:
            StubRepository(session).park(stub.id, workspace_id=stub.workspace_id)
        deployments.stop_deployment_containers(
            stub.workspace_id, resource.deployment, reason=StopContainerReason.User
        )
        return self._describe(resource)

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
            power = StubRepository(session).power(stub.id, workspace_id=workspace_id)
            containers = ContainerRepository(session)
            container = containers.newest_live_for_stub(stub.id) if deployment.active else None
            now = self.clock()
            woken_at = power.woken_at
            waking = (
                container is None
                and deployment.active
                and not power.parked
                and woken_at is not None
                and now < woken_at + timedelta(seconds=POD_WAKE_START_SECONDS)
            )
            refusal = (
                _start_refusal(
                    AutoscalerStateRepository(session).get(
                        workspace_id=workspace_id,
                        target_kind=AutoscalerTargetKind.Pod,
                        target_id=stub.id,
                    )
                )
                if waking
                else None
            )
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
                    since=now - timedelta(seconds=stub.config.autoscaler.failed_container_window),
                )
                if container is None and not saving_disk and not waking
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
            waking=waking,
            refusal=refusal,
            recent_failure=failure.reason if failure is not None else None,
        )
        connections, idle_deadline = self._keep_alive(
            container,
            workspace_id=workspace_id,
            stub_id=stub.id,
            keep_warm_seconds=stub.config.runtime.keep_warm,
            woken_at=woken_at,
            now=now,
        )
        return DevboxResponse(
            ssh_command=shlex.join(command),
            ssh_host=ssh_host_alias(workspace_name, resource.app.name, deployment.name),
            state=_state(container, starting=waking and refusal is None),
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
        woken_at: datetime | None,
        now: datetime,
    ) -> tuple[int, datetime | None]:
        if container is None or container.status is not ContainerStatus.Running:
            return 0, None
        state = self.keep_alive.keep_alive(
            workspace_id=workspace_id, stub_id=stub_id, container_id=container.id
        )
        if state.connections > 0 or keep_warm_seconds < 0:
            return state.connections, None
        # The autoscaler spares a container for its first keep-warm window, for
        # as long as the marker lives, and, when a start asked for it, for at
        # least the devbox idle default, so it stops at the latest of those.
        # A deadline already past says only that the next pass may stop it.
        candidates = [now + timedelta(seconds=state.idle_seconds)] if state.idle_seconds else []
        if container.started_at is not None:
            started_at = to_utc(container.started_at)
            window = keep_warm_seconds
            if woken_at is not None and started_at >= woken_at:
                window = max(window, DEFAULT_DEVBOX_KEEP_WARM_SECONDS)
            candidates.append(started_at + timedelta(seconds=window))
        upcoming = [deadline for deadline in candidates if deadline > now]
        return 0, max(upcoming) if upcoming else None


def _is_devbox(resource: DeploymentResource) -> bool:
    deployment = resource.deployment
    return resolve_pod_role(deployment.kind, deployment.spec.role) is PodRole.Devbox


def _devbox_stub(resource: DeploymentResource) -> StubRecord:
    if not _is_devbox(resource):
        raise InvalidInputError(f"deployment {resource.deployment.name} is not a devbox")
    return resource.stub


def _start_refusal(state: AutoscalerStateRecord | None) -> str | None:
    """Why the autoscaler last declined to start a container, if that is what it said."""
    if state is None:
        return None
    for action in state.last_actions:
        if action.action == SCALE_UP_FAILED_ACTION and action.reason:
            return action.reason
    if state.guardrails.get("limited") is True:
        reason = state.guardrails.get("reason")
        return reason if isinstance(reason, str) and reason else None
    return None


def _state(container: LiveContainer | None, *, starting: bool) -> DevboxState:
    if container is None:
        return DevboxState.Starting if starting else DevboxState.Stopped
    if container.status is ContainerStatus.Running:
        return DevboxState.Running
    return DevboxState.Starting


__all__ = [
    "ContainerStartupReader",
    "DevboxDeployments",
    "DevboxService",
    "StreamContainerStartupReader",
    "devbox_phase",
]
