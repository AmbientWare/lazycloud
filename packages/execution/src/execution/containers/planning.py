from __future__ import annotations

from datetime import datetime
from pathlib import Path

from shared.compute_policy import ComputePlacementTarget
from shared.container_requests import (
    DEFAULT_WORKSPACE_STORAGE_BASE_MOUNT_PATH,
    OciRuntimeName,
    RequestMount,
    WorkerStartupKind,
)
from shared.contracts import ContractModel
from shared.errors import InvalidInputError
from shared.workload_config import StubRuntimeConfig


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
    memory_mib: int = 0
    gpu_type: str = ""
    gpu_request: list[str] | None = None
    gpu_count: int = 0
    pool_selector: str = ""
    requested_placement: ComputePlacementTarget | None = None
    runtime: OciRuntimeName | str = OciRuntimeName.Runc
    runtime_class: str = ""
    docker_enabled: bool = False
    block_network: bool = False
    allow_list: list[str] | None = None
    preemptible: bool = False
    gpu_limit: int = 0
    cpu_limit_millicores: int = 0
    cost_per_ms: float = 0.0
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
    requested_class = runtime_class.strip()
    runtime_name = _normalize_oci_runtime(requested_class or runtime)
    if docker_enabled and not requested_class:
        runtime_name = OciRuntimeName.Runsc
        requested_class = runtime_name.value
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
        WorkerStartupKind.TaskQueue,
        WorkerStartupKind.Pod,
        WorkerStartupKind.PodRun,
        WorkerStartupKind.Sandbox,
    }:
        raise InvalidInputError(
            f"checkpointing is not supported for {startup_kind.value or 'unknown'} workloads"
        )
    if runtime is not OciRuntimeName.Runc:
        raise InvalidInputError("checkpointing requires the runc runtime")
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
    if normalized in {OciRuntimeName.Runsc.value, "gvisor"}:
        return OciRuntimeName.Runsc
    if normalized in {OciRuntimeName.Runc.value, ""}:
        return OciRuntimeName.Runc
    msg = f"unsupported OCI runtime: {value}"
    raise ValueError(msg)


__all__ = [
    "ContainerSchedulingOptions",
    "resolve_oci_runtime",
    "validate_checkpoint_activation",
    "validate_checkpoint_request",
]
