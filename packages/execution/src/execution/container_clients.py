from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol, TypeVar

from foundation.io_utils import OutputMessage
from shared.containers import ContainerRecord
from shared.scheduling import (
    SchedulerContainerAddress,
    SchedulerContainerAddressMap,
    SchedulerContainerState,
)


class ContainerOperationResponse(Protocol):
    @property
    def ok(self) -> bool: ...

    @property
    def error_msg(self) -> str: ...


class ContainerSandboxFileInfo(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def mode(self) -> int: ...
    @property
    def size(self) -> int: ...
    @property
    def mod_time(self) -> int: ...
    @property
    def owner(self) -> str: ...
    @property
    def group(self) -> str: ...
    @property
    def is_dir(self) -> bool: ...
    @property
    def permissions(self) -> int: ...


class ContainerFileSearchMatch(Protocol):
    @property
    def path(self) -> str: ...
    @property
    def text(self) -> str: ...
    @property
    def line(self) -> int: ...
    @property
    def column(self) -> int: ...


class ContainerSandboxProcessInfo(Protocol):
    @property
    def pid(self) -> int: ...
    @property
    def command(self) -> str: ...


class ContainerExecResponse(ContainerOperationResponse, Protocol):
    @property
    def pid(self) -> int: ...
    @property
    def stderr(self) -> str: ...


class ContainerSandboxExecResponse(ContainerOperationResponse, Protocol):
    @property
    def pid(self) -> int: ...


class ContainerStatusResponse(ContainerOperationResponse, Protocol):
    @property
    def status(self) -> str: ...
    @property
    def exit_code(self) -> int: ...


class ContainerStdoutResponse(ContainerOperationResponse, Protocol):
    @property
    def stdout(self) -> str: ...


class ContainerStderrResponse(ContainerOperationResponse, Protocol):
    @property
    def stderr(self) -> str: ...


class ContainerDownloadResponse(ContainerOperationResponse, Protocol):
    @property
    def data(self) -> bytes: ...


class ContainerStatResponse(ContainerOperationResponse, Protocol):
    @property
    def file_info(self) -> ContainerSandboxFileInfo: ...


class ContainerFilesResponse(ContainerOperationResponse, Protocol):
    @property
    def files(self) -> Sequence[ContainerSandboxFileInfo]: ...


class ContainerSearchResponse(ContainerOperationResponse, Protocol):
    @property
    def results(self) -> Sequence[ContainerFileSearchMatch]: ...


class ContainerExposeResponse(ContainerOperationResponse, Protocol):
    @property
    def url(self) -> str: ...


class ContainerCheckpointResponse(ContainerOperationResponse, Protocol):
    @property
    def checkpoint_id(self) -> str: ...


class ContainerProcessesResponse(ContainerOperationResponse, Protocol):
    @property
    def processes(self) -> Sequence[ContainerSandboxProcessInfo]: ...


class ContainerPortsResponse(ContainerOperationResponse, Protocol):
    @property
    def ports(self) -> Sequence[int]: ...


class PodContainerControlClient(Protocol):
    def exec(
        self,
        container_id: str,
        command: str,
        env: Sequence[str] = (),
    ) -> ContainerExecResponse: ...

    def sandbox_exec(
        self,
        container_id: str,
        command: str,
        *,
        env: dict[str, str] | None = None,
        cwd: str = ".",
        timeout_seconds: float = ...,
    ) -> ContainerSandboxExecResponse: ...
    def sandbox_status(
        self, container_id: str, pid: int, *, timeout_seconds: float = ...
    ) -> ContainerStatusResponse: ...
    def sandbox_stdout(self, container_id: str, pid: int) -> ContainerStdoutResponse: ...
    def sandbox_stderr(self, container_id: str, pid: int) -> ContainerStderrResponse: ...
    def sandbox_kill(self, container_id: str, pid: int) -> ContainerOperationResponse: ...
    def sandbox_upload_file(
        self, container_id: str, container_path: str, data: bytes, *, mode: int = 0o644
    ) -> ContainerOperationResponse: ...
    def sandbox_download_file(
        self, container_id: str, container_path: str
    ) -> ContainerDownloadResponse: ...
    def sandbox_stat_file(
        self, container_id: str, container_path: str
    ) -> ContainerStatResponse: ...
    def sandbox_list_files(
        self, container_id: str, container_path: str = "."
    ) -> ContainerFilesResponse: ...
    def sandbox_delete_file(
        self, container_id: str, container_path: str
    ) -> ContainerOperationResponse: ...
    def sandbox_create_directory(
        self, container_id: str, container_path: str, *, mode: int = 0o755
    ) -> ContainerOperationResponse: ...
    def sandbox_delete_directory(
        self, container_id: str, container_path: str
    ) -> ContainerOperationResponse: ...
    def sandbox_replace_in_files(
        self, container_id: str, container_path: str, pattern: str, new_string: str
    ) -> ContainerOperationResponse: ...
    def sandbox_find_in_files(
        self, container_id: str, container_path: str, pattern: str
    ) -> ContainerSearchResponse: ...
    def sandbox_expose_port(self, container_id: str, port: int) -> ContainerExposeResponse: ...
    def sandbox_unexpose_port(self, container_id: str, port: int) -> ContainerOperationResponse: ...
    def sandbox_update_network_permissions(
        self, container_id: str, *, block_network: bool, allow_list: Sequence[str] = ()
    ) -> ContainerOperationResponse: ...
    def archive(
        self, container_id: str, image_id: str, output: Callable[[OutputMessage], None]
    ) -> None: ...
    def status(self, container_id: str) -> ContainerStatusResponse: ...
    def kill(self, container_id: str) -> ContainerOperationResponse: ...
    def checkpoint(
        self,
        container_id: str,
        *,
        checkpoint_id: str = "",
    ) -> ContainerCheckpointResponse: ...
    def sandbox_list_processes(self, container_id: str) -> ContainerProcessesResponse: ...
    def sandbox_list_exposed_ports(self, container_id: str) -> ContainerPortsResponse: ...


ClientT_co = TypeVar("ClientT_co", covariant=True)


class ContainerClientHandle(Protocol[ClientT_co]):
    @property
    def client(self) -> ClientT_co: ...

    @property
    def state(self) -> SchedulerContainerState: ...

    @property
    def worker_address(self) -> SchedulerContainerAddress: ...


class SchedulerContainerClientFactory(Protocol[ClientT_co]):
    def client_for(self, container: ContainerRecord) -> ContainerClientHandle[ClientT_co]: ...

    def state_for(self, container: ContainerRecord) -> SchedulerContainerState | None: ...

    def address_map_for(self, container_id: str) -> SchedulerContainerAddressMap: ...
