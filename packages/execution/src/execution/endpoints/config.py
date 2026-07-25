from __future__ import annotations

from foundation.resources import parse_memory_mib
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator
from shared.deployment_records import DEFAULT_MAX_PENDING_TASKS, resolve_http_wait_timeout_seconds
from shared.lifecycle import LifecycleHooks
from shared.mounts import MountAuthMode, validate_mount_auth
from shared.tasks import RetryPolicy

from execution.config import (
    ExecutionPythonVersion,
    ExecutionPythonVersionInput,
    ManagedPythonExecutable,
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


class EndpointRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    cpu: int | float | None = Field(default=None, ge=0)
    cpu_millicores: int = Field(default=0, ge=0)
    memory: str | int | None = None
    memory_mib: int = Field(default=0, ge=0)
    gpu: str | None = None
    gpu_type: str | None = None
    gpu_count: int = Field(default=0, ge=0)
    image_id: str | None = None
    timeout_seconds: int | float | None = Field(default=None, ge=0)
    concurrency: int = Field(default=0, ge=0)
    workers: int = Field(default=0, ge=0)
    pool_selector: str | None = None
    runtime: str = "runc"
    runtime_class: str | None = None
    docker_enabled: bool = False
    preemptible: bool = False
    gpu_limit: int = Field(default=0, ge=0)
    cpu_limit_millicores: int = Field(default=0, ge=0)
    checkpoint_enabled: bool = False

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
    def requested_gpu_type(self) -> str:
        return self.gpu or self.gpu_type or ""


class EndpointPoolConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    name: str = ""


class EndpointMetadataConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    workers: int = Field(default=1, ge=1)


class EndpointVolumeProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    read_only: bool = False
    bucket_name: str = ""
    prefix: str = ""
    auth_mode: MountAuthMode = MountAuthMode.Ambient
    access_key: str = ""
    secret_key: str = ""
    endpoint_url: str = ""
    region: str = ""
    force_path_style: bool = False

    @model_validator(mode="after")
    def credentials_match_auth_mode(self) -> EndpointVolumeProviderConfig:
        validate_mount_auth(self.auth_mode, self.access_key, self.secret_key)
        return self

    def mount_values(self, *, read_only: bool) -> dict[str, str | bool | MountAuthMode]:
        return {
            "read_only": read_only or self.read_only,
            "bucket_name": self.bucket_name,
            "prefix": self.prefix,
            "auth_mode": self.auth_mode,
            "access_key": self.access_key,
            "secret_key": self.secret_key,
            "endpoint_url": self.endpoint_url,
            "region": self.region,
            "force_path_style": self.force_path_style,
        }


class EndpointVolumeConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    id: str = ""
    name: str = ""
    mount_path: str = ""
    read_only: bool = False
    config: EndpointVolumeProviderConfig | None = None

    def mount_input(self) -> dict[str, JsonValue]:
        provider = self.config or EndpointVolumeProviderConfig()
        return {
            "id": self.name or self.id,
            "mount_path": self.mount_path,
            "config": provider.mount_values(read_only=self.read_only),
        }


class EndpointStubConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    object_id: str = ""
    image: EndpointImageConfig = Field(default_factory=EndpointImageConfig)
    runtime: EndpointRuntimeConfig = Field(default_factory=EndpointRuntimeConfig)
    task_policy: EndpointTaskPolicy = Field(default_factory=EndpointTaskPolicy)
    env: dict[str, str] = Field(default_factory=dict)
    secrets: list[str] = Field(default_factory=list)
    volumes: list[EndpointVolumeConfig] = Field(default_factory=list)
    retry_policy: RetryPolicy | None = None
    lifecycle_hooks: LifecycleHooks = Field(default_factory=LifecycleHooks)
    max_pending_tasks: int | None = Field(default=None, ge=0)
    pool: EndpointPoolConfig = Field(default_factory=EndpointPoolConfig)
    metadata: EndpointMetadataConfig = Field(default_factory=EndpointMetadataConfig)

    @property
    def effective_image_id(self) -> str:
        return self.image.image_id or self.runtime.image_id or ""

    @property
    def effective_pool_selector(self) -> str:
        return self.runtime.pool_selector or self.pool.name

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
    def volume_inputs(self) -> list[dict[str, JsonValue]]:
        return [volume.mount_input() for volume in self.volumes]


__all__ = ["EndpointStubConfig"]
