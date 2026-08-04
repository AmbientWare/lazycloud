from __future__ import annotations

from collections.abc import Mapping

from foundation.resources import parse_memory_mib
from pydantic import BaseModel, ConfigDict, Field, field_validator
from shared.app_identity import TASK_QUEUE_IMAGE
from shared.deployment_records import DEFAULT_DISK, DEFAULT_MAX_PENDING_TASKS
from shared.lifecycle import LifecycleHooks
from shared.tasks import RetryPolicy

from execution.config import (
    ExecutionPythonVersion,
    ExecutionPythonVersionInput,
    ManagedPythonExecutable,
    VolumeConfig,
    VolumeMountInput,
    managed_python_executable,
)
from execution.taskqueues.planning import DEFAULT_TASK_QUEUE_TASK_TTL_SECONDS


class TaskQueueImageConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    image_id: str | None = None
    python_version: ExecutionPythonVersionInput = ExecutionPythonVersion.Python312

    @property
    def python_executable(self) -> ManagedPythonExecutable:
        return managed_python_executable(self.python_version)


class TaskQueueTaskPolicy(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    ttl: int = Field(default=0, ge=0)
    ttl_seconds: int = Field(default=0, ge=0)

    @property
    def effective_ttl_seconds(self) -> int:
        return self.ttl or self.ttl_seconds or DEFAULT_TASK_QUEUE_TASK_TTL_SECONDS


class TaskQueueRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    cpu: int | float | None = Field(default=None, ge=0)
    cpu_millicores: int = Field(default=0, ge=0)
    memory: str | int | None = None
    memory_mib: int = Field(default=0, ge=0)
    disk: str | int = DEFAULT_DISK
    gpu: str | None = None
    gpu_type: str | None = None
    gpu_count: int = Field(default=0, ge=0)
    image_id: str | None = None
    keep_warm: int = Field(default=0, ge=0)
    concurrency: int = Field(default=1, ge=1)
    pool_selector: str | None = None
    runtime: str = "runc"
    runtime_class: str | None = None
    docker_enabled: bool = False
    preemptible: bool = False
    gpu_limit: int = Field(default=0, ge=0)
    cpu_limit_millicores: int = Field(default=0, ge=0)
    checkpoint_enabled: bool = False

    @field_validator("disk", mode="before")
    @classmethod
    def disk_defaults_to_the_platform_ceiling(cls, value: object) -> object:
        # Every container has a ceiling, so an absent value is the default
        # rather than 'unlimited'.
        if value is None or value == "":
            return DEFAULT_DISK
        return value

    @field_validator("memory")
    @classmethod
    def memory_must_be_valid(cls, value: str | int | None) -> str | int | None:
        parsed = parse_memory_mib(value)
        if parsed is not None and parsed < 0:
            msg = "memory must be non-negative"
            raise ValueError(msg)
        return value

    @property
    def requested_cpu_millicores(self) -> int:
        if self.cpu_millicores:
            return self.cpu_millicores
        return int(float(self.cpu) * 1000) if self.cpu is not None else 0

    @property
    def requested_memory_mib(self) -> int:
        if self.memory_mib:
            return self.memory_mib
        return parse_memory_mib(self.memory) or 0

    @property
    def requested_disk_mib(self) -> int:
        # Reuses the memory parser: the units are the same and disk accepts the
        # same "10Gi" strings users already write for memory.
        return parse_memory_mib(self.disk) or 0

    @property
    def requested_gpu_type(self) -> str:
        return self.gpu or self.gpu_type or ""


def _is_empty_mapping(value: object) -> bool:
    return isinstance(value, Mapping) and value == {}


class TaskQueueStubConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    object_id: str = ""
    image: TaskQueueImageConfig = Field(default_factory=TaskQueueImageConfig)
    runtime: TaskQueueRuntimeConfig = Field(default_factory=TaskQueueRuntimeConfig)
    task_policy: TaskQueueTaskPolicy = Field(default_factory=TaskQueueTaskPolicy)
    env: dict[str, str] = Field(default_factory=dict)
    secrets: list[str] = Field(default_factory=list)
    volumes: list[VolumeConfig] = Field(default_factory=list)
    retry_policy: RetryPolicy | None = None
    lifecycle_hooks: LifecycleHooks = Field(default_factory=LifecycleHooks)
    max_pending_tasks: int | None = Field(default=None, ge=0)

    @field_validator("retry_policy", mode="before")
    @classmethod
    def empty_retry_policy_uses_defaults(cls, value: object) -> object:
        if _is_empty_mapping(value):
            return None
        return value

    @property
    def effective_image_id(self) -> str:
        return self.image.image_id or self.runtime.image_id or TASK_QUEUE_IMAGE

    @property
    def effective_max_pending_tasks(self) -> int:
        if self.max_pending_tasks is None:
            return DEFAULT_MAX_PENDING_TASKS
        return self.max_pending_tasks

    @property
    def effective_retry_policy(self) -> RetryPolicy:
        return self.retry_policy or RetryPolicy.from_retries(3)

    @property
    def effective_keep_warm_seconds(self) -> int:
        return self.runtime.keep_warm

    @property
    def consumers_per_container(self) -> int:
        return self.runtime.concurrency

    @property
    def volume_inputs(self) -> list[VolumeMountInput]:
        return [volume.mount_input() for volume in self.volumes]


__all__ = ["TaskQueueStubConfig"]
