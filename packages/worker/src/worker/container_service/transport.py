from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass

from shared.contracts import ContractModel

from worker.container_client.models import (
    ContainerArchiveRequest,
    ContainerCheckpointRequest,
    ContainerExecRequest,
    ContainerKillRequest,
    ContainerSandboxCreateDirectoryRequest,
    ContainerSandboxDeleteDirectoryRequest,
    ContainerSandboxDeleteFileRequest,
    ContainerSandboxDownloadFileRequest,
    ContainerSandboxExecRequest,
    ContainerSandboxExposePortRequest,
    ContainerSandboxFindInFilesRequest,
    ContainerSandboxKillRequest,
    ContainerSandboxListExposedPortsRequest,
    ContainerSandboxListFilesRequest,
    ContainerSandboxListProcessesRequest,
    ContainerSandboxReplaceInFilesRequest,
    ContainerSandboxStatFileRequest,
    ContainerSandboxStatusRequest,
    ContainerSandboxStderrRequest,
    ContainerSandboxStdoutRequest,
    ContainerSandboxUnexposePortRequest,
    ContainerSandboxUpdateNetworkPermissionsRequest,
    ContainerSandboxUploadFileRequest,
    ContainerServiceMethod,
    ContainerServicePayload,
    ContainerStatusRequest,
    ContainerStreamLogsRequest,
    SyncContainerWorkspaceRequest,
)
from worker.container_service.service import WorkerContainerService


@dataclass(slots=True)
class WorkerContainerServiceTransport:
    service: WorkerContainerService

    def unary(
        self,
        method: ContainerServiceMethod,
        request: ContainerServicePayload,
        *,
        timeout_seconds: float | None = None,
    ) -> ContainerServicePayload:
        _ = timeout_seconds
        match method:
            case ContainerServiceMethod.ContainerStatus:
                return self.service.container_status(_request(request, ContainerStatusRequest))
            case ContainerServiceMethod.ContainerExec:
                return self.service.container_exec(_request(request, ContainerExecRequest))
            case ContainerServiceMethod.ContainerSandboxExec:
                return self.service.sandbox_exec(_request(request, ContainerSandboxExecRequest))
            case ContainerServiceMethod.ContainerSandboxListExposedPorts:
                return self.service.sandbox_list_exposed_ports(
                    _request(request, ContainerSandboxListExposedPortsRequest)
                )
            case ContainerServiceMethod.ContainerSandboxListProcesses:
                return self.service.sandbox_list_processes(
                    _request(request, ContainerSandboxListProcessesRequest)
                )
            case ContainerServiceMethod.ContainerSandboxStatus:
                return self.service.sandbox_status(_request(request, ContainerSandboxStatusRequest))
            case ContainerServiceMethod.ContainerSandboxStdout:
                return self.service.sandbox_stdout(_request(request, ContainerSandboxStdoutRequest))
            case ContainerServiceMethod.ContainerSandboxStderr:
                return self.service.sandbox_stderr(_request(request, ContainerSandboxStderrRequest))
            case ContainerServiceMethod.ContainerSandboxKill:
                return self.service.sandbox_kill(_request(request, ContainerSandboxKillRequest))
            case ContainerServiceMethod.ContainerSandboxUploadFile:
                return self.service.sandbox_upload_file(
                    _request(request, ContainerSandboxUploadFileRequest)
                )
            case ContainerServiceMethod.ContainerSandboxDownloadFile:
                return self.service.sandbox_download_file(
                    _request(request, ContainerSandboxDownloadFileRequest)
                )
            case ContainerServiceMethod.ContainerSandboxDeleteFile:
                return self.service.sandbox_delete_file(
                    _request(request, ContainerSandboxDeleteFileRequest)
                )
            case ContainerServiceMethod.ContainerSandboxCreateDirectory:
                return self.service.sandbox_create_directory(
                    _request(request, ContainerSandboxCreateDirectoryRequest)
                )
            case ContainerServiceMethod.ContainerSandboxDeleteDirectory:
                return self.service.sandbox_delete_directory(
                    _request(request, ContainerSandboxDeleteDirectoryRequest)
                )
            case ContainerServiceMethod.ContainerSandboxStatFile:
                return self.service.sandbox_stat_file(
                    _request(request, ContainerSandboxStatFileRequest)
                )
            case ContainerServiceMethod.ContainerSandboxListFiles:
                return self.service.sandbox_list_files(
                    _request(request, ContainerSandboxListFilesRequest)
                )
            case ContainerServiceMethod.ContainerSandboxReplaceInFiles:
                return self.service.sandbox_replace_in_files(
                    _request(request, ContainerSandboxReplaceInFilesRequest)
                )
            case ContainerServiceMethod.ContainerSandboxFindInFiles:
                return self.service.sandbox_find_in_files(
                    _request(request, ContainerSandboxFindInFilesRequest)
                )
            case ContainerServiceMethod.ContainerSandboxExposePort:
                return self.service.sandbox_expose_port(
                    _request(request, ContainerSandboxExposePortRequest)
                )
            case ContainerServiceMethod.ContainerSandboxUnexposePort:
                return self.service.sandbox_unexpose_port(
                    _request(request, ContainerSandboxUnexposePortRequest)
                )
            case ContainerServiceMethod.ContainerSandboxUpdateNetworkPermissions:
                return self.service.sandbox_update_network_permissions(
                    _request(request, ContainerSandboxUpdateNetworkPermissionsRequest)
                )
            case ContainerServiceMethod.ContainerKill:
                return self.service.container_kill(_request(request, ContainerKillRequest))
            case ContainerServiceMethod.ContainerCheckpoint:
                return self.service.container_checkpoint(
                    _request(request, ContainerCheckpointRequest)
                )
            case ContainerServiceMethod.ContainerSyncWorkspace:
                return self.service.sync_workspace(_request(request, SyncContainerWorkspaceRequest))
            case _:
                msg = f"{method.value} is a streaming container service method"
                raise ValueError(msg)

    def stream(
        self,
        method: ContainerServiceMethod,
        request: ContainerServicePayload,
        *,
        timeout_seconds: float | None = None,
    ) -> Generator[ContainerServicePayload, None, None]:
        _ = timeout_seconds
        match method:
            case ContainerServiceMethod.ContainerStreamLogs:
                yield from self.service.stream_logs(_request(request, ContainerStreamLogsRequest))
            case ContainerServiceMethod.ContainerArchive:
                yield from self.service.container_archive(
                    _request(request, ContainerArchiveRequest)
                )
            case _:
                msg = f"{method.value} is a unary container service method"
                raise ValueError(msg)


def _request[RequestT: ContractModel](
    request: ContainerServicePayload,
    model: type[RequestT],
) -> RequestT:
    if isinstance(request, model):
        return request
    return model.model_validate(request)
