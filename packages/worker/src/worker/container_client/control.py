from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol, TypeVar

from foundation.io_utils import OutputMessage
from shared.contracts import ContractModel

from .models import (
    CONTAINER_CLIENT_LOG_KEEPALIVE_SECONDS,
    CONTAINER_CLIENT_MAX_MESSAGE_SIZE_BYTES,
    CONTAINER_CLIENT_SANDBOX_EXEC_TIMEOUT_SECONDS,
    CONTAINER_CLIENT_SANDBOX_STATUS_TIMEOUT_SECONDS,
    ContainerArchiveRequest,
    ContainerArchiveResponse,
    ContainerCheckpointRequest,
    ContainerCheckpointResponse,
    ContainerClientConnectionOptions,
    ContainerClientInterceptor,
    ContainerExecRequest,
    ContainerExecResponse,
    ContainerKillRequest,
    ContainerKillResponse,
    ContainerLogEntry,
    ContainerSandboxCreateDirectoryRequest,
    ContainerSandboxCreateDirectoryResponse,
    ContainerSandboxDeleteDirectoryRequest,
    ContainerSandboxDeleteDirectoryResponse,
    ContainerSandboxDeleteFileRequest,
    ContainerSandboxDeleteFileResponse,
    ContainerSandboxDownloadFileRequest,
    ContainerSandboxDownloadFileResponse,
    ContainerSandboxExecRequest,
    ContainerSandboxExecResponse,
    ContainerSandboxExposePortRequest,
    ContainerSandboxExposePortResponse,
    ContainerSandboxFindInFilesRequest,
    ContainerSandboxFindInFilesResponse,
    ContainerSandboxKillRequest,
    ContainerSandboxKillResponse,
    ContainerSandboxListExposedPortsRequest,
    ContainerSandboxListExposedPortsResponse,
    ContainerSandboxListFilesRequest,
    ContainerSandboxListFilesResponse,
    ContainerSandboxListProcessesRequest,
    ContainerSandboxListProcessesResponse,
    ContainerSandboxReplaceInFilesRequest,
    ContainerSandboxReplaceInFilesResponse,
    ContainerSandboxStatFileRequest,
    ContainerSandboxStatFileResponse,
    ContainerSandboxStatusRequest,
    ContainerSandboxStatusResponse,
    ContainerSandboxStderrRequest,
    ContainerSandboxStderrResponse,
    ContainerSandboxStdoutRequest,
    ContainerSandboxStdoutResponse,
    ContainerSandboxUnexposePortRequest,
    ContainerSandboxUnexposePortResponse,
    ContainerSandboxUpdateNetworkPermissionsRequest,
    ContainerSandboxUpdateNetworkPermissionsResponse,
    ContainerSandboxUploadFileRequest,
    ContainerSandboxUploadFileResponse,
    ContainerServiceMethod,
    ContainerServicePayload,
    ContainerStatusRequest,
    ContainerStatusResponse,
    ContainerStreamLogsRequest,
    ContainerTransportSecurity,
    SyncContainerWorkspaceRequest,
    SyncContainerWorkspaceResponse,
)

ResponseT = TypeVar("ResponseT", bound=ContractModel)
OutputCallback = Callable[[OutputMessage], None]


class ContainerServiceTransport(Protocol):
    def unary(
        self,
        method: ContainerServiceMethod,
        request: ContractModel,
        *,
        timeout_seconds: float | None = None,
    ) -> ContainerServicePayload: ...

    def stream(
        self,
        method: ContainerServiceMethod,
        request: ContractModel,
        *,
        timeout_seconds: float | None = None,
    ) -> Iterable[ContainerServicePayload]: ...


class ContainerClientStreamError(RuntimeError):
    pass


class ContainerArchiveError(RuntimeError):
    pass


@dataclass(slots=True)
class ContainerServiceClient:
    transport: ContainerServiceTransport

    def status(self, container_id: str) -> ContainerStatusResponse:
        return self._unary(
            ContainerServiceMethod.ContainerStatus,
            ContainerStatusRequest(container_id=container_id),
            ContainerStatusResponse,
        )

    def exec(
        self,
        container_id: str,
        command: str,
        env: Sequence[str] = (),
    ) -> ContainerExecResponse:
        return self._unary(
            ContainerServiceMethod.ContainerExec,
            ContainerExecRequest(container_id=container_id, command=command, env=tuple(env)),
            ContainerExecResponse,
        )

    def sandbox_exec(
        self,
        container_id: str,
        command: str,
        *,
        env: dict[str, str] | None = None,
        cwd: str = ".",
        timeout_seconds: float = CONTAINER_CLIENT_SANDBOX_EXEC_TIMEOUT_SECONDS,
    ) -> ContainerSandboxExecResponse:
        return self._unary(
            ContainerServiceMethod.ContainerSandboxExec,
            ContainerSandboxExecRequest(
                container_id=container_id,
                command=command,
                env=env or {},
                cwd=cwd,
            ),
            ContainerSandboxExecResponse,
            timeout_seconds=timeout_seconds,
        )

    def sandbox_status(
        self,
        container_id: str,
        pid: int,
        *,
        timeout_seconds: float = CONTAINER_CLIENT_SANDBOX_STATUS_TIMEOUT_SECONDS,
    ) -> ContainerSandboxStatusResponse:
        return self._unary(
            ContainerServiceMethod.ContainerSandboxStatus,
            ContainerSandboxStatusRequest(container_id=container_id, pid=pid),
            ContainerSandboxStatusResponse,
            timeout_seconds=timeout_seconds,
        )

    def sandbox_list_exposed_ports(
        self,
        container_id: str,
    ) -> ContainerSandboxListExposedPortsResponse:
        return self._unary(
            ContainerServiceMethod.ContainerSandboxListExposedPorts,
            ContainerSandboxListExposedPortsRequest(container_id=container_id),
            ContainerSandboxListExposedPortsResponse,
        )

    def sandbox_list_processes(self, container_id: str) -> ContainerSandboxListProcessesResponse:
        return self._unary(
            ContainerServiceMethod.ContainerSandboxListProcesses,
            ContainerSandboxListProcessesRequest(container_id=container_id),
            ContainerSandboxListProcessesResponse,
        )

    def sandbox_stdout(self, container_id: str, pid: int) -> ContainerSandboxStdoutResponse:
        return self._unary(
            ContainerServiceMethod.ContainerSandboxStdout,
            ContainerSandboxStdoutRequest(container_id=container_id, pid=pid),
            ContainerSandboxStdoutResponse,
        )

    def sandbox_stderr(self, container_id: str, pid: int) -> ContainerSandboxStderrResponse:
        return self._unary(
            ContainerServiceMethod.ContainerSandboxStderr,
            ContainerSandboxStderrRequest(container_id=container_id, pid=pid),
            ContainerSandboxStderrResponse,
        )

    def sandbox_kill(self, container_id: str, pid: int) -> ContainerSandboxKillResponse:
        return self._unary(
            ContainerServiceMethod.ContainerSandboxKill,
            ContainerSandboxKillRequest(container_id=container_id, pid=pid),
            ContainerSandboxKillResponse,
        )

    def sandbox_upload_file(
        self,
        container_id: str,
        container_path: str,
        data: bytes,
        *,
        mode: int = 0o644,
    ) -> ContainerSandboxUploadFileResponse:
        return self._unary(
            ContainerServiceMethod.ContainerSandboxUploadFile,
            ContainerSandboxUploadFileRequest(
                container_id=container_id,
                container_path=container_path,
                data=data,
                mode=mode,
            ),
            ContainerSandboxUploadFileResponse,
        )

    def sandbox_download_file(
        self,
        container_id: str,
        container_path: str,
    ) -> ContainerSandboxDownloadFileResponse:
        return self._unary(
            ContainerServiceMethod.ContainerSandboxDownloadFile,
            ContainerSandboxDownloadFileRequest(
                container_id=container_id,
                container_path=container_path,
            ),
            ContainerSandboxDownloadFileResponse,
        )

    def sandbox_delete_file(
        self,
        container_id: str,
        container_path: str,
    ) -> ContainerSandboxDeleteFileResponse:
        return self._unary(
            ContainerServiceMethod.ContainerSandboxDeleteFile,
            ContainerSandboxDeleteFileRequest(
                container_id=container_id,
                container_path=container_path,
            ),
            ContainerSandboxDeleteFileResponse,
        )

    def sandbox_create_directory(
        self,
        container_id: str,
        container_path: str,
        *,
        mode: int = 0o755,
    ) -> ContainerSandboxCreateDirectoryResponse:
        return self._unary(
            ContainerServiceMethod.ContainerSandboxCreateDirectory,
            ContainerSandboxCreateDirectoryRequest(
                container_id=container_id,
                container_path=container_path,
                mode=mode,
            ),
            ContainerSandboxCreateDirectoryResponse,
        )

    def sandbox_delete_directory(
        self,
        container_id: str,
        container_path: str,
    ) -> ContainerSandboxDeleteDirectoryResponse:
        return self._unary(
            ContainerServiceMethod.ContainerSandboxDeleteDirectory,
            ContainerSandboxDeleteDirectoryRequest(
                container_id=container_id,
                container_path=container_path,
            ),
            ContainerSandboxDeleteDirectoryResponse,
        )

    def sandbox_stat_file(
        self,
        container_id: str,
        container_path: str,
    ) -> ContainerSandboxStatFileResponse:
        return self._unary(
            ContainerServiceMethod.ContainerSandboxStatFile,
            ContainerSandboxStatFileRequest(
                container_id=container_id,
                container_path=container_path,
            ),
            ContainerSandboxStatFileResponse,
        )

    def sandbox_list_files(
        self,
        container_id: str,
        container_path: str = ".",
    ) -> ContainerSandboxListFilesResponse:
        return self._unary(
            ContainerServiceMethod.ContainerSandboxListFiles,
            ContainerSandboxListFilesRequest(
                container_id=container_id,
                container_path=container_path,
            ),
            ContainerSandboxListFilesResponse,
        )

    def sandbox_replace_in_files(
        self,
        container_id: str,
        container_path: str,
        pattern: str,
        new_string: str,
    ) -> ContainerSandboxReplaceInFilesResponse:
        return self._unary(
            ContainerServiceMethod.ContainerSandboxReplaceInFiles,
            ContainerSandboxReplaceInFilesRequest(
                container_id=container_id,
                container_path=container_path,
                pattern=pattern,
                new_string=new_string,
            ),
            ContainerSandboxReplaceInFilesResponse,
        )

    def sandbox_find_in_files(
        self,
        container_id: str,
        container_path: str,
        pattern: str,
    ) -> ContainerSandboxFindInFilesResponse:
        return self._unary(
            ContainerServiceMethod.ContainerSandboxFindInFiles,
            ContainerSandboxFindInFilesRequest(
                container_id=container_id,
                container_path=container_path,
                pattern=pattern,
            ),
            ContainerSandboxFindInFilesResponse,
        )

    def sandbox_expose_port(
        self,
        container_id: str,
        port: int,
    ) -> ContainerSandboxExposePortResponse:
        return self._unary(
            ContainerServiceMethod.ContainerSandboxExposePort,
            ContainerSandboxExposePortRequest(container_id=container_id, port=port),
            ContainerSandboxExposePortResponse,
        )

    def sandbox_unexpose_port(
        self,
        container_id: str,
        port: int,
    ) -> ContainerSandboxUnexposePortResponse:
        return self._unary(
            ContainerServiceMethod.ContainerSandboxUnexposePort,
            ContainerSandboxUnexposePortRequest(container_id=container_id, port=port),
            ContainerSandboxUnexposePortResponse,
        )

    def sandbox_update_network_permissions(
        self,
        container_id: str,
        *,
        block_network: bool,
        allow_list: Sequence[str] = (),
    ) -> ContainerSandboxUpdateNetworkPermissionsResponse:
        return self._unary(
            ContainerServiceMethod.ContainerSandboxUpdateNetworkPermissions,
            ContainerSandboxUpdateNetworkPermissionsRequest(
                container_id=container_id,
                block_network=block_network,
                allow_list=tuple(allow_list),
            ),
            ContainerSandboxUpdateNetworkPermissionsResponse,
        )

    def kill(self, container_id: str) -> ContainerKillResponse:
        return self._unary(
            ContainerServiceMethod.ContainerKill,
            ContainerKillRequest(container_id=container_id),
            ContainerKillResponse,
        )

    def stream_logs(
        self,
        container_id: str,
        output: OutputCallback,
        *,
        keepalive_interval_seconds: float = CONTAINER_CLIENT_LOG_KEEPALIVE_SECONDS,
    ) -> None:
        request = ContainerStreamLogsRequest(container_id=container_id)
        stream = self._stream(ContainerServiceMethod.ContainerStreamLogs, request)
        stop_keepalive = threading.Event()
        keepalive_thread = _start_keepalive_thread(
            output,
            stop_keepalive,
            keepalive_interval_seconds,
        )
        try:
            for raw in stream:
                entry = ContainerLogEntry.model_validate(raw)
                if entry.msg:
                    output(OutputMessage(msg=entry.msg))
        except Exception as exc:
            raise ContainerClientStreamError("error receiving from log stream") from exc
        finally:
            stop_keepalive.set()
            if keepalive_thread is not None:
                keepalive_thread.join(timeout=max(0.1, keepalive_interval_seconds))

    def checkpoint(
        self,
        container_id: str,
        *,
        checkpoint_id: str = "",
    ) -> ContainerCheckpointResponse:
        return self._unary(
            ContainerServiceMethod.ContainerCheckpoint,
            ContainerCheckpointRequest(
                container_id=container_id,
                checkpoint_id=checkpoint_id,
            ),
            ContainerCheckpointResponse,
        )

    def archive(self, container_id: str, image_id: str, output: OutputCallback) -> None:
        output(
            OutputMessage(
                archiving=True,
                msg="\nSaving image, this may take a few minutes...\n",
            )
        )
        request = ContainerArchiveRequest(container_id=container_id, image_id=image_id)
        stream = self._stream(ContainerServiceMethod.ContainerArchive, request)
        try:
            for raw in stream:
                response = ContainerArchiveResponse.model_validate(raw)
                if response.error_msg:
                    output(OutputMessage(msg=f"{response.error_msg}\n", archiving=True))
                if not response.done and not response.error_msg:
                    message = (
                        "."
                        if response.progress == 0
                        else generate_progress_bar(response.progress, 100)
                    )
                    output(OutputMessage(msg=message, archiving=True))
                if response.done:
                    if response.success:
                        return
                    raise ContainerArchiveError("image archiving failed")
        except ContainerArchiveError:
            raise
        except Exception as exc:
            raise ContainerClientStreamError("error receiving from archive stream") from exc

    def sync_workspace(
        self,
        request: SyncContainerWorkspaceRequest,
    ) -> SyncContainerWorkspaceResponse:
        return self._unary(
            ContainerServiceMethod.ContainerSyncWorkspace,
            request,
            SyncContainerWorkspaceResponse,
        )

    def _unary(
        self,
        method: ContainerServiceMethod,
        request: ContractModel,
        response_model: type[ResponseT],
        *,
        timeout_seconds: float | None = None,
    ) -> ResponseT:
        return response_model.model_validate(
            self.transport.unary(method, request, timeout_seconds=timeout_seconds)
        )

    def _stream(
        self,
        method: ContainerServiceMethod,
        request: ContractModel,
    ) -> Iterable[ContainerServicePayload]:
        try:
            return self.transport.stream(method, request)
        except Exception as exc:
            raise ContainerClientStreamError(f"error creating {method.value} stream") from exc


def plan_container_client_connection_options(
    service_url: str,
    service_token: str = "",
    *,
    backend_route_id: str = "",
    existing_connection: bool = False,
    max_message_size_bytes: int = CONTAINER_CLIENT_MAX_MESSAGE_SIZE_BYTES,
) -> ContainerClientConnectionOptions:
    if max_message_size_bytes <= 0:
        msg = "max_message_size_bytes must be greater than zero"
        raise ValueError(msg)
    has_token = bool(service_token)
    return ContainerClientConnectionOptions(
        service_url=service_url,
        transport_security=(
            ContainerTransportSecurity.Tls
            if service_url.endswith("443")
            else ContainerTransportSecurity.Insecure
        ),
        backend_route_id=backend_route_id,
        max_receive_message_size_bytes=max_message_size_bytes,
        max_send_message_size_bytes=max_message_size_bytes,
        auth_metadata={"authorization": f"Bearer {service_token}"} if has_token else {},
        unary_interceptors=((ContainerClientInterceptor.Auth,) if has_token else ()),
        existing_connection=existing_connection,
    )


def generate_progress_bar(progress: int, total: int) -> str:
    if total <= 0:
        msg = "total must be greater than zero"
        raise ValueError(msg)
    if progress < 0:
        msg = "progress cannot be negative"
        raise ValueError(msg)
    bar_width = 50
    progress_width = (progress * bar_width) // total
    remaining_width = max(0, bar_width - progress_width)
    progress_bar = f"[{'=' * progress_width}{' ' * remaining_width}]"
    percent = (progress * 100) // total
    up = "\033[A" if percent > 0 else ""
    return f"{up}\r{progress_bar} {percent}%\n"


def _start_keepalive_thread(
    output: OutputCallback,
    stop_event: threading.Event,
    interval_seconds: float,
) -> threading.Thread | None:
    if interval_seconds <= 0:
        return None

    def emit_keepalives() -> None:
        while not stop_event.wait(interval_seconds):
            output(OutputMessage(msg=""))

    thread = threading.Thread(target=emit_keepalives, daemon=True)
    thread.start()
    return thread
