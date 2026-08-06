from __future__ import annotations

from pathlib import Path
from typing import Self

import yaml
from pydantic import Field, field_validator, model_validator
from shared.app_identity import ENV_PREFIX, NAME, WORKER_CHECKPOINT_ROOT
from shared.contracts import ContractModel
from shared.routing import BackendRouteTransport

from worker.container_rootfs import DEFAULT_CONTAINER_ROOTFS_ROOT
from worker.events import WorkerPoolMode
from worker.image_build_scratch import (
    DEFAULT_IMAGE_BUILD_MIN_FREE_BYTES,
    DEFAULT_IMAGE_BUILD_PER_BUILD_MAX_BYTES,
    DEFAULT_IMAGE_BUILD_ROOT,
    DEFAULT_IMAGE_BUILD_SCRATCH_MAX_BYTES,
    DEFAULT_IMAGE_BUILD_STALE_SECONDS,
)
from worker.image_lifecycle import DEFAULT_IMAGE_CACHE_PATH
from worker.oci_runtime import DEFAULT_WORKER_BUNDLE_ROOT, DEFAULT_WORKER_IMAGE_MOUNT_ROOT
from worker.runtime_config import OciRuntimeName
from worker.source_code import (
    DEFAULT_SOURCE_CACHE_MAX_BYTES,
    DEFAULT_SOURCE_CACHE_MAX_ENTRIES,
)

# The prefix is load-bearing: the suite clears LAZYCLOUD_-prefixed variables, so
# an unprefixed name would let a file left at the default path on one developer's
# machine decide what the tests observe.
WORKER_CONFIG_PATH_ENV = f"{ENV_PREFIX}_WORKER_CONFIG_PATH"
DEFAULT_WORKER_CONFIG_PATH = f"/etc/{NAME}/worker/worker.yaml"
WORKER_CONFIGURATION_SECTION = "configuration"


class WorkerCapacityConfiguration(ContractModel):
    cpu_millicores: int = 0
    memory_mib: int = 0
    gpu_type: str = ""
    gpu_count: int = 0

    @field_validator("cpu_millicores", "memory_mib", "gpu_count")
    @classmethod
    def values_cannot_be_negative(cls, value: int) -> int:
        if value < 0:
            msg = "worker capacity values cannot be negative"
            raise ValueError(msg)
        return value


class WorkerExecutionConfiguration(ContractModel):
    runtime: OciRuntimeName = OciRuntimeName.Runsc
    runtimes: list[OciRuntimeName] = Field(default_factory=lambda: [OciRuntimeName.Runsc])
    capacity: WorkerCapacityConfiguration = Field(default_factory=WorkerCapacityConfiguration)
    pool_mode: WorkerPoolMode = WorkerPoolMode.Public
    requires_pool_selector: bool = False
    preemptible: bool = False
    persistent: bool = False
    agent_worker: bool = True

    @model_validator(mode="after")
    def default_runtime_must_be_a_candidate(self) -> Self:
        if len(set(self.runtimes)) != len(self.runtimes):
            raise ValueError("worker runtime candidates must be unique")
        if self.runtime not in self.runtimes:
            raise ValueError("default worker runtime must be included in runtime candidates")
        return self


class WorkerNetworkConfiguration(ContractModel):
    route_transport: BackendRouteTransport = BackendRouteTransport.TsnetRestricted
    agent_bridge_network: bool = False


class WorkerPathConfiguration(ContractModel):
    bundle_root: Path = Path(DEFAULT_WORKER_BUNDLE_ROOT)
    image_cache_path: str = DEFAULT_IMAGE_CACHE_PATH
    image_mount_root: str = DEFAULT_WORKER_IMAGE_MOUNT_ROOT
    image_build_root: Path = DEFAULT_IMAGE_BUILD_ROOT
    cache_root: Path | None = None
    source_cache_root: Path | None = None
    checkpoint_root: str = WORKER_CHECKPOINT_ROOT
    container_rootfs_root: Path = Path(DEFAULT_CONTAINER_ROOTFS_ROOT)

    @model_validator(mode="after")
    def image_build_root_is_dedicated_disk_storage(self) -> Self:
        build_root = self.image_build_root.expanduser().resolve()
        shared_memory_root = Path("/dev/shm")
        if build_root == shared_memory_root or shared_memory_root in build_root.parents:
            raise ValueError("image build root cannot use /dev/shm")
        managed_roots = {
            Path(self.image_cache_path).expanduser().resolve(),
            Path(self.image_mount_root).expanduser().resolve(),
            Path(self.checkpoint_root).expanduser().resolve(),
            self.container_rootfs_root.expanduser().resolve(),
        }
        if self.source_cache_root is not None:
            managed_roots.add(self.source_cache_root.expanduser().resolve())
        if self.cache_root is not None:
            managed_roots.add(self.cache_root.expanduser().resolve())
        if any(
            build_root == managed_root
            or managed_root in build_root.parents
            or build_root in managed_root.parents
            for managed_root in managed_roots
        ):
            raise ValueError("image build root cannot overlap worker-managed cache roots")
        return self


class WorkerMonitoringConfiguration(ContractModel):
    metrics_enabled: bool = True
    metrics_interval_seconds: float = Field(default=5.0, gt=0)


class WorkerSourceCacheConfiguration(ContractModel):
    max_bytes: int = Field(default=DEFAULT_SOURCE_CACHE_MAX_BYTES, gt=0)
    max_entries: int = Field(default=DEFAULT_SOURCE_CACHE_MAX_ENTRIES, gt=0)


class WorkerImageBuildConfiguration(ContractModel):
    scratch_max_bytes: int = Field(default=DEFAULT_IMAGE_BUILD_SCRATCH_MAX_BYTES, gt=0)
    per_build_max_bytes: int = Field(default=DEFAULT_IMAGE_BUILD_PER_BUILD_MAX_BYTES, gt=0)
    minimum_free_bytes: int = Field(default=DEFAULT_IMAGE_BUILD_MIN_FREE_BYTES, ge=0)
    stale_seconds: int = Field(default=DEFAULT_IMAGE_BUILD_STALE_SECONDS, ge=0)

    @model_validator(mode="after")
    def per_build_limit_must_fit_aggregate(self) -> Self:
        if self.per_build_max_bytes > self.scratch_max_bytes:
            raise ValueError("per-build scratch limit cannot exceed aggregate scratch limit")
        return self


class WorkerConfiguration(ContractModel):
    execution: WorkerExecutionConfiguration = Field(default_factory=WorkerExecutionConfiguration)
    network: WorkerNetworkConfiguration = Field(default_factory=WorkerNetworkConfiguration)
    paths: WorkerPathConfiguration = Field(default_factory=WorkerPathConfiguration)
    monitoring: WorkerMonitoringConfiguration = Field(default_factory=WorkerMonitoringConfiguration)
    source_cache: WorkerSourceCacheConfiguration = Field(
        default_factory=WorkerSourceCacheConfiguration
    )
    image_build: WorkerImageBuildConfiguration = Field(
        default_factory=WorkerImageBuildConfiguration
    )


def serialize_worker_configuration(config: WorkerConfiguration) -> str:
    payload = {
        WORKER_CONFIGURATION_SECTION: config.model_dump(mode="json"),
    }
    return yaml.safe_dump(payload, sort_keys=False)


__all__ = [
    "DEFAULT_WORKER_CONFIG_PATH",
    "WORKER_CONFIGURATION_SECTION",
    "WORKER_CONFIG_PATH_ENV",
    "WorkerCapacityConfiguration",
    "WorkerConfiguration",
    "WorkerExecutionConfiguration",
    "WorkerImageBuildConfiguration",
    "WorkerMonitoringConfiguration",
    "WorkerNetworkConfiguration",
    "WorkerPathConfiguration",
    "WorkerSourceCacheConfiguration",
    "serialize_worker_configuration",
]
