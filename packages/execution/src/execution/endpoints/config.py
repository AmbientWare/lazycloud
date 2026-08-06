from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from shared.compute_policy import MachinePool
from shared.deployment_records import (
    DEFAULT_MAX_PENDING_TASKS,
    resolve_http_wait_timeout_seconds,
)
from shared.lifecycle import LifecycleHooks
from shared.tasks import RetryPolicy

from execution.config import (
    ContainerResourceConfig,
    ExecutionPythonVersion,
    ExecutionPythonVersionInput,
    ManagedPythonExecutable,
    VolumeConfig,
    VolumeMountInput,
    managed_python_executable,
)


class EndpointImageConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    image_id: str | None = None
    python_version: ExecutionPythonVersionInput = ExecutionPythonVersion.Python312

    @property
    def python_executable(self) -> ManagedPythonExecutable:
        return managed_python_executable(self.python_version)


class EndpointTaskPolicy(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    timeout: int | float = Field(default=0, ge=0)
    timeout_seconds: int | float | None = Field(default=None, ge=0)

    @property
    def effective_timeout_seconds(self) -> float:
        return float(self.timeout or self.timeout_seconds or 0)


class EndpointRuntimeConfig(ContainerResourceConfig):
    timeout_seconds: int | float | None = Field(default=None, ge=0)
    concurrency: int = Field(default=0, ge=0)
    workers: int = Field(default=0, ge=0)
    checkpoint_enabled: bool = False


class EndpointMetadataConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    workers: int = Field(default=1, ge=1)


class EndpointStubConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    object_id: str = ""
    image: EndpointImageConfig = Field(default_factory=EndpointImageConfig)
    runtime: EndpointRuntimeConfig = Field(default_factory=EndpointRuntimeConfig)
    task_policy: EndpointTaskPolicy = Field(default_factory=EndpointTaskPolicy)
    env: dict[str, str] = Field(default_factory=dict)
    secrets: list[str] = Field(default_factory=list)
    volumes: list[VolumeConfig] = Field(default_factory=list)
    retry_policy: RetryPolicy | None = None
    lifecycle_hooks: LifecycleHooks = Field(default_factory=LifecycleHooks)
    max_pending_tasks: int | None = Field(default=None, ge=0)
    pool: MachinePool = MachinePool("")
    metadata: EndpointMetadataConfig = Field(default_factory=EndpointMetadataConfig)

    @property
    def effective_image_id(self) -> str:
        return self.image.image_id or self.runtime.image_id or ""

    @property
    def effective_pool_selector(self) -> str:
        return self.runtime.pool_selector or self.pool

    @property
    def effective_retry_policy(self) -> RetryPolicy:
        return self.retry_policy or RetryPolicy()

    @property
    def effective_max_pending_tasks(self) -> int:
        if self.max_pending_tasks is None:
            return DEFAULT_MAX_PENDING_TASKS
        return self.max_pending_tasks

    @property
    def workers(self) -> int:
        return max(self.runtime.workers, self.metadata.workers, 1)

    @property
    def requests_per_worker(self) -> int:
        return max(self.runtime.concurrency, 1)

    @property
    def container_concurrency(self) -> int:
        return self.workers * self.requests_per_worker

    @property
    def wait_timeout_seconds(self) -> float:
        return resolve_http_wait_timeout_seconds(
            self.task_policy.effective_timeout_seconds or self.runtime.timeout_seconds
        )

    @property
    def volume_inputs(self) -> list[VolumeMountInput]:
        return [volume.mount_input() for volume in self.volumes]


__all__ = ["EndpointStubConfig"]
