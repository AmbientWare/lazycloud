from __future__ import annotations

import logging
import secrets
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from control.service import ControlPlaneService, StubRecord
from database.repositories.orchestration import ContainerRepository
from database.types import DatabaseSession
from shared.app_identity import SHELL_IMAGE, SHELL_LOG_PATH
from shared.container_requests import WorkerStartupKind
from shared.containers import TERMINAL_CONTAINER_STATUSES, ContainerRecord, ContainerStatus
from shared.env import parse_environment
from shared.errors import NotFoundError, UpstreamUnavailableError
from shared.events import EventLevel
from shared.http.shells import (
    ExistingContainerShellSession,
    ShellConnectPlanResponse,
    StandaloneShellSession,
)
from shared.http.workspace_changes import WorkspaceChangeType
from shared.scheduling import (
    ContainerSchedulingDirectory,
    SchedulerContainerAddressMap,
    SchedulerContainerStatus,
)
from shared.shell_protocol import (
    SHELL_FRAME_HEADER_SIZE,
    SHELL_FRAME_MAX_PAYLOAD_BYTES,
    ShellAuthRequest,
    ShellFrameType,
    encode_shell_frame,
)
from shared.timestamps import utc_now

from database import AsyncDatabaseClient
from execution.config import ContainerResourceConfig
from execution.container_clients import (
    PodContainerControlClient,
    SchedulerContainerClientFactory,
)
from execution.containers.planning import ContainerSchedulingOptions
from execution.containers.service import PendingContainerReservation
from execution.mounts import source_code_mounts
from execution.services import ExecutionServices
from execution.shells.planning import (
    SHELL_SERVER_PROBE_TIMEOUT_SECONDS,
    SHELL_SERVER_READY_TIMEOUT_SECONDS,
    SHELL_WORKER_PORT,
    ShellExistingContainerStatus,
    ShellStandaloneRequest,
    existing_container_shell_credentials,
    plan_existing_shell_container,
    plan_shell_proxy,
    plan_shell_standalone,
    shell_server_exec_command,
    shell_server_probe_command,
)
from execution.shells.proxy import ShellBackendTarget


class ShellTargetNotFoundError(NotFoundError):
    pass


class ShellTargetUnavailableError(UpstreamUnavailableError):
    pass


class ShellTicketCompensationStatus(StrEnum):
    Cleaned = "cleaned"
    Failed = "failed"


LOGGER = logging.getLogger(__name__)


class AsyncShellContainerDirectory(Protocol):
    async def get_container_address_map(
        self,
        container_id: str,
    ) -> SchedulerContainerAddressMap: ...


@dataclass(frozen=True, slots=True)
class ShellTicketCompensationResult:
    container_id: str
    status: ShellTicketCompensationStatus
    terminal_status: ContainerStatus | None = None
    failure_recorded: bool = False
    reason: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status is ShellTicketCompensationStatus.Cleaned


class ShellControlService:
    def __init__(
        self,
        services: ExecutionServices,
        *,
        scheduler_containers: ContainerSchedulingDirectory | None = None,
        container_clients: SchedulerContainerClientFactory[PodContainerControlClient] | None = None,
        backend_connector: Callable[[ShellBackendTarget], socket.socket],
        async_database: AsyncDatabaseClient | None = None,
        async_scheduler_containers: AsyncShellContainerDirectory | None = None,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self.services = services
        self.control_plane = ControlPlaneService(services.context)
        candidate = getattr(services.containers, "scheduler_containers", None)
        self.scheduler_containers = scheduler_containers or (
            candidate if isinstance(candidate, ContainerSchedulingDirectory) else None
        )
        self.container_clients = container_clients
        self.backend_connector = backend_connector
        self.async_database = async_database
        self.async_scheduler_containers = async_scheduler_containers
        self.poll_interval_seconds = poll_interval_seconds

    def create_standalone_shell(
        self,
        *,
        workspace_id: str,
        stub_id: str,
    ) -> StandaloneShellSession:
        stub = self.control_plane.get_stub(stub_id, workspace=workspace_id)

        token_key = secrets.token_urlsafe(24)
        container_id = str(uuid4())
        request = self._standalone_request(stub, token_key=token_key, container_id=container_id)
        plan = plan_shell_standalone(request)
        env = parse_environment(plan.env) | {"SHELL_CONTAINER_ID": plan.container_id}
        with self.services.context.database.session() as session:
            record = self.services.containers.reserve_pending(
                session,
                PendingContainerReservation(
                    id=plan.container_id,
                    region=stub.config.runtime.region,
                    availability_zone=stub.config.runtime.availability_zone,
                    name=f"shell-{stub.name}",
                    image=request.image_id or SHELL_IMAGE,
                    command=list(plan.entrypoint),
                    workspace_id=stub.workspace_id,
                    stub_id=stub.id,
                    app_id=stub.app_id,
                    env=env,
                    ports={"shell": SHELL_WORKER_PORT},
                    gpu=list(plan.gpu),
                    gpu_count=plan.gpu_count,
                ),
            )
        self.services.containers.publish_lifecycle_change(
            record,
            WorkspaceChangeType.Created,
        )
        workspace = self.control_plane.get_workspace(stub.workspace_id)
        submitted = self.services.containers.submit_scheduler_request(
            record,
            ContainerSchedulingOptions(
                region=stub.config.runtime.region,
                availability_zone=stub.config.runtime.availability_zone,
                workspace_name=workspace.name,
                stub_type="shell",
                preemptible=stub.config.runtime.preemptible,
                startup_kind=WorkerStartupKind.Pod,
                entrypoint=list(plan.entrypoint),
                env=env,
                env_list=list(plan.env),
                image_id=record.image,
                app_id=stub.app_id or "",
                deployment_id=stub.deployment_id or "",
                ports=[SHELL_WORKER_PORT],
                requested_ports=[SHELL_WORKER_PORT],
                cpu_millicores=plan.cpu_millicores,
                memory_mib=plan.memory_mib,
                disk_mib=plan.disk_mib,
                gpu=list(record.gpu),
                gpu_count=record.gpu_count,
                mounts=source_code_mounts(
                    context=self.services.context,
                    object_storage=self.services.object_storage,
                    workspace_id=stub.workspace_id,
                    workspace_name=workspace.name,
                    object_id=stub.config.object_id,
                ),
            ),
        )
        if not submitted.accepted:
            reason = submitted.reason or "failed to schedule shell container"
            self._mark_container_failed(record, reason)
            raise UpstreamUnavailableError(reason)
        if not self._wait_for_running(record, plan.wait_timeout_seconds):
            msg = "shell container did not become running before timeout"
            raise UpstreamUnavailableError(msg)
        self.services.events.emit(
            "shell.created",
            level=EventLevel.Info,
            resource_type="container",
            resource_id=record.id,
            message=f"scheduled shell container for {stub.name}",
            data={
                "stub_id": stub.id,
                "workspace_id": stub.workspace_id,
                "worker_port": plan.worker_port,
                "idle_timeout_seconds": plan.idle_timeout_seconds,
            },
            workspace_id=stub.workspace_id,
        )
        return StandaloneShellSession(
            container_id=record.id,
            username=plan.credentials.username,
            password=plan.credentials.password,
        )

    def create_shell_in_existing_container(
        self,
        *,
        workspace_id: str,
        container_id: str,
    ) -> ExistingContainerShellSession:
        container = self._container(container_id)
        workspace = self.control_plane.get_workspace(workspace_id)
        container_workspace_id = container.workspace_id if container is not None else ""
        container_stub_id = container.stub_id if container is not None and container.stub_id else ""
        credentials = existing_container_shell_credentials(
            workspace_signing_key=workspace.signing_key,
            container_id=container_id,
            token_external_id=container_stub_id or "shell",
        )
        plan = plan_existing_shell_container(
            container_id=container_id,
            container_status=container.status if container is not None else None,
            container_workspace_id=container_workspace_id,
            request_workspace_id=workspace.id,
            stub_id=container_stub_id,
            token_external_id=container_stub_id or "shell",
            token_key=credentials.password,
        )
        if plan.status is ShellExistingContainerStatus.ContainerNotRunning:
            raise UpstreamUnavailableError(plan.error_message or "Container is not running")
        if not plan.ok or plan.credentials is None or container is None:
            raise NotFoundError(plan.error_message or "Container not found")
        client: PodContainerControlClient | None = None
        try:
            client = self._container_client_factory().client_for(container).client
            command = shell_server_exec_command(
                plan.expose_port,
                log_path=SHELL_LOG_PATH,
                idle_timeout_seconds=plan.idle_timeout_seconds,
            )
            credentials_env = (
                f"USERNAME={plan.credentials.username}",
                f"PASSWORD={plan.credentials.password}",
            )
            executed = client.exec(
                container.id,
                command,
                env=credentials_env,
            )
            if not executed.ok:
                raise UpstreamUnavailableError(
                    executed.error_msg or executed.stderr or "failed to start shell"
                )
            probed = client.exec(
                container.id,
                shell_server_probe_command(
                    plan.expose_port,
                    timeout_seconds=SHELL_SERVER_PROBE_TIMEOUT_SECONDS,
                ),
                env=credentials_env,
            )
            if not probed.ok:
                raise UpstreamUnavailableError(
                    probed.error_msg or probed.stderr or "shell listener did not become ready"
                )
            exposed = client.sandbox_expose_port(container.id, plan.expose_port)
            if not exposed.ok:
                raise UpstreamUnavailableError(exposed.error_msg or "failed to expose shell port")
            target = self.shell_backend_target(
                stub_id=plan.stub_id,
                container_id=container.id,
                workspace_id=workspace.id,
            )
            self._wait_for_shell_backend(
                target,
                username=plan.credentials.username,
                password=plan.credentials.password,
            )
        except UpstreamUnavailableError as exc:
            if client is not None:
                rollback_error = _rollback_shell_port(client, container.id, plan.expose_port)
                if rollback_error:
                    raise UpstreamUnavailableError(
                        f"{exc}; shell port rollback failed: {rollback_error}"
                    ) from exc
            raise
        except Exception as exc:
            if client is not None:
                rollback_error = _rollback_shell_port(client, container.id, plan.expose_port)
                if rollback_error:
                    raise UpstreamUnavailableError(
                        f"{exc}; shell port rollback failed: {rollback_error}"
                    ) from exc
            raise UpstreamUnavailableError(str(exc)) from exc
        return ExistingContainerShellSession(
            username=plan.credentials.username,
            password=plan.credentials.password,
            stub_id=plan.stub_id,
        )

    def compensate_standalone_ticket_failure(
        self,
        *,
        workspace_id: str,
        container_id: str,
    ) -> ShellTicketCompensationResult:
        """Stop a standalone shell whose connection ticket could not be issued."""

        try:
            container = self._container(container_id)
        except Exception as exc:
            LOGGER.exception("shell ticket compensation could not read container %s", container_id)
            return ShellTicketCompensationResult(
                container_id=container_id,
                status=ShellTicketCompensationStatus.Failed,
                reason=f"{type(exc).__name__}: {exc}",
            )
        if container is None or container.workspace_id != workspace_id:
            return ShellTicketCompensationResult(
                container_id=container_id,
                status=ShellTicketCompensationStatus.Failed,
            )
        if container.status in TERMINAL_CONTAINER_STATUSES:
            self._record_ticket_compensation(
                container,
                action="shell.ticket.compensated",
                level=EventLevel.Info,
                message="standalone shell was already terminal after ticket issuance failed",
            )
            return ShellTicketCompensationResult(
                container_id=container.id,
                status=ShellTicketCompensationStatus.Cleaned,
                terminal_status=container.status,
            )
        try:
            stopped = self.services.containers.stop(container.id)
        except Exception:
            LOGGER.warning("shell container %s did not stop", container.id, exc_info=True)
            refreshed = self._container_after_stop_failure(container.id, workspace_id)
            if refreshed is not None and refreshed.status in TERMINAL_CONTAINER_STATUSES:
                return ShellTicketCompensationResult(
                    container_id=refreshed.id,
                    status=ShellTicketCompensationStatus.Cleaned,
                    terminal_status=refreshed.status,
                )
            failed = refreshed or container
            recorded = self._record_ticket_compensation(
                failed,
                action="shell.ticket.compensation_failed",
                level=EventLevel.Error,
                message="standalone shell stop failed after ticket issuance failed",
            )
            return ShellTicketCompensationResult(
                container_id=failed.id,
                status=ShellTicketCompensationStatus.Failed,
                failure_recorded=recorded,
            )
        if stopped.status not in TERMINAL_CONTAINER_STATUSES:
            recorded = self._record_ticket_compensation(
                stopped,
                action="shell.ticket.compensation_failed",
                level=EventLevel.Error,
                message="standalone shell remained active after ticket issuance failed",
            )
            return ShellTicketCompensationResult(
                container_id=stopped.id,
                status=ShellTicketCompensationStatus.Failed,
                failure_recorded=recorded,
            )
        self._record_ticket_compensation(
            stopped,
            action="shell.ticket.compensated",
            level=EventLevel.Info,
            message="stopped standalone shell after ticket issuance failed",
        )
        return ShellTicketCompensationResult(
            container_id=stopped.id,
            status=ShellTicketCompensationStatus.Cleaned,
            terminal_status=stopped.status,
        )

    def compensate_existing_container_ticket_failure(
        self,
        *,
        workspace_id: str,
        container_id: str,
    ) -> ShellTicketCompensationResult:
        """Unpublish an existing-container shell listener after ticket failure."""

        try:
            container = self._container(container_id)
        except Exception as exc:
            LOGGER.exception("shell ticket compensation could not read container %s", container_id)
            return ShellTicketCompensationResult(
                container_id=container_id,
                status=ShellTicketCompensationStatus.Failed,
                reason=f"{type(exc).__name__}: {exc}",
            )
        if container is None or container.workspace_id != workspace_id:
            return ShellTicketCompensationResult(
                container_id=container_id,
                status=ShellTicketCompensationStatus.Failed,
            )
        try:
            client = self._container_client_factory().client_for(container).client
            response = client.sandbox_unexpose_port(container.id, SHELL_WORKER_PORT)
        except Exception:
            LOGGER.exception("shell listener unpublish failed for container %s", container.id)
            recorded = self._record_ticket_compensation(
                container,
                action="shell.ticket.compensation_failed",
                level=EventLevel.Error,
                message="could not unpublish shell listener after ticket issuance failed",
            )
            return ShellTicketCompensationResult(
                container_id=container.id,
                status=ShellTicketCompensationStatus.Failed,
                failure_recorded=recorded,
            )
        if not response.ok:
            recorded = self._record_ticket_compensation(
                container,
                action="shell.ticket.compensation_failed",
                level=EventLevel.Error,
                message="worker rejected shell listener cleanup after ticket issuance failed",
            )
            return ShellTicketCompensationResult(
                container_id=container.id,
                status=ShellTicketCompensationStatus.Failed,
                failure_recorded=recorded,
            )
        self._record_ticket_compensation(
            container,
            action="shell.ticket.compensated",
            level=EventLevel.Info,
            message="unpublished shell listener after ticket issuance failed",
        )
        return ShellTicketCompensationResult(
            container_id=container.id,
            status=ShellTicketCompensationStatus.Cleaned,
        )

    def _wait_for_shell_backend(
        self,
        target: ShellBackendTarget,
        *,
        username: str,
        password: str,
    ) -> None:
        deadline = time.monotonic() + SHELL_SERVER_READY_TIMEOUT_SECONDS
        error = "shell backend did not become ready"
        probe_target = replace(target, dial_timeout_seconds=1)
        auth = ShellAuthRequest(username=username, password=password, probe=True)
        payload = auth.model_dump_json().encode()
        while time.monotonic() < deadline:
            connection: socket.socket | None = None
            try:
                connection = self.backend_connector(probe_target)
                connection.settimeout(1.0)
                connection.sendall(encode_shell_frame(ShellFrameType.Auth.value, payload))
                frame_type, frame_payload = _read_shell_frame(connection)
                if frame_type == ShellFrameType.Ready.value:
                    return
                error = frame_payload.decode(errors="replace") or "shell backend rejected readiness"
            except OSError as exc:
                error = str(exc)
            finally:
                if connection is not None:
                    connection.close()
            time.sleep(0.05)
        raise UpstreamUnavailableError(error)

    def connect_plan(
        self,
        *,
        stub_id: str,
        container_id: str,
        workspace_id: str | None = None,
    ) -> ShellConnectPlanResponse:
        self._shell_container(
            stub_id=stub_id,
            container_id=container_id,
            workspace_id=workspace_id,
        )
        return ShellConnectPlanResponse.from_plan(plan_shell_proxy(stub_id, container_id))

    def shell_backend_target(
        self,
        *,
        stub_id: str,
        container_id: str,
        workspace_id: str | None = None,
    ) -> ShellBackendTarget:
        container = self._shell_container(
            stub_id=stub_id,
            container_id=container_id,
            workspace_id=workspace_id,
        )
        address_map = self._container_client_factory().address_map_for(container_id)
        return self._shell_backend_target(container, address_map, stub_id=stub_id)

    async def shell_backend_target_async(
        self,
        *,
        stub_id: str,
        container_id: str,
        workspace_id: str | None = None,
    ) -> ShellBackendTarget:
        if self.async_database is None or self.async_scheduler_containers is None:
            raise RuntimeError("asynchronous shell routing is not configured")
        container = await self.async_database.run_transaction(
            lambda session: self._shell_container_in_session(
                session,
                stub_id=stub_id,
                container_id=container_id,
                workspace_id=workspace_id,
            )
        )
        address_map = await self.async_scheduler_containers.get_container_address_map(container_id)
        return self._shell_backend_target(container, address_map, stub_id=stub_id)

    @staticmethod
    def _shell_backend_target(
        container: ContainerRecord,
        address_map: SchedulerContainerAddressMap,
        *,
        stub_id: str,
    ) -> ShellBackendTarget:
        plan = plan_shell_proxy(stub_id, container.id)
        address = address_map.address_map.get(SHELL_WORKER_PORT, "")
        if not address:
            msg = "shell port is not published for container"
            raise ShellTargetUnavailableError(msg)
        route = next(
            (item for item in address_map.routes if item.port == SHELL_WORKER_PORT),
            None,
        )
        return ShellBackendTarget(
            container_id=container.id,
            stub_id=stub_id,
            address=address,
            route=route,
            worker_port=SHELL_WORKER_PORT,
            buffer_size_bytes=plan.buffer_size_bytes,
            dial_timeout_seconds=plan.dial_timeout_seconds,
        )

    def _shell_container_in_session(
        self,
        session: DatabaseSession,
        *,
        stub_id: str,
        container_id: str,
        workspace_id: str | None,
    ) -> ContainerRecord:
        try:
            stub = self.control_plane.get_stub_in_session(
                session,
                stub_id,
                workspace=workspace_id,
            )
        except NotFoundError as exc:
            raise ShellTargetNotFoundError("Container not found") from exc
        if stub.id != stub_id:
            raise ShellTargetNotFoundError("Container not found")
        repository = ContainerRepository(session)
        container = (
            repository.get(container_id, workspace_id=stub.workspace_id)
            if workspace_id is not None
            else repository.get_across_workspaces(container_id)
        )
        return self._validated_shell_container(stub, container)

    def _container_client_factory(
        self,
    ) -> SchedulerContainerClientFactory[PodContainerControlClient]:
        if self.container_clients is None:
            msg = "container client factory is not configured"
            raise RuntimeError(msg)
        return self.container_clients

    def _standalone_request(
        self,
        stub: StubRecord,
        *,
        token_key: str,
        container_id: str,
    ) -> ShellStandaloneRequest:
        runtime_config = stub.config.runtime
        resources = ContainerResourceConfig.model_validate(runtime_config.model_dump())
        return ShellStandaloneRequest(
            stub_id=stub.id,
            handler=stub.handler or stub.config.handler or "",
            gateway_token=token_key,
            token_external_id=stub.id,
            token_key=token_key,
            container_id=container_id,
            container_id_suffix=secrets.token_hex(4),
            cpu_millicores=runtime_config.cpu_millicores,
            memory_mib=runtime_config.memory_mib,
            disk_mib=resources.requested_disk_mib,
            gpu_count=runtime_config.gpu_count,
            requires_gpu=runtime_config.requires_gpu,
            gpu=tuple(runtime_config.gpu),
            image_id=runtime_config.image_id or "",
            app_id=stub.app_id or "",
            workspace_id=stub.workspace_id,
        )

    def _wait_for_running(self, record: ContainerRecord, timeout_seconds: int) -> bool:
        if self.scheduler_containers is None:
            return True
        deadline = time.monotonic() + max(timeout_seconds, 0)
        while True:
            state = self.scheduler_containers.get_container_state(record.id)
            if state is not None:
                if state.status is SchedulerContainerStatus.Running:
                    record.status = ContainerStatus.Running
                    record.started_at = state.started_at or utc_now()
                    with self.services.context.database.session() as session:
                        ContainerRepository(session).records.upsert(
                            record,
                            key=record.id,
                            workspace_id=record.workspace_id,
                            name=record.name,
                            status=record.status.value,
                        )
                    self.services.containers.publish_lifecycle_change(
                        record,
                        WorkspaceChangeType.Updated,
                    )
                    return True
                if state.status in {
                    SchedulerContainerStatus.Complete,
                    SchedulerContainerStatus.Failed,
                    SchedulerContainerStatus.Stopping,
                }:
                    return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(max(self.poll_interval_seconds, 0.0))

    def _mark_container_failed(self, record: ContainerRecord, reason: str) -> None:
        record.status = ContainerStatus.Failed
        record.exit_code = 1
        record.finished_at = utc_now()
        with self.services.context.database.session() as session:
            ContainerRepository(session).records.upsert(
                record,
                key=record.id,
                workspace_id=record.workspace_id,
                name=record.name,
                status=record.status.value,
            )
        self.services.containers.publish_lifecycle_change(
            record,
            WorkspaceChangeType.Updated,
        )
        self.services.events.emit(
            "shell.schedule.failed",
            level=EventLevel.Error,
            resource_type="container",
            resource_id=record.id,
            message=reason or f"failed to schedule shell container {record.name}",
            workspace_id=record.workspace_id,
        )

    def _container_after_stop_failure(
        self,
        container_id: str,
        workspace_id: str,
    ) -> ContainerRecord | None:
        try:
            container = self._container(container_id)
        except Exception:
            LOGGER.exception("shell compensation could not read container %s", container_id)
            return None
        if container is None or container.workspace_id != workspace_id:
            return None
        return container

    def _record_ticket_compensation(
        self,
        container: ContainerRecord,
        *,
        action: str,
        level: EventLevel,
        message: str,
    ) -> bool:
        try:
            self.services.events.emit(
                action,
                level=level,
                resource_type="container",
                resource_id=container.id,
                message=message,
                data={"ticket_issued": False},
                workspace_id=container.workspace_id,
            )
        except Exception:
            LOGGER.exception("recording shell ticket compensation failed")
            return False
        return True

    def _container(self, container_id: str) -> ContainerRecord | None:
        try:
            return self.services.containers.get(container_id)
        except NotFoundError:
            return None

    def _shell_container(
        self,
        *,
        stub_id: str,
        container_id: str,
        workspace_id: str | None,
    ) -> ContainerRecord:
        with self.services.context.database.session() as session:
            return self._shell_container_in_session(
                session,
                stub_id=stub_id,
                container_id=container_id,
                workspace_id=workspace_id,
            )

    @staticmethod
    def _validated_shell_container(
        stub: StubRecord,
        container: ContainerRecord | None,
    ) -> ContainerRecord:
        if container is None:
            raise ShellTargetNotFoundError("Container not found")
        if container.workspace_id != stub.workspace_id:
            raise ShellTargetNotFoundError("Container not found")
        if container.stub_id not in {None, stub.id}:
            raise ShellTargetNotFoundError("Container not found")
        if container.status is not ContainerStatus.Running:
            raise ShellTargetUnavailableError("Container is not running")
        return container


def _rollback_shell_port(
    client: PodContainerControlClient,
    container_id: str,
    port: int,
) -> str:
    try:
        response = client.sandbox_unexpose_port(container_id, port)
    except Exception as exc:
        return str(exc)
    if response.ok:
        return ""
    return response.error_msg or "worker rejected shell port rollback"


def _read_shell_frame(connection: socket.socket) -> tuple[bytes, bytes]:
    header = _read_exact(connection, SHELL_FRAME_HEADER_SIZE)
    payload_size = int.from_bytes(header[1:], "big")
    if payload_size > SHELL_FRAME_MAX_PAYLOAD_BYTES:
        raise OSError("shell frame payload exceeds maximum size")
    return header[:1], _read_exact(connection, payload_size)


def _read_exact(connection: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise OSError("shell backend closed before readiness")
        data.extend(chunk)
    return bytes(data)


__all__ = [
    "ShellControlService",
    "ShellTargetNotFoundError",
    "ShellTargetUnavailableError",
    "ShellTicketCompensationResult",
    "ShellTicketCompensationStatus",
]
