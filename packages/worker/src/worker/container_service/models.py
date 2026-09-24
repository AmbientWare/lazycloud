from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import Field
from shared.container_requests import WORKER_USER_CODE_VOLUME
from shared.contracts import ContractModel
from shared.placement import Placement

from worker.checkpoint_readiness import CheckpointReadinessProbe
from worker.routes import WorkerRouteContext
from worker.runtime_config import OciRuntimeName
from worker.sandbox_server import SandboxContainerMount, SandboxFileOperation, SandboxLogStream

CONTAINER_NOT_FOUND_MESSAGE = "Container not found"
SANDBOX_FILESYSTEM_OVER_LIMIT_EXIT = 3
"""The supervisor's exit status for a download larger than its limit."""


class SandboxFileOverLimitError(ValueError):
    """The supervisor refused a download larger than the limit it was given."""


SANDBOX_PROCESS_MANAGER_NOT_READY_MESSAGE = "Sandbox process manager is not ready"


class SandboxFilesystemRequest(ContractModel):
    operation: SandboxFileOperation
    path: str
    source: str = ""
    mode: int = 0o644
    pattern: str = ""
    replacement: str = ""
    # Left out when unset: a supervisor started before these fields existed
    # rejects unknown keys.
    limit: int = Field(default=0, ge=0, exclude_if=lambda value: value == 0)
    """Entries a listing returns or bytes a download writes; 0 is unbounded."""

    truncate: bool = Field(default=False, exclude_if=lambda value: not value)
    """Write the first `limit` bytes of a larger file instead of refusing it."""


class SandboxProcessEventType(StrEnum):
    Started = "started"
    Chunk = "chunk"
    Exited = "exited"


class SandboxDockerDaemonStatus(StrEnum):
    Disabled = "disabled"
    Starting = "starting"
    Ready = "ready"
    Stopping = "stopping"
    Stopped = "stopped"
    Failed = "failed"


class SandboxProcessEvent(ContractModel):
    event_type: SandboxProcessEventType
    pid: int = 0
    seq: int = 0
    stream: SandboxLogStream = SandboxLogStream.Stdout
    data: bytes = b""
    exit_code: int = -1
    error_message: str = ""


class WorkerSandboxProcess(ContractModel):
    pid: int
    command: str
    cwd: str = ""
    env: list[str] = Field(default_factory=list)
    exit_code: int = -1
    running: bool = True


class WorkerContainerServiceInstance(ContractModel):
    container_id: str
    root_path: str
    bundle_path: str = ""
    config_path: str = ""
    top_layer_path: str = ""
    upper_path: str = ""
    workspace_path: str = ""
    cwd: str = "/workspace"
    runtime: OciRuntimeName = OciRuntimeName.Runsc
    env: list[str] = Field(default_factory=list)
    request_env: list[str] = Field(default_factory=list)
    image_env: list[str] = Field(default_factory=list, repr=False)
    build_secret_env: list[str] = Field(default_factory=list)
    build_request: bool = False
    status: str = ""
    exit_code: int = 0
    build_error_message: str = ""
    sandbox_process_manager_ready: bool = False
    sandbox_supervisor_token_path: str = ""
    sandbox_process_next_pid: int = 1
    sandbox_processes: dict[int, WorkerSandboxProcess] = Field(default_factory=dict)
    sandbox_process_stdout: dict[int, str] = Field(default_factory=dict)
    sandbox_process_stderr: dict[int, str] = Field(default_factory=dict)
    docker_enabled: bool = False
    docker_daemon_pid: int = 0
    docker_daemon_status: SandboxDockerDaemonStatus = SandboxDockerDaemonStatus.Disabled
    docker_daemon_error: str = ""
    ports: list[int] = Field(default_factory=list)
    exposed_ports: list[int] = Field(default_factory=list)
    address_map: dict[int, str] = Field(default_factory=dict)
    mounts: list[SandboxContainerMount] = Field(default_factory=list)
    log_messages: list[str] = Field(default_factory=list)
    network_blocked: bool = False
    network_allow_list: list[str] = Field(default_factory=list)
    workspace_id: str = ""
    workspace_name: str = ""
    app_id: str = ""
    stub_id: str = ""
    stub_type: str = ""
    worker_id: str = ""
    machine_id: str = ""
    placement: Placement = Placement.platform()
    image_id: str = ""
    build_archive_object_key: str = ""
    build_archive_size_bytes: int = 0
    build_archive_sha256: str = ""
    container_ip: str = ""
    workspace_storage_available: bool = False
    cache_available: bool = False
    gpu: str = ""
    gpu_count: int = 0
    checkpoint_readiness: CheckpointReadinessProbe | None = None

    @property
    def workspace_root(self) -> str:
        for mount in self.mounts:
            if mount.destination == WORKER_USER_CODE_VOLUME and mount.source:
                return mount.source
        return self.workspace_path or str(Path(self.root_path) / self.cwd.strip("/"))

    @property
    def route_context(self) -> WorkerRouteContext | None:
        if not (self.workspace_id and self.machine_id and self.worker_id and self.placement):
            return None
        return WorkerRouteContext(
            workspace_id=self.workspace_id,
            placement=self.placement,
            machine_id=self.machine_id,
            worker_id=self.worker_id,
            container_id=self.container_id,
        )
