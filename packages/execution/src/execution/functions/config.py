from __future__ import annotations

from foundation.resources import parse_memory_mib
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from shared.deployment_records import DEFAULT_DISK
from shared.lifecycle import LifecycleHooks
from shared.mounts import MountAuthMode, validate_mount_auth
from shared.tasks import RetryPolicy

from execution.config import (
    ExecutionPythonVersion,
    ExecutionPythonVersionInput,
    ManagedPythonExecutable,
    managed_python_executable,
)


class FunctionImageConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    image_id: str | None = None
    python_version: ExecutionPythonVersionInput = ExecutionPythonVersion.Python312

    @property
    def python_executable(self) -> ManagedPythonExecutable:
        return managed_python_executable(self.python_version)


class FunctionRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    cpu: int | float | None = Field(default=None, ge=0)
    cpu_millicores: int = Field(default=0, ge=0)
    memory: str | int | None = None
    memory_mib: int = Field(default=0, ge=0)
    disk: str | int = DEFAULT_DISK
    gpu: str | None = None
    gpu_type: str | None = None
    gpu_count: int = Field(default=0, ge=0)
    requires_gpu: bool = False
    image_id: str | None = None
    task_ttl_seconds: int = Field(default=0, ge=0)
    retries: int = Field(default=0, ge=0)
    pool_selector: str | None = None
    runtime: str = "runc"
    runtime_class: str | None = None
    docker_enabled: bool = False
    preemptible: bool = False
    gpu_limit: int = Field(default=0, ge=0)
    cpu_limit_millicores: int = Field(default=0, ge=0)

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

    @property
    def gpu_required(self) -> bool:
        return self.requires_gpu or bool(self.requested_gpu_type) or self.gpu_count > 0


class FunctionVolumeProviderConfig(BaseModel):
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
    def credentials_match_auth_mode(self) -> FunctionVolumeProviderConfig:
        validate_mount_auth(self.auth_mode, self.access_key, self.secret_key)
        return self

    def for_mount(self, *, read_only: bool) -> FunctionVolumeProviderConfig:
        return self.model_copy(update={"read_only": read_only or self.read_only})


class FunctionVolumeMountInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    mount_path: str
    config: FunctionVolumeProviderConfig


class FunctionVolumeConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    id: str = ""
    name: str = ""
    mount_path: str = ""
    read_only: bool = False
    config: FunctionVolumeProviderConfig | None = None

    def mount_input(self) -> FunctionVolumeMountInput:
        provider = self.config or FunctionVolumeProviderConfig()
        return FunctionVolumeMountInput(
            id=self.name or self.id,
            mount_path=self.mount_path,
            config=provider.for_mount(read_only=self.read_only),
        )


class FunctionStubConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    object_id: str = ""
    image: FunctionImageConfig = Field(default_factory=FunctionImageConfig)
    runtime: FunctionRuntimeConfig = Field(default_factory=FunctionRuntimeConfig)
    env: dict[str, str] = Field(default_factory=dict)
    secrets: list[str] = Field(default_factory=list)
    volumes: list[FunctionVolumeConfig] = Field(default_factory=list)
    retry_policy: RetryPolicy | None = None
    lifecycle_hooks: LifecycleHooks = Field(default_factory=LifecycleHooks)

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
    def volume_inputs(self) -> list[FunctionVolumeMountInput]:
        return [volume.mount_input() for volume in self.volumes]


__all__ = ["FunctionStubConfig"]
