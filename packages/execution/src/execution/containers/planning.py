from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import Field
from shared.container_requests import (
    DEFAULT_WORKSPACE_STORAGE_BASE_MOUNT_PATH,
    OciRuntimeName,
    RequestMount,
    WorkerStartupKind,
)
from shared.contracts import ContractModel
from shared.deployment_records import DEFAULT_DISK
from shared.errors import InvalidInputError
from shared.resources import parse_memory_mib
from shared.workload_config import StubRuntimeConfig

# The platform ceiling in MiB, for paths that start a container without a
# workload config to read it from.
DEFAULT_CONTAINER_DISK_MIB = parse_memory_mib(DEFAULT_DISK) or 0


class ContainerSchedulingOptions(ContractModel):
    workspace_name: str = "default"
    stub_type: str = "container"
    startup_kind: WorkerStartupKind = WorkerStartupKind.Pod
    entrypoint: list[str] | None = None
    cwd: str | Path | None = None
    env: dict[str, str] | None = None
    env_list: list[str] | None = None
    image_id: str | None = None
    app_id: str | None = None
    deployment_id: str | None = None
    ports: list[int] | None = None
    requested_ports: list[int] | None = None
    checkpoint_exposed_ports: list[int] | None = None
    checkpoint_id: str = ""
    checkpoint_enabled: bool = False
    checkpoint_readiness_path: str = ""
    checkpoint_readiness_port: int = 0
    checkpoint_readiness_timeout_seconds: int = 600
    checkpoint_readiness_interval_seconds: float = 1.0
    cpu_millicores: int = 0
    cpu_limit_millicores: int = 0
    memory_mib: int = 0
    memory_limit_mib: int = 0
    # Required and positive: a start path that forgets the ceiling must fail
    # here rather than silently fall back to the platform default, and zero
    # would mean unlimited, which no container gets.
    disk_mib: int = Field(gt=0)
    gpu: list[str] = Field(default_factory=list)
    gpu_count: int = 0
    pool_selector: str = ""
    runtime: OciRuntimeName | str = OciRuntimeName.Runsc
    runtime_class: str = ""
    docker_enabled: bool = False
    block_network: bool = False
    allow_list: list[str] | None = None
    preemptible: bool = False
    workspace_gpu_quota: int = 0
    workspace_cpu_quota_millicores: int = 0
    mounts: list[RequestMount] | None = None
    secret_names: list[str] | None = None
    gateway_token_required: bool = False
    workspace_storage_required: bool = False
    workspace_storage_available: bool = False
    workspace_storage_base_mount_path: str = DEFAULT_WORKSPACE_STORAGE_BASE_MOUNT_PATH
    ready_at: datetime | None = None


def resolve_oci_runtime(
    *,
    runtime: OciRuntimeName | str,
    runtime_class: str,
    docker_enabled: bool,
) -> tuple[OciRuntimeName, str]:
    del docker_enabled
    runtime_name = _normalize_oci_runtime(runtime_class.strip() or runtime)
    # The class travels to the scheduler and is matched against what a worker
    # advertises, so it has to name the runtime that was actually resolved. A
    # stub still asking for the runc class would otherwise resolve to a sandbox
    # and then match no worker, sitting pending with nothing naming the cause.
    requested_class = runtime_name.value if runtime_class.strip() else ""
    return runtime_name, requested_class


def validate_checkpoint_request(
    *,
    startup_kind: WorkerStartupKind,
    enabled: bool,
    restoring: bool,
    runtime: OciRuntimeName,
    gpu_count: int,
    readiness_path: str,
    readiness_port: int,
) -> None:
    if not enabled and not restoring:
        return
    if startup_kind not in {
        WorkerStartupKind.Endpoint,
        WorkerStartupKind.Asgi,
        WorkerStartupKind.Function,
        WorkerStartupKind.Pod,
        WorkerStartupKind.PodRun,
        WorkerStartupKind.Sandbox,
    }:
        raise InvalidInputError(
            f"checkpointing is not supported for {startup_kind.value or 'unknown'} workloads"
        )
    if gpu_count > 1:
        raise InvalidInputError("checkpointing does not support more than one GPU")
    if (
        enabled
        and startup_kind in {WorkerStartupKind.Pod, WorkerStartupKind.PodRun}
        and (not readiness_path.startswith("/") or readiness_port <= 0)
    ):
        raise InvalidInputError(
            "Pod checkpointing requires an absolute readiness path and readiness port"
        )


def validate_checkpoint_activation(
    *,
    startup_kind: WorkerStartupKind,
    runtime: StubRuntimeConfig,
) -> None:
    runtime_name, _ = resolve_oci_runtime(
        runtime=runtime.runtime,
        runtime_class=runtime.runtime_class or "",
        docker_enabled=runtime.docker_enabled,
    )
    validate_checkpoint_request(
        startup_kind=startup_kind,
        enabled=runtime.checkpoint_enabled,
        restoring=False,
        runtime=runtime_name,
        gpu_count=runtime.gpu_count,
        readiness_path=runtime.checkpoint_readiness_path,
        readiness_port=runtime.checkpoint_readiness_port,
    )


def _normalize_oci_runtime(value: OciRuntimeName | str) -> OciRuntimeName:
    if isinstance(value, OciRuntimeName):
        return value
    normalized = value.strip().lower()
    if normalized in {OciRuntimeName.Runsc.value, "gvisor", ""}:
        return OciRuntimeName.Runsc
    if normalized == OciRuntimeName.Runc.value:
        # Sandboxed, not refused. Every stub deployed before workloads moved to
        # gVisor carries runc in its persisted config, and rejecting those would
        # strand already-deployed applications until each was redeployed. Asking
        # for less isolation than the platform gives is not an error; it is a
        # request the platform declines to honour downwards.
        return OciRuntimeName.Runsc
    msg = f"unsupported OCI runtime: {value}"
    raise ValueError(msg)


__all__ = [
    "ContainerSchedulingOptions",
    "resolve_oci_runtime",
    "validate_checkpoint_activation",
    "validate_checkpoint_request",
]
