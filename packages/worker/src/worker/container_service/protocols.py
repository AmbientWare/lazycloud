from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from shared.contracts import ContractModel
from shared.routing import AgentBackendRoute

from worker.container_client.models import ContainerArchiveResponse, ContainerExecResponse
from worker.container_service.models import (
    SandboxProcessEvent,
    WorkerContainerServiceInstance,
    WorkerSandboxProcess,
)
from worker.execution import PortBinding
from worker.sandbox_server import SandboxProcessLogEntry


class WorkerContainerInstanceStore(Protocol):
    def get_container_instance(
        self, container_id: str
    ) -> WorkerContainerServiceInstance | None: ...

    def save_container_instance(self, instance: WorkerContainerServiceInstance) -> None: ...

    def list_container_instances(self) -> list[WorkerContainerServiceInstance]: ...


class WorkerContainerRuntimeController(Protocol):
    def status(self, container_id: str) -> str: ...

    def exec_container(
        self,
        container_id: str,
        *,
        argv: list[str],
        env: list[str],
        cwd: str,
    ) -> ContainerExecResponse: ...

    def kill_container(self, container_id: str, *, signal: int, force_delete: bool) -> None: ...


class WorkerSandboxProcessManager(Protocol):
    def ready(self) -> bool: ...

    def stream_exec(
        self,
        argv: list[str],
        *,
        cwd: str,
        env: list[str],
    ) -> Iterable[SandboxProcessEvent]: ...

    def ack(self, pid: int, seq: int, *, ok: bool) -> None: ...

    def status(self, pid: int) -> int | None: ...

    def stdout(self, pid: int) -> str: ...

    def stderr(self, pid: int) -> str: ...

    def kill(self, pid: int) -> None: ...

    def list_processes(self) -> list[WorkerSandboxProcess]: ...

    def cleanup(self) -> None: ...


class WorkerSandboxProcessManagerFactory(Protocol):
    def create_process_manager(
        self,
        instance: WorkerContainerServiceInstance,
    ) -> WorkerSandboxProcessManager: ...

    def suspend_process_streams(self, instance: WorkerContainerServiceInstance) -> None: ...

    def resume_process_streams(self, instance: WorkerContainerServiceInstance) -> None: ...


class WorkerSandboxDockerLifecycle(Protocol):
    def prepare(self, container_id: str) -> None: ...

    def ensure_ready(self, instance: WorkerContainerServiceInstance) -> None: ...

    def stop(self, container_id: str) -> None: ...


class WorkerSandboxLogSink(Protocol):
    def append_sandbox_process_log(self, entry: SandboxProcessLogEntry) -> None: ...


class WorkerSandboxPortPublisher(Protocol):
    def allocate_port(
        self,
        instance: WorkerContainerServiceInstance,
        *,
        container_port: int,
    ) -> int: ...

    def local_target(
        self,
        instance: WorkerContainerServiceInstance,
        *,
        host_port: int,
        container_port: int,
    ) -> str: ...

    def publish_exposed_port(
        self,
        instance: WorkerContainerServiceInstance,
        *,
        port: int,
        local_target: str,
        binding: PortBinding | None = None,
        address_map: dict[int, str] | None = None,
        routes: list[AgentBackendRoute] | None = None,
    ) -> str: ...

    def unpublish_exposed_port(
        self,
        instance: WorkerContainerServiceInstance,
        *,
        port: int,
        local_target: str,
        address_map: dict[int, str],
    ) -> None: ...


class WorkerSandboxNetworkPolicyUpdater(Protocol):
    def update_network_permissions(
        self,
        container_id: str,
        *,
        block_network: bool,
        allow_list: list[str],
    ) -> ContractModel | None: ...


class WorkerContainerCheckpointCreator(Protocol):
    def create_checkpoint(
        self,
        instance: WorkerContainerServiceInstance,
        *,
        checkpoint_id: str = "",
    ) -> str: ...


class WorkerContainerArchiveCreator(Protocol):
    def archive_container(
        self,
        instance: WorkerContainerServiceInstance,
        *,
        image_id: str,
    ) -> Iterable[ContainerArchiveResponse]: ...


@dataclass(slots=True)
class LocalSandboxPortPublisher:
    host: str = "127.0.0.1"
    scheme: str = "http"

    def allocate_port(
        self,
        instance: WorkerContainerServiceInstance,
        *,
        container_port: int,
    ) -> int:
        _ = instance
        return container_port

    def local_target(
        self,
        instance: WorkerContainerServiceInstance,
        *,
        host_port: int,
        container_port: int,
    ) -> str:
        _ = instance, container_port
        return f"{self.host}:{host_port}"

    def publish_exposed_port(
        self,
        instance: WorkerContainerServiceInstance,
        *,
        port: int,
        local_target: str,
        binding: PortBinding | None = None,
        address_map: dict[int, str] | None = None,
        routes: list[AgentBackendRoute] | None = None,
    ) -> str:
        _ = instance, port, binding, address_map, routes
        return f"{self.scheme}://{local_target}"

    def unpublish_exposed_port(
        self,
        instance: WorkerContainerServiceInstance,
        *,
        port: int,
        local_target: str,
        address_map: dict[int, str],
    ) -> None:
        _ = instance, port, local_target, address_map
