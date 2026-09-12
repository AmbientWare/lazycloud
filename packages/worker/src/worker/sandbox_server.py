from __future__ import annotations

import posixpath
import shlex
from enum import StrEnum

from pydantic import Field, field_validator
from shared.app_identity import HOME_DIR
from shared.container_requests import CONTAINER_INNER_PORT
from shared.contracts import ContractModel
from shared.routing import AgentBackendRoute, BackendRouteKind, parse_backend_route_address

from worker.execution import PortBinding
from worker.lifecycle import WORKER_SANDBOX_PROCESS_MANAGER_PORT, WORKER_SHELL_PORT
from worker.routes import (
    WorkerRouteContext,
    build_agent_backend_route,
)
from worker.runtime_config import OciRuntimeName

WORKER_CONTAINER_UPLOADS_HOST_PATH = "/tmp/container-uploads"
WORKER_CONTAINER_UPLOADS_MOUNT_PATH = f"/tmp/{HOME_DIR}"
SANDBOX_INTERNAL_PORTS = (
    WORKER_SANDBOX_PROCESS_MANAGER_PORT,
    WORKER_SHELL_PORT,
    CONTAINER_INNER_PORT,
)


class SandboxProcessStatus(StrEnum):
    Pending = "pending"
    Running = "running"
    Exited = "exited"


class SandboxLogStream(StrEnum):
    Stdout = "stdout"
    Stderr = "stderr"


class SandboxFileOperation(StrEnum):
    UploadFile = "upload-file"
    CreateDirectory = "create-directory"
    DeleteDirectory = "delete-directory"
    DownloadFile = "download-file"
    DeleteFile = "delete-file"
    StatFile = "stat-file"
    ListFiles = "list-files"
    ReplaceInFiles = "replace-in-files"
    FindInFiles = "find-in-files"


class SandboxFileAccessMode(StrEnum):
    DirectHostPath = "direct-host-path"
    SandboxedRuntimeStagedUpload = "sandboxed-runtime-staged-upload"


class SandboxStatusPlan(ContractModel):
    ok: bool
    status: SandboxProcessStatus = SandboxProcessStatus.Pending
    exit_code: int = -1
    error_message: str = ""


class SandboxProcessLogEntry(ContractModel):
    container_id: str
    stub_id: str
    workspace_id: str
    app_id: str
    worker_id: str
    stream: SandboxLogStream
    line: str
    pid: int
    process_args: list[str] = Field(default_factory=list)
    process_cwd: str = ""
    process_seq: int


class SandboxProcessLogAckPlan(ContractModel):
    ok: bool
    seq: int
    entry: SandboxProcessLogEntry
    error_message: str = ""


class SandboxExposedPortRecordPlan(ContractModel):
    ports: list[int]
    appended: bool


class SandboxExposedPortsPlan(ContractModel):
    ok: bool
    exposed_ports: list[int] = Field(default_factory=list)
    excluded_ports: list[int] = Field(default_factory=list)
    error_message: str = ""


class SandboxContainerMount(ContractModel):
    source: str
    destination: str

    @field_validator("source", "destination")
    @classmethod
    def path_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            msg = "sandbox mount paths cannot be blank"
            raise ValueError(msg)
        return stripped


class SandboxContainerPathResolution(ContractModel):
    container_path: str
    host_path: str
    root_path: str
    mount_source: str = ""
    mount_destination: str = ""
    used_mount: bool = False


class SandboxFileOperationPlan(ContractModel):
    operation: SandboxFileOperation
    container_id: str
    container_path: str
    host_path: str
    access_mode: SandboxFileAccessMode = SandboxFileAccessMode.DirectHostPath
    mode: int = 0o644
    data_size_bytes: int = 0
    create_parent_directory: bool = False
    temp_host_path: str = ""
    temp_container_path: str = ""
    command: str = ""
    ok: bool = True
    error_message: str = ""


class SandboxExposePortRequest(ContractModel):
    container_id: str
    port: int
    existing_address_map: dict[int, str] = Field(default_factory=dict)
    existing_ports: list[int] = Field(default_factory=list)
    host_port: int = 0
    local_target: str = ""
    route_context: WorkerRouteContext | None = None

    @field_validator("port")
    @classmethod
    def port_must_be_valid(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            msg = "sandbox exposed port must be between 1 and 65535"
            raise ValueError(msg)
        return value

    @field_validator("host_port")
    @classmethod
    def host_port_must_be_valid(cls, value: int) -> int:
        if value and not 1 <= value <= 65535:
            msg = "sandbox host port must be between 1 and 65535"
            raise ValueError(msg)
        return value


class SandboxExposePortPlan(ContractModel):
    ok: bool
    container_id: str
    port: int
    address_map: dict[int, str] = Field(default_factory=dict)
    routes: list[AgentBackendRoute] = Field(default_factory=list)
    exposed_ports: list[int] = Field(default_factory=list)
    bind: PortBinding | None = None
    record_port: bool = False
    existing_target: bool = False
    error_message: str = ""


def plan_sandbox_status(
    *,
    manager_ready: bool,
    pid: int = 0,
    process_exit_code: int | None = None,
) -> SandboxStatusPlan:
    if pid == 0:
        return SandboxStatusPlan(
            ok=True,
            status=SandboxProcessStatus.Running if manager_ready else SandboxProcessStatus.Pending,
            exit_code=-1,
        )
    if not manager_ready:
        return SandboxStatusPlan(
            ok=False,
            error_message="Sandbox process manager is not ready",
        )
    if process_exit_code is None:
        return SandboxStatusPlan(ok=True, status=SandboxProcessStatus.Running, exit_code=-1)
    return SandboxStatusPlan(
        ok=True,
        status=SandboxProcessStatus.Exited,
        exit_code=process_exit_code,
    )


def plan_sandbox_process_log_ack(
    *,
    container_id: str,
    stub_id: str,
    workspace_id: str,
    app_id: str,
    worker_id: str,
    stream: SandboxLogStream,
    seq: int,
    pid: int,
    data: bytes | str,
    process_args: list[str] | None = None,
    process_cwd: str = "",
    persist_error: str = "",
) -> SandboxProcessLogAckPlan:
    line = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
    entry = SandboxProcessLogEntry(
        container_id=container_id,
        stub_id=stub_id,
        workspace_id=workspace_id,
        app_id=app_id,
        worker_id=worker_id,
        stream=stream,
        line=line,
        pid=pid,
        process_args=list(process_args or []),
        process_cwd=process_cwd,
        process_seq=seq,
    )
    return SandboxProcessLogAckPlan(
        ok=not persist_error,
        seq=seq,
        entry=entry,
        error_message=persist_error,
    )


def plan_sandbox_list_exposed_ports(
    request_ports: list[int],
    *,
    wait_error: str = "",
    internal_ports: tuple[int, ...] = SANDBOX_INTERNAL_PORTS,
) -> SandboxExposedPortsPlan:
    if wait_error:
        return SandboxExposedPortsPlan(ok=False, error_message=wait_error)
    internal = set(internal_ports)
    exposed: list[int] = []
    excluded: list[int] = []
    for port in request_ports:
        if port in internal:
            excluded.append(port)
            continue
        exposed.append(port)
    return SandboxExposedPortsPlan(ok=True, exposed_ports=exposed, excluded_ports=excluded)


def resolve_sandbox_container_path(
    container_path: str,
    *,
    cwd: str = "/workspace",
) -> str:
    path = container_path.strip()
    if not path:
        msg = "sandbox container path is required"
        raise ValueError(msg)
    if not path.startswith("/"):
        path = posixpath.join(cwd or "/", path)
    normalized = posixpath.normpath(path)
    return normalized if normalized.startswith("/") else f"/{normalized}"


def resolve_sandbox_host_path(
    container_path: str,
    *,
    root_path: str,
    mounts: list[SandboxContainerMount] | None = None,
    cwd: str = "/workspace",
) -> SandboxContainerPathResolution:
    normalized = resolve_sandbox_container_path(container_path, cwd=cwd)
    for mount in sorted(mounts or [], key=lambda item: len(item.destination), reverse=True):
        destination = posixpath.normpath(mount.destination)
        if normalized == destination or normalized.startswith(f"{destination}/"):
            relative = normalized.removeprefix(destination).lstrip("/")
            return SandboxContainerPathResolution(
                container_path=normalized,
                host_path=posixpath.normpath(posixpath.join(mount.source, relative)),
                root_path=root_path,
                mount_source=mount.source,
                mount_destination=destination,
                used_mount=True,
            )
    return SandboxContainerPathResolution(
        container_path=normalized,
        host_path=posixpath.normpath(posixpath.join(root_path, normalized.lstrip("/"))),
        root_path=root_path,
    )


def plan_sandbox_file_operation(
    operation: SandboxFileOperation,
    *,
    container_id: str,
    container_path: str,
    root_path: str,
    mounts: list[SandboxContainerMount] | None = None,
    cwd: str = "/workspace",
    mode: int = 0o644,
    data_size_bytes: int = 0,
) -> SandboxFileOperationPlan:
    if data_size_bytes < 0:
        msg = "sandbox file data size cannot be negative"
        raise ValueError(msg)
    resolution = resolve_sandbox_host_path(
        container_path,
        root_path=root_path,
        mounts=mounts,
        cwd=cwd,
    )
    return SandboxFileOperationPlan(
        operation=operation,
        container_id=container_id,
        container_path=resolution.container_path,
        host_path=resolution.host_path,
        mode=mode,
        data_size_bytes=data_size_bytes,
        create_parent_directory=operation
        in {SandboxFileOperation.UploadFile, SandboxFileOperation.CreateDirectory},
    )


def plan_sandbox_upload_file(
    *,
    container_id: str,
    container_path: str,
    root_path: str,
    mounts: list[SandboxContainerMount] | None = None,
    cwd: str = "/workspace",
    runtime: OciRuntimeName | str = OciRuntimeName.Runsc,
    mode: int = 0o644,
    data_size_bytes: int = 0,
    upload_file_name: str = "upload",
    uploads_host_root: str = WORKER_CONTAINER_UPLOADS_HOST_PATH,
    uploads_mount_path: str = WORKER_CONTAINER_UPLOADS_MOUNT_PATH,
) -> SandboxFileOperationPlan:
    plan = plan_sandbox_file_operation(
        SandboxFileOperation.UploadFile,
        container_id=container_id,
        container_path=container_path,
        root_path=root_path,
        mounts=mounts,
        cwd=cwd,
        mode=mode,
        data_size_bytes=data_size_bytes,
    )
    if not _is_sandboxed_oci_runtime(runtime):
        return plan

    temp_file = posixpath.basename(upload_file_name.strip()) or "upload"
    temp_host_path = posixpath.join(uploads_host_root, container_id, temp_file)
    temp_container_path = posixpath.join(uploads_mount_path, temp_file)
    command = " && ".join(
        [
            f"mkdir -p {shlex.quote(posixpath.dirname(plan.container_path))}",
            f"mv {shlex.quote(temp_container_path)} {shlex.quote(plan.container_path)}",
            f"chmod {mode:o} {shlex.quote(plan.container_path)}",
        ]
    )
    return plan.model_copy(
        update={
            "access_mode": SandboxFileAccessMode.SandboxedRuntimeStagedUpload,
            "temp_host_path": temp_host_path,
            "temp_container_path": temp_container_path,
            "command": command,
        }
    )


def writable_container_address_map(address_map: dict[int, str] | None) -> dict[int, str]:
    return dict(address_map or {})


def record_sandbox_exposed_port(
    existing_ports: list[int],
    port: int,
) -> SandboxExposedPortRecordPlan:
    ports = list(existing_ports)
    if port in ports:
        return SandboxExposedPortRecordPlan(ports=ports, appended=False)
    ports.append(port)
    return SandboxExposedPortRecordPlan(ports=ports, appended=True)


def backend_route_for_sandbox_port(
    context: WorkerRouteContext | None,
    *,
    port: int,
    local_target: str,
) -> AgentBackendRoute | None:
    if context is None or not local_target:
        return None
    _, is_route = parse_backend_route_address(local_target)
    if is_route:
        return None
    return build_agent_backend_route(
        context,
        kind=BackendRouteKind.Container,
        port=port,
        local_target=local_target,
    )


def plan_sandbox_expose_port(request: SandboxExposePortRequest) -> SandboxExposePortPlan:
    address_map = writable_container_address_map(request.existing_address_map)
    target = address_map.get(request.port, "")
    existing_target = bool(target)
    bind: PortBinding | None = None
    if not existing_target:
        if not request.local_target:
            return SandboxExposePortPlan(
                ok=False,
                container_id=request.container_id,
                port=request.port,
                address_map=address_map,
                exposed_ports=list(request.existing_ports),
                error_message="container port address unavailable",
            )
        target = request.local_target
        address_map[request.port] = target
        if request.host_port:
            bind = PortBinding(host_port=request.host_port, container_port=request.port)

    route = backend_route_for_sandbox_port(
        request.route_context,
        port=request.port,
        local_target=target,
    )
    ports = record_sandbox_exposed_port(request.existing_ports, request.port)
    return SandboxExposePortPlan(
        ok=True,
        container_id=request.container_id,
        port=request.port,
        address_map=address_map,
        routes=[route] if route is not None else [],
        exposed_ports=ports.ports,
        bind=bind,
        record_port=ports.appended,
        existing_target=existing_target,
    )


def _is_sandboxed_oci_runtime(value: OciRuntimeName | str) -> bool:
    if isinstance(value, OciRuntimeName):
        return value is OciRuntimeName.Runsc
    return value.strip().lower() in {"runsc", "gvisor"}
