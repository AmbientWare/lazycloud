from __future__ import annotations

import os
import stat
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from subprocess import CompletedProcess
from typing import Protocol

from pydantic import Field, JsonValue, TypeAdapter, ValidationError
from shared.contracts import ContractModel

from worker.events import ContainerRequestContext
from worker.execution import (
    DeviceNode,
    OciDevice,
    OciMount,
    inject_nvidia_environment,
    nvidia_device_paths,
    plan_nvidia_devices,
    plan_nvidia_mounts,
)

NVIDIA_GPU_RESOURCE_NAME = "nvidia.com/gpu"
NVIDIA_VISIBLE_DEVICES_ENV = "NVIDIA_VISIBLE_DEVICES"
NVIDIA_VISIBLE_DEVICES_ALL = "all"
NVIDIA_VISIBLE_DEVICES_VOID = "void"
WORKER_GPU_DEVICES_ENV = "WORKER_GPU_DEVICES"
WORKER_POD_UID_ENV = "WORKER_POD_UID"
WORKER_PERSISTENT_ENV = "WORKER_PERSISTENT"
WORKER_MACHINE_ENV = "WORKER_MACHINE"
DEFAULT_KUBELET_DEVICE_CHECKPOINT_PATH = (
    "/var/lib/kubelet/device-plugins/kubelet_internal_checkpoint"
)
DEFAULT_NVIDIA_CDI_CONFIG_PATHS = (
    "/var/run/runtime/cdi/nvidia.yaml",
    "/var/run/cdi/nvidia.yaml",
    "/etc/cdi/nvidia.yaml",
)

type JsonObject = dict[str, JsonValue]

_JSON_OBJECT: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


class GpuVisibilitySource(StrEnum):
    KubeletCheckpoint = "kubelet-checkpoint"
    WorkerEnv = "worker-env"
    AgentFallback = "agent-fallback"
    NvidiaEnv = "nvidia-env"
    Unavailable = "unavailable"


class NvidiaCdiAttemptStatus(StrEnum):
    Selected = "selected"
    GenerateFailed = "generate-failed"
    ConfigureFailed = "configure-failed"
    Unresolvable = "unresolvable"


class VisibleGpuDevicesPlan(ContractModel):
    value: str = ""
    source: GpuVisibilitySource = GpuVisibilitySource.Unavailable
    device_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class NvidiaSmiDevice(ContractModel):
    pci_domain: str
    pci_bus_id: str
    system_bus_id: str
    index: int
    uuid: str


class GpuMemoryUsageStats(ContractModel):
    used_bytes: int = 0
    total_bytes: int = 0


class NvidiaCdiCandidate(ContractModel):
    path: str
    generate_ok: bool = True
    generate_output: str = ""
    configure_ok: bool = True
    device_resolvable: bool = True


class NvidiaCdiAttempt(ContractModel):
    path: str
    spec_dir: str
    command: tuple[str, ...]
    status: NvidiaCdiAttemptStatus
    error_message: str = ""


class NvidiaCdiGenerationPlan(ContractModel):
    ok: bool
    selected_path: str = ""
    attempts: list[NvidiaCdiAttempt] = Field(default_factory=list)
    error_message: str = ""


class GpuAllocationResult(ContractModel):
    container_id: str
    requested_count: int
    assigned_devices: list[int] = Field(default_factory=list)
    ok: bool = True
    error_message: str = ""


class ContainerGpuAssignmentResult(ContractModel):
    container_id: str
    requested_count: int = 0
    assigned_devices: list[int] = Field(default_factory=list)
    env: list[str] = Field(default_factory=list)
    oci_mounts: list[OciMount] = Field(default_factory=list)
    oci_devices: list[OciDevice] = Field(default_factory=list)
    cdi_devices: list[str] = Field(default_factory=list)
    ok: bool = True
    error_message: str = ""
    reason: str = ""


class DeviceNodeProbe(Protocol):
    def device_node(self, path: str) -> DeviceNode | None: ...


@dataclass(frozen=True, slots=True)
class HostDeviceNodeProbe:
    """Reads a character device's numbers off the host.

    Missing nodes answer None rather than raising: a driver without modeset, or
    without uvm-tools, is a normal install, and a container that does not need
    the node must not be refused because of it.
    """

    def device_node(self, path: str) -> DeviceNode | None:
        try:
            info = os.stat(path)
        except OSError:
            return None
        if not stat.S_ISCHR(info.st_mode):
            return None
        return DeviceNode(
            path=path,
            major=os.major(info.st_rdev),
            minor=os.minor(info.st_rdev),
            file_mode=stat.S_IMODE(info.st_mode),
        )


class GpuDeviceIndexProvider(Protocol):
    def available_devices(self) -> list[int]: ...


class GpuAllocationBackend(Protocol):
    def assign(self, container_id: str, requested_count: int) -> GpuAllocationResult: ...

    def get(self, container_id: str) -> list[int]: ...

    def release_gpu(self, container_id: str) -> None: ...


def parse_kubelet_checkpoint_devices(
    checkpoint: str | bytes | Mapping[str, JsonValue] | None,
    *,
    pod_uid: str,
    resource_name: str = NVIDIA_GPU_RESOURCE_NAME,
) -> list[str]:
    if not checkpoint or not pod_uid:
        return []
    payload = _checkpoint_payload(checkpoint)
    data = payload.get("Data")
    if not isinstance(data, Mapping):
        return []
    entries = data.get("PodDeviceEntries")
    if not isinstance(entries, list):
        return []

    devices: list[str] = []
    for raw_entry in entries:
        if not isinstance(raw_entry, Mapping):
            continue
        if raw_entry.get("PodUID") != pod_uid or raw_entry.get("ResourceName") != resource_name:
            continue
        device_ids = raw_entry.get("DeviceIDs")
        if not isinstance(device_ids, Mapping):
            continue
        for raw_values in device_ids.values():
            if not isinstance(raw_values, list):
                continue
            devices.extend(
                value.strip() for value in raw_values if isinstance(value, str) and value.strip()
            )
    return devices


def resolve_visible_gpu_devices(
    env: Mapping[str, str],
    *,
    kubelet_checkpoint: str | bytes | Mapping[str, JsonValue] | None = None,
) -> VisibleGpuDevicesPlan:
    pod_uid = env.get(WORKER_POD_UID_ENV, "").strip()
    checkpoint_devices = parse_kubelet_checkpoint_devices(kubelet_checkpoint, pod_uid=pod_uid)
    if checkpoint_devices:
        value = ",".join(checkpoint_devices)
        return VisibleGpuDevicesPlan(
            value=value,
            source=GpuVisibilitySource.KubeletCheckpoint,
            device_ids=checkpoint_devices,
            reason="kubelet device-plugin checkpoint assigned GPUs to this pod",
        )

    worker_devices = env.get(WORKER_GPU_DEVICES_ENV, "").strip()
    if worker_devices:
        return VisibleGpuDevicesPlan(
            value=worker_devices,
            source=GpuVisibilitySource.WorkerEnv,
            device_ids=split_visible_devices(worker_devices),
            reason="worker GPU assignment env is set",
        )

    nvidia_devices = env.get(NVIDIA_VISIBLE_DEVICES_ENV, "").strip()
    if nvidia_devices == NVIDIA_VISIBLE_DEVICES_VOID and agent_managed_worker(env):
        return VisibleGpuDevicesPlan(
            value=NVIDIA_VISIBLE_DEVICES_ALL,
            source=GpuVisibilitySource.AgentFallback,
            reason="agent-managed worker has toolkit void marker on a dedicated machine",
        )
    if nvidia_devices:
        return VisibleGpuDevicesPlan(
            value=nvidia_devices,
            source=GpuVisibilitySource.NvidiaEnv,
            device_ids=split_visible_devices(nvidia_devices),
            reason="NVIDIA visible-devices env is set",
        )
    return VisibleGpuDevicesPlan(reason="no visible GPU assignment is available")


def split_visible_devices(value: str) -> list[str]:
    if value in {NVIDIA_VISIBLE_DEVICES_ALL, NVIDIA_VISIBLE_DEVICES_VOID}:
        return []
    return [token.strip() for token in value.split(",") if token.strip()]


def agent_managed_worker(env: Mapping[str, str]) -> bool:
    return _parse_bool(env.get(WORKER_PERSISTENT_ENV, "")) and bool(
        env.get(WORKER_MACHINE_ENV, "").strip()
    )


def device_visible(visible_devices: str, uuid: str, index: int | str) -> bool:
    if visible_devices == NVIDIA_VISIBLE_DEVICES_ALL:
        return True
    index_text = str(index).strip()
    return any(token in (uuid, index_text) for token in split_visible_devices(visible_devices))


def parse_nvidia_smi_devices(
    output: str,
    *,
    visible_devices: str = NVIDIA_VISIBLE_DEVICES_ALL,
    existing_system_bus_ids: set[str] | None = None,
) -> list[NvidiaSmiDevice]:
    devices: list[NvidiaSmiDevice] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 4:
            msg = f"unexpected nvidia-smi device output: {line}"
            raise ValueError(msg)

        pci_domain = hex_to_padded_pci_domain(parts[0])
        bus_parts = parts[1].split(":")
        if len(bus_parts) != 3:
            msg = f"unexpected nvidia-smi bus id: {line}"
            raise ValueError(msg)
        index = int(parts[2])
        uuid = parts[3]
        if not device_visible(visible_devices, uuid, index):
            continue

        system_bus_id = f"{pci_domain}:{bus_parts[1]}:{bus_parts[2]}".lower()
        if existing_system_bus_ids is not None and system_bus_id not in existing_system_bus_ids:
            continue
        devices.append(
            NvidiaSmiDevice(
                pci_domain=pci_domain,
                pci_bus_id=parts[1],
                system_bus_id=system_bus_id,
                index=index,
                uuid=uuid,
            )
        )
    return devices


def available_gpu_indices(
    output: str,
    *,
    visible_devices: str = NVIDIA_VISIBLE_DEVICES_ALL,
    existing_system_bus_ids: set[str] | None = None,
) -> list[int]:
    return [
        device.index
        for device in parse_nvidia_smi_devices(
            output,
            visible_devices=visible_devices,
            existing_system_bus_ids=existing_system_bus_ids,
        )
    ]


def parse_gpu_memory_usage(output: str) -> GpuMemoryUsageStats:
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != 2:
            msg = "unable to parse GPU memory info"
            raise ValueError(msg)
        total_mib = int(fields[0])
        used_mib = int(fields[1])
        return GpuMemoryUsageStats(
            total_bytes=total_mib * 1024 * 1024,
            used_bytes=used_mib * 1024 * 1024,
        )
    return GpuMemoryUsageStats()


def hex_to_padded_pci_domain(value: str) -> str:
    return f"{int(value.removeprefix('0x').removeprefix('0X'), 16):04x}"


def plan_nvidia_cdi_generation(
    candidates: Sequence[NvidiaCdiCandidate] | None = None,
) -> NvidiaCdiGenerationPlan:
    candidates = candidates or [
        NvidiaCdiCandidate(path=path) for path in DEFAULT_NVIDIA_CDI_CONFIG_PATHS
    ]
    attempts: list[NvidiaCdiAttempt] = []
    for candidate in candidates:
        command = ("nvidia-ctk", "cdi", "generate", "--output", candidate.path)
        spec_dir = str(PurePosixPath(candidate.path).parent)
        if not candidate.generate_ok:
            attempts.append(
                NvidiaCdiAttempt(
                    path=candidate.path,
                    spec_dir=spec_dir,
                    command=command,
                    status=NvidiaCdiAttemptStatus.GenerateFailed,
                    error_message=candidate.generate_output or "nvidia-ctk generation failed",
                )
            )
            continue
        if not candidate.configure_ok:
            attempts.append(
                NvidiaCdiAttempt(
                    path=candidate.path,
                    spec_dir=spec_dir,
                    command=command,
                    status=NvidiaCdiAttemptStatus.ConfigureFailed,
                    error_message="CDI cache configuration failed",
                )
            )
            continue
        if not candidate.device_resolvable:
            attempts.append(
                NvidiaCdiAttempt(
                    path=candidate.path,
                    spec_dir=spec_dir,
                    command=command,
                    status=NvidiaCdiAttemptStatus.Unresolvable,
                    error_message="generated NVIDIA CDI device is not resolvable",
                )
            )
            continue
        attempts.append(
            NvidiaCdiAttempt(
                path=candidate.path,
                spec_dir=spec_dir,
                command=command,
                status=NvidiaCdiAttemptStatus.Selected,
            )
        )
        return NvidiaCdiGenerationPlan(ok=True, selected_path=candidate.path, attempts=attempts)

    error_message = "; ".join(
        f"{attempt.path}: {attempt.error_message}" for attempt in attempts if attempt.error_message
    )
    return NvidiaCdiGenerationPlan(
        ok=False,
        attempts=attempts,
        error_message=error_message or "no NVIDIA CDI config path is available",
    )


class GpuAllocationManager:
    def __init__(self, available_devices: Sequence[int]) -> None:
        self._available_devices = sorted(dict.fromkeys(available_devices))
        self._allocations: dict[str, list[int]] = {}
        self._lock = threading.Lock()

    def assign(self, container_id: str, requested_count: int) -> GpuAllocationResult:
        if requested_count < 0:
            msg = "requested GPU count cannot be negative"
            raise ValueError(msg)
        if not container_id:
            msg = "container_id is required"
            raise ValueError(msg)
        with self._lock:
            allocated = {device for devices in self._allocations.values() for device in devices}
            allocable = [device for device in self._available_devices if device not in allocated]
            if len(allocable) < requested_count:
                return GpuAllocationResult(
                    container_id=container_id,
                    requested_count=requested_count,
                    ok=False,
                    error_message=(
                        "not enough GPUs available: "
                        f"requested={requested_count}, allocable={len(allocable)}, "
                        f"visible={len(self._available_devices)}, "
                        f"already_allocated={len(allocated)}"
                    ),
                )
            assigned = allocable[:requested_count]
            self._allocations[container_id] = assigned
            return GpuAllocationResult(
                container_id=container_id,
                requested_count=requested_count,
                assigned_devices=assigned,
            )

    def get(self, container_id: str) -> list[int]:
        with self._lock:
            return list(self._allocations.get(container_id, []))

    def unassign(self, container_id: str) -> None:
        with self._lock:
            self._allocations.pop(container_id, None)

    def release_gpu(self, container_id: str) -> None:
        self.unassign(container_id)

    def snapshot(self) -> dict[str, list[int]]:
        with self._lock:
            return {
                container_id: list(devices) for container_id, devices in self._allocations.items()
            }


@dataclass(slots=True)
class NvidiaGpuIndexProvider:
    visible_devices: str = NVIDIA_VISIBLE_DEVICES_ALL
    existing_system_bus_ids: set[str] | None = None
    command_runner: Callable[..., CompletedProcess[str]] = subprocess.run

    def available_devices(self) -> list[int]:
        result = self.command_runner(
            [
                "nvidia-smi",
                "--query-gpu=pci.domain,pci.bus_id,index,uuid",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if not isinstance(result, CompletedProcess) or result.returncode != 0:
            return []
        return available_gpu_indices(
            result.stdout,
            visible_devices=self.visible_devices or NVIDIA_VISIBLE_DEVICES_ALL,
            existing_system_bus_ids=self.existing_system_bus_ids,
        )


class DynamicGpuAllocationManager:
    def __init__(self, provider: GpuDeviceIndexProvider) -> None:
        self._provider = provider
        self._allocations: dict[str, list[int]] = {}
        self._lock = threading.Lock()

    def assign(self, container_id: str, requested_count: int) -> GpuAllocationResult:
        if requested_count < 0:
            msg = "requested GPU count cannot be negative"
            raise ValueError(msg)
        if not container_id:
            msg = "container_id is required"
            raise ValueError(msg)
        with self._lock:
            available_devices = sorted(dict.fromkeys(self._provider.available_devices()))
            allocated = {device for devices in self._allocations.values() for device in devices}
            allocable = [device for device in available_devices if device not in allocated]
            if len(allocable) < requested_count:
                return GpuAllocationResult(
                    container_id=container_id,
                    requested_count=requested_count,
                    ok=False,
                    error_message=(
                        "not enough GPUs available: "
                        f"requested={requested_count}, allocable={len(allocable)}, "
                        f"visible={len(available_devices)}, "
                        f"already_allocated={len(allocated)}"
                    ),
                )
            assigned = allocable[:requested_count]
            self._allocations[container_id] = assigned
            return GpuAllocationResult(
                container_id=container_id,
                requested_count=requested_count,
                assigned_devices=assigned,
            )

    def get(self, container_id: str) -> list[int]:
        with self._lock:
            return list(self._allocations.get(container_id, []))

    def release_gpu(self, container_id: str) -> None:
        with self._lock:
            self._allocations.pop(container_id, None)


@dataclass(slots=True)
class WorkerGpuRuntimeAssigner:
    allocation: GpuAllocationBackend
    cdi_enabled: bool = True
    host_paths: set[str] = field(default_factory=set)
    device_probe: DeviceNodeProbe = field(default_factory=HostDeviceNodeProbe)

    def assign_gpus(self, request: ContainerRequestContext) -> ContainerGpuAssignmentResult:
        if request.gpu_count <= 0:
            return ContainerGpuAssignmentResult(
                container_id=request.container_id,
                requested_count=0,
                reason="no gpu requested",
            )
        result = self.allocation.assign(request.container_id, request.gpu_count)
        if not result.ok:
            return ContainerGpuAssignmentResult(
                container_id=request.container_id,
                requested_count=request.gpu_count,
                ok=False,
                error_message=result.error_message,
                reason="gpu assignment failed",
            )
        env_plan = inject_nvidia_environment(request.env)
        assigned = list(result.assigned_devices)
        return ContainerGpuAssignmentResult(
            container_id=request.container_id,
            requested_count=request.gpu_count,
            assigned_devices=assigned,
            env=env_plan.env,
            oci_mounts=plan_nvidia_mounts(self.host_paths),
            oci_devices=plan_nvidia_devices(
                [
                    node
                    for node in (
                        self.device_probe.device_node(path)
                        for path in nvidia_device_paths(assigned)
                    )
                    if node is not None
                ]
            ),
            cdi_devices=[
                f"{NVIDIA_GPU_RESOURCE_NAME}={device}" for device in assigned if self.cdi_enabled
            ],
            reason="gpu assignment ready",
        )

    def get(self, container_id: str) -> list[int]:
        return self.allocation.get(container_id)

    def release_gpu(self, container_id: str) -> None:
        self.allocation.release_gpu(container_id)


def _checkpoint_payload(checkpoint: str | bytes | Mapping[str, JsonValue]) -> JsonObject:
    source: str | bytes | Mapping[str, JsonValue] = checkpoint
    if isinstance(checkpoint, Mapping):
        source = dict(checkpoint)
    try:
        if isinstance(source, str | bytes):
            return _JSON_OBJECT.validate_json(source)
        return _JSON_OBJECT.validate_python(source)
    except ValidationError:
        return {}


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "t", "yes", "y", "on"}
