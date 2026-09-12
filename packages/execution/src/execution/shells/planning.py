from __future__ import annotations

import base64
import hashlib
import hmac
import shlex
from enum import StrEnum

from pydantic import Field
from shared.app_identity import CONTAINER_HELPER_PATH
from shared.containers import ContainerStatus
from shared.contracts import ContractModel
from shared.env import GATEWAY_TOKEN_ENV, STUB_ID_ENV

SHELL_ROUTE_PREFIX = "/api/v1/shells"
SHELL_CONTAINER_PREFIX = "shell"
SHELL_PROXY_BUFFER_SIZE_BYTES = 32 * 1024
SHELL_KEEPALIVE_INTERVAL_SECONDS = 60
SHELL_DEFAULT_CONTAINER_CPU_MILLICORES = 100
SHELL_DEFAULT_CONTAINER_MEMORY_MIB = 128
SHELL_CONTAINER_DIAL_TIMEOUT_SECONDS = 300
SHELL_CONTAINER_WAIT_TIMEOUT_SECONDS = 300
SHELL_CONTAINER_WAIT_POLL_INTERVAL_SECONDS = 1
SHELL_SERVER_IDLE_TIMEOUT_SECONDS = 30
SHELL_WORKER_PORT = 2222
SHELL_SERVER_PROBE_TIMEOUT_SECONDS = 5.0
SHELL_SERVER_READY_TIMEOUT_SECONDS = 10.0


def shell_server_command(
    port: int,
    *,
    log_path: str | None = None,
    idle_timeout_seconds: int = SHELL_SERVER_IDLE_TIMEOUT_SECONDS,
) -> str:
    """Shell command that starts the in-container shell server.

    With ``log_path`` the server is started in the background (exec into an
    already-running container); without it the command replaces the current
    process (standalone shell container entrypoint).
    """
    launch = (
        f"{shlex.quote(CONTAINER_HELPER_PATH)} shell --port {port} "
        f"--idle-timeout {idle_timeout_seconds}s"
    )
    if log_path is None:
        return f"exec {launch}"
    return f"({launch} >{shlex.quote(log_path)} 2>&1 &)"


def shell_server_exec_command(
    port: int,
    *,
    log_path: str,
    idle_timeout_seconds: int = SHELL_SERVER_IDLE_TIMEOUT_SECONDS,
) -> str:
    """Serialize the compound server script for the worker's direct-argv exec API."""
    return shlex.join(
        (
            "/bin/sh",
            "-lc",
            shell_server_command(
                port,
                log_path=log_path,
                idle_timeout_seconds=idle_timeout_seconds,
            ),
        )
    )


def shell_server_probe_command(port: int, *, timeout_seconds: float) -> str:
    timeout = max(timeout_seconds, 0.1)
    command = (
        f"{shlex.quote(CONTAINER_HELPER_PATH)} shell --probe --port {port} --timeout {timeout:g}s"
    )
    return shlex.join(("/bin/sh", "-lc", command))


class ShellContainerEnvVar(StrEnum):
    Handler = "HANDLER"
    GatewayToken = GATEWAY_TOKEN_ENV
    StubId = STUB_ID_ENV
    Username = "USERNAME"
    Password = "PASSWORD"


class ShellExistingContainerStatus(StrEnum):
    Ready = "ready"
    ContainerNotRunning = "container-not-running"
    WorkspaceMismatch = "workspace-mismatch"
    ContainerNotFound = "container-not-found"


class ShellCredentialPlan(ContractModel):
    username: str
    password: str


class ShellStandaloneRequest(ContractModel):
    stub_id: str
    handler: str
    gateway_token: str
    token_external_id: str
    token_key: str
    container_id: str = ""
    container_id_suffix: str = ""
    cpu_millicores: int = 0
    memory_mib: int = 0
    disk_mib: int = Field(gt=0)
    gpu: tuple[str, ...] = ()
    gpu_count: int = 0
    requires_gpu: bool = False
    image_id: str = ""
    app_id: str = ""
    workspace_id: str = ""


class ShellStandalonePlan(ContractModel):
    container_id: str
    credentials: ShellCredentialPlan
    worker_port: int
    idle_timeout_seconds: int
    cpu_millicores: int
    memory_mib: int
    disk_mib: int = Field(gt=0)
    gpu: tuple[str, ...]
    gpu_count: int
    env: tuple[str, ...]
    entrypoint: tuple[str, ...]
    wait_timeout_seconds: int
    wait_poll_interval_seconds: int


class ShellExistingContainerPlan(ContractModel):
    status: ShellExistingContainerStatus
    container_id: str
    stub_id: str = ""
    credentials: ShellCredentialPlan | None = None
    expose_port: int = SHELL_WORKER_PORT
    idle_timeout_seconds: int = SHELL_SERVER_IDLE_TIMEOUT_SECONDS
    dial_timeout_seconds: int = SHELL_CONTAINER_DIAL_TIMEOUT_SECONDS
    error_message: str = ""

    @property
    def ok(self) -> bool:
        return self.status is ShellExistingContainerStatus.Ready


class ShellProxyPlan(ContractModel):
    route_path: str
    container_id: str
    stub_id: str
    worker_port: int = SHELL_WORKER_PORT
    buffer_size_bytes: int = SHELL_PROXY_BUFFER_SIZE_BYTES
    keepalive_interval_seconds: int = SHELL_KEEPALIVE_INTERVAL_SECONDS
    dial_timeout_seconds: int = SHELL_CONTAINER_DIAL_TIMEOUT_SECONDS


def shell_container_id(stub_id: str, suffix: str) -> str:
    return f"{SHELL_CONTAINER_PREFIX}-{stub_id}-{suffix}"


def shell_credentials(token_external_id: str, token_key: str) -> ShellCredentialPlan:
    username = token_external_id.replace("-", "")[:6] or "shell"
    return ShellCredentialPlan(username=username, password=token_key)


def existing_container_shell_credentials(
    *,
    workspace_signing_key: str,
    container_id: str,
    token_external_id: str,
) -> ShellCredentialPlan:
    if not workspace_signing_key:
        msg = "workspace signing key is required for existing-container shell credentials"
        raise ValueError(msg)
    digest = hmac.new(
        workspace_signing_key.encode(),
        f"shell:{container_id}".encode(),
        hashlib.sha256,
    ).digest()
    password = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return shell_credentials(token_external_id, password)


def plan_shell_standalone(request: ShellStandaloneRequest) -> ShellStandalonePlan:
    suffix = request.container_id_suffix or "pending"
    container_id = request.container_id or shell_container_id(request.stub_id, suffix)
    credentials = shell_credentials(request.token_external_id, request.token_key)
    cpu = (
        request.cpu_millicores
        if request.cpu_millicores > 0
        else SHELL_DEFAULT_CONTAINER_CPU_MILLICORES
    )
    memory = request.memory_mib if request.memory_mib > 0 else SHELL_DEFAULT_CONTAINER_MEMORY_MIB
    gpu_count = request.gpu_count
    if request.requires_gpu and gpu_count == 0:
        gpu_count = 1
    env = (
        f"{ShellContainerEnvVar.Handler.value}={request.handler}",
        f"{ShellContainerEnvVar.GatewayToken.value}={request.gateway_token}",
        f"{ShellContainerEnvVar.StubId.value}={request.stub_id}",
        f"{ShellContainerEnvVar.Username.value}={credentials.username}",
        f"{ShellContainerEnvVar.Password.value}={credentials.password}",
    )
    return ShellStandalonePlan(
        container_id=container_id,
        credentials=credentials,
        worker_port=SHELL_WORKER_PORT,
        idle_timeout_seconds=SHELL_SERVER_IDLE_TIMEOUT_SECONDS,
        cpu_millicores=cpu,
        memory_mib=memory,
        disk_mib=request.disk_mib,
        gpu=request.gpu,
        gpu_count=gpu_count,
        env=env,
        entrypoint=(
            "/bin/sh",
            "-lc",
            shell_server_command(
                SHELL_WORKER_PORT,
                idle_timeout_seconds=SHELL_SERVER_IDLE_TIMEOUT_SECONDS,
            ),
        ),
        wait_timeout_seconds=SHELL_CONTAINER_WAIT_TIMEOUT_SECONDS,
        wait_poll_interval_seconds=SHELL_CONTAINER_WAIT_POLL_INTERVAL_SECONDS,
    )


def plan_existing_shell_container(
    *,
    container_id: str,
    container_status: ContainerStatus | None,
    container_workspace_id: str,
    request_workspace_id: str,
    stub_id: str = "",
    token_external_id: str = "",
    token_key: str = "",
) -> ShellExistingContainerPlan:
    if container_status is None:
        return ShellExistingContainerPlan(
            status=ShellExistingContainerStatus.ContainerNotFound,
            container_id=container_id,
            error_message="Container not found",
        )
    if container_status is not ContainerStatus.Running:
        return ShellExistingContainerPlan(
            status=ShellExistingContainerStatus.ContainerNotRunning,
            container_id=container_id,
            stub_id=stub_id,
            error_message="Container is not running",
        )
    if container_workspace_id != request_workspace_id:
        return ShellExistingContainerPlan(
            status=ShellExistingContainerStatus.WorkspaceMismatch,
            container_id=container_id,
            stub_id=stub_id,
            error_message="Container not found",
        )
    return ShellExistingContainerPlan(
        status=ShellExistingContainerStatus.Ready,
        container_id=container_id,
        stub_id=stub_id,
        credentials=shell_credentials(token_external_id, token_key),
    )


def plan_shell_proxy(stub_id: str, container_id: str) -> ShellProxyPlan:
    return ShellProxyPlan(
        route_path=f"{SHELL_ROUTE_PREFIX}/id/{stub_id}/{container_id}",
        stub_id=stub_id,
        container_id=container_id,
    )
