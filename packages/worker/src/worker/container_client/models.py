from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from pydantic import Field, JsonValue
from shared.contracts import ContractModel

CONTAINER_CLIENT_SANDBOX_EXEC_TIMEOUT_SECONDS = 15.0
CONTAINER_CLIENT_SANDBOX_STATUS_TIMEOUT_SECONDS = 5.0
CONTAINER_CLIENT_MAX_MESSAGE_SIZE_BYTES = 1 << 30
CONTAINER_CLIENT_LOG_KEEPALIVE_SECONDS = 10.0

type ContainerServiceWireValue = (
    None
    | bool
    | int
    | float
    | str
    | bytes
    | list[ContainerServiceWireValue]
    | dict[str, ContainerServiceWireValue]
)
type ContainerServicePayload = (
    ContractModel | ContainerServiceWireValue | Mapping[str, ContainerServiceWireValue]
)


class ContainerServiceMethod(StrEnum):
    ContainerStatus = "ContainerStatus"
    ContainerExec = "ContainerExec"
    ContainerSandboxExec = "ContainerSandboxExec"
    ContainerSandboxListExposedPorts = "ContainerSandboxListExposedPorts"
    ContainerSandboxListProcesses = "ContainerSandboxListProcesses"
    ContainerSandboxStatus = "ContainerSandboxStatus"
    ContainerSandboxStdout = "ContainerSandboxStdout"
    ContainerSandboxStderr = "ContainerSandboxStderr"
    ContainerSandboxKill = "ContainerSandboxKill"
    ContainerSandboxUploadFile = "ContainerSandboxUploadFile"
    ContainerSandboxDownloadFile = "ContainerSandboxDownloadFile"
    ContainerSandboxDeleteFile = "ContainerSandboxDeleteFile"
    ContainerSandboxCreateDirectory = "ContainerSandboxCreateDirectory"
    ContainerSandboxDeleteDirectory = "ContainerSandboxDeleteDirectory"
    ContainerSandboxStatFile = "ContainerSandboxStatFile"
    ContainerSandboxListFiles = "ContainerSandboxListFiles"
    ContainerSandboxReplaceInFiles = "ContainerSandboxReplaceInFiles"
    ContainerSandboxFindInFiles = "ContainerSandboxFindInFiles"
    ContainerSandboxExposePort = "ContainerSandboxExposePort"
    ContainerSandboxUnexposePort = "ContainerSandboxUnexposePort"
    ContainerSandboxUpdateNetworkPermissions = "ContainerSandboxUpdateNetworkPermissions"
    ContainerKill = "ContainerKill"
    ContainerStreamLogs = "ContainerStreamLogs"
    ContainerCheckpoint = "ContainerCheckpoint"
    ContainerArchive = "ContainerArchive"
    ContainerSyncWorkspace = "ContainerSyncWorkspace"


class ContainerTransportSecurity(StrEnum):
    Insecure = "insecure"
    Tls = "tls"


class ContainerClientInterceptor(StrEnum):
    Auth = "auth"


class ContainerWorkspaceSyncOperation(StrEnum):
    Delete = "delete"
    Write = "write"
    Move = "move"


class ContainerClientConnectionOptions(ContractModel):
    service_url: str
    transport_security: ContainerTransportSecurity
    backend_route_id: str = ""
    max_receive_message_size_bytes: int = CONTAINER_CLIENT_MAX_MESSAGE_SIZE_BYTES
    max_send_message_size_bytes: int = CONTAINER_CLIENT_MAX_MESSAGE_SIZE_BYTES
    auth_metadata: dict[str, str] = Field(default_factory=dict)
    unary_interceptors: tuple[ContainerClientInterceptor, ...] = Field(default_factory=tuple)
    existing_connection: bool = False

    @property
    def tls(self) -> bool:
        return self.transport_security is ContainerTransportSecurity.Tls


class ContainerStatusRequest(ContractModel):
    container_id: str


class ContainerStatusResponse(ContractModel):
    ok: bool = True
    status: str = ""
    exit_code: int = 0
    build_archive_object_id: str = ""
    build_archive_object_key: str = ""
    build_archive_size_bytes: int = 0
    build_archive_sha256: str = ""
    error_msg: str = ""


class ContainerExecRequest(ContractModel):
    container_id: str
    command: str
    env: tuple[str, ...] = Field(default_factory=tuple)


class ContainerExecResponse(ContractModel):
    ok: bool = True
    error_msg: str = ""
    pid: int = 0
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


class ContainerSandboxExecRequest(ContractModel):
    container_id: str
    command: str
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str = "."


class ContainerSandboxExecResponse(ContractModel):
    ok: bool = True
    error_msg: str = ""
    pid: int = 0


class ContainerSandboxStatusRequest(ContractModel):
    container_id: str
    pid: int


class ContainerSandboxStatusResponse(ContractModel):
    ok: bool = True
    error_msg: str = ""
    status: str = ""
    exit_code: int = 0


class ContainerSandboxListExposedPortsRequest(ContractModel):
    container_id: str


class ContainerSandboxListExposedPortsResponse(ContractModel):
    ok: bool = True
    error_msg: str = ""
    ports: tuple[int, ...] = Field(default_factory=tuple)


class ContainerSandboxProcessInfo(ContractModel):
    pid: int
    command: str = ""


class ContainerSandboxListProcessesRequest(ContractModel):
    container_id: str


class ContainerSandboxListProcessesResponse(ContractModel):
    ok: bool = True
    error_msg: str = ""
    processes: tuple[ContainerSandboxProcessInfo, ...] = Field(default_factory=tuple)


class ContainerSandboxStdoutRequest(ContractModel):
    container_id: str
    pid: int


class ContainerSandboxStdoutResponse(ContractModel):
    ok: bool = True
    error_msg: str = ""
    stdout: str = ""


class ContainerSandboxStderrRequest(ContractModel):
    container_id: str
    pid: int


class ContainerSandboxStderrResponse(ContractModel):
    ok: bool = True
    error_msg: str = ""
    stderr: str = ""


class ContainerSandboxKillRequest(ContractModel):
    container_id: str
    pid: int


class ContainerSandboxKillResponse(ContractModel):
    ok: bool = True
    error_msg: str = ""


class ContainerSandboxUploadFileRequest(ContractModel):
    container_id: str
    container_path: str
    data: bytes = b""
    mode: int = 0o644


class ContainerSandboxUploadFileResponse(ContractModel):
    ok: bool = True
    error_msg: str = ""


class ContainerSandboxDownloadFileRequest(ContractModel):
    container_id: str
    container_path: str


class ContainerSandboxDownloadFileResponse(ContractModel):
    ok: bool = True
    error_msg: str = ""
    data: bytes = b""


class ContainerSandboxDeleteFileRequest(ContractModel):
    container_id: str
    container_path: str


class ContainerSandboxDeleteFileResponse(ContractModel):
    ok: bool = True
    error_msg: str = ""


class ContainerSandboxCreateDirectoryRequest(ContractModel):
    container_id: str
    container_path: str
    mode: int = 0o755


class ContainerSandboxCreateDirectoryResponse(ContractModel):
    ok: bool = True
    error_msg: str = ""


class ContainerSandboxDeleteDirectoryRequest(ContractModel):
    container_id: str
    container_path: str


class ContainerSandboxDeleteDirectoryResponse(ContractModel):
    ok: bool = True
    error_msg: str = ""


class ContainerSandboxFileInfo(ContractModel):
    name: str = ""
    mode: int = 0
    size: int = 0
    mod_time: int = 0
    owner: str = ""
    group: str = ""
    is_dir: bool = False
    permissions: int = 0


class ContainerSandboxStatFileRequest(ContractModel):
    container_id: str
    container_path: str


class ContainerSandboxStatFileResponse(ContractModel):
    ok: bool = True
    error_msg: str = ""
    file_info: ContainerSandboxFileInfo = Field(default_factory=ContainerSandboxFileInfo)


class ContainerSandboxListFilesRequest(ContractModel):
    container_id: str
    container_path: str = "."


class ContainerSandboxListFilesResponse(ContractModel):
    ok: bool = True
    error_msg: str = ""
    files: tuple[ContainerSandboxFileInfo, ...] = Field(default_factory=tuple)


class ContainerSandboxReplaceInFilesRequest(ContractModel):
    container_id: str
    container_path: str
    pattern: str
    new_string: str


class ContainerSandboxReplaceInFilesResponse(ContractModel):
    ok: bool = True
    error_msg: str = ""


class ContainerFileSearchMatch(ContractModel):
    path: str
    text: str = ""
    line: int = 0
    column: int = 0


class ContainerSandboxFindInFilesRequest(ContractModel):
    container_id: str
    container_path: str
    pattern: str


class ContainerSandboxFindInFilesResponse(ContractModel):
    ok: bool = True
    error_msg: str = ""
    results: tuple[ContainerFileSearchMatch, ...] = Field(default_factory=tuple)


class ContainerSandboxExposePortRequest(ContractModel):
    container_id: str
    port: int


class ContainerSandboxExposePortResponse(ContractModel):
    ok: bool = True
    url: str = ""
    error_msg: str = ""


class ContainerSandboxUnexposePortRequest(ContractModel):
    container_id: str
    port: int


class ContainerSandboxUnexposePortResponse(ContractModel):
    ok: bool = True
    error_msg: str = ""


class ContainerSandboxUpdateNetworkPermissionsRequest(ContractModel):
    container_id: str
    block_network: bool = False
    allow_list: tuple[str, ...] = Field(default_factory=tuple)


class ContainerSandboxUpdateNetworkPermissionsResponse(ContractModel):
    ok: bool = True
    error_msg: str = ""


class ContainerKillRequest(ContractModel):
    container_id: str


class ContainerKillResponse(ContractModel):
    ok: bool = True
    error_msg: str = ""


class ContainerStreamLogsRequest(ContractModel):
    container_id: str


class ContainerLogEntry(ContractModel):
    msg: str = ""


class ContainerCheckpointRequest(ContractModel):
    container_id: str
    checkpoint_id: str = ""


class ContainerCheckpointResponse(ContractModel):
    ok: bool = True
    error_msg: str = ""
    checkpoint_id: str = ""


class ContainerArchiveRequest(ContractModel):
    container_id: str
    image_id: str


class ContainerArchiveResponse(ContractModel):
    progress: int = Field(default=0, ge=0, le=100)
    done: bool = False
    success: bool = False
    error_msg: str = ""


class SyncContainerWorkspaceRequest(ContractModel):
    container_id: str
    operation: ContainerWorkspaceSyncOperation
    path: str = ""
    new_path: str = ""
    data: bytes = b""
    mode: int = 0o644
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class SyncContainerWorkspaceResponse(ContractModel):
    ok: bool = True
    error_msg: str = ""
    path: str = ""
