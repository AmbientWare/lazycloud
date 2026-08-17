from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from shared.deployment_records import (
    DEFAULT_FUNCTION_KEEP_WARM_SECONDS,
    DEFAULT_MAX_PENDING_TASKS,
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


class FunctionImageConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    image_id: str | None = None
    python_version: ExecutionPythonVersionInput = ExecutionPythonVersion.Python312

    @property
    def python_executable(self) -> ManagedPythonExecutable:
        return managed_python_executable(self.python_version)


class FunctionRuntimeConfig(ContainerResourceConfig):
    requires_gpu: bool = False
    task_ttl_seconds: int = Field(default=0, ge=0)
    retries: int = Field(default=0, ge=0)
    checkpoint_enabled: bool = False
    # Idle seconds a container stays available for the next call. `-1` never
    # scales to zero; `0` is the old behaviour, one container per invocation.
    keep_warm: int = Field(default=DEFAULT_FUNCTION_KEEP_WARM_SECONDS, ge=-1)
    # How many invocations one container serves at once, each in its own process.
    concurrency: int = Field(default=1, gt=0)

    @property
    def gpu_required(self) -> bool:
        return self.requires_gpu or bool(self.requested_gpu_type) or self.gpu_count > 0


class FunctionStubConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    object_id: str = ""
    max_pending_tasks: int | None = Field(default=None, ge=0)
    image: FunctionImageConfig = Field(default_factory=FunctionImageConfig)
    runtime: FunctionRuntimeConfig = Field(default_factory=FunctionRuntimeConfig)
    env: dict[str, str] = Field(default_factory=dict)
    secrets: list[str] = Field(default_factory=list)
    volumes: list[VolumeConfig] = Field(default_factory=list)
    retry_policy: RetryPolicy | None = None
    lifecycle_hooks: LifecycleHooks = Field(default_factory=LifecycleHooks)

    @property
    def effective_max_pending_tasks(self) -> int:
        if self.max_pending_tasks is None:
            return DEFAULT_MAX_PENDING_TASKS
        return self.max_pending_tasks

    @property
    def effective_image_id(self) -> str:
        return self.image.image_id or self.runtime.image_id or ""

    @property
    def effective_retry_policy(self) -> RetryPolicy:
        return self.retry_policy or RetryPolicy.from_retries(self.runtime.retries)

    @property
    def env_list(self) -> list[str]:
        return [f"{name}={value}" for name, value in self.env.items()]

    @property
    def volume_inputs(self) -> list[VolumeMountInput]:
        return [volume.mount_input() for volume in self.volumes]


__all__ = ["FunctionStubConfig"]
