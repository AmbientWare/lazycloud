from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated, Literal

from foundation.resources import parse_memory_mib
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from shared.container_requests import OciRuntimeName
from shared.deployment_records import DEFAULT_DISK
from shared.enums import StringEnum
from shared.image_building.authoring import PythonVersion
from shared.mounts import MountAuthMode, validate_mount_auth

type ManagedPythonExecutable = Literal[
    "python3.10",
    "python3.11",
    "python3.12",
    "micromamba3.10",
    "micromamba3.11",
    "micromamba3.12",
]


class ExecutionPythonVersion(StringEnum):
    Python310 = "3.10"
    Python311 = "3.11"
    Python312 = "3.12"
    Micromamba310 = "micromamba3.10"
    Micromamba311 = "micromamba3.11"
    Micromamba312 = "micromamba3.12"


def parse_execution_python_version(value: object) -> ExecutionPythonVersion:
    if isinstance(value, ExecutionPythonVersion):
        return value
    if isinstance(value, PythonVersion):
        return ExecutionPythonVersion(value.value)
    if not isinstance(value, str):
        msg = "Python version must be a supported major.minor string"
        raise ValueError(msg)

    normalized = value.strip()
    if not normalized.startswith("micromamba"):
        normalized = normalized.removeprefix("python")
    try:
        return ExecutionPythonVersion(normalized)
    except ValueError as exc:
        supported = ", ".join(version.value for version in ExecutionPythonVersion)
        msg = f"Python version must be one of {supported}; received {value!r}"
        raise ValueError(msg) from exc


type ExecutionPythonVersionInput = Annotated[
    ExecutionPythonVersion,
    BeforeValidator(parse_execution_python_version),
]


def managed_python_executable(version: ExecutionPythonVersion) -> ManagedPythonExecutable:
    match version:
        case ExecutionPythonVersion.Python310:
            return "python3.10"
        case ExecutionPythonVersion.Python311:
            return "python3.11"
        case ExecutionPythonVersion.Python312:
            return "python3.12"
        case ExecutionPythonVersion.Micromamba310:
            return "micromamba3.10"
        case ExecutionPythonVersion.Micromamba311:
            return "micromamba3.11"
        case ExecutionPythonVersion.Micromamba312:
            return "micromamba3.12"


def env_sequence_mapping(values: Iterable[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for value in values:
        key, separator, item = value.partition("=")
        if separator:
            env[key] = item
    return env


class VolumeProviderConfig(BaseModel):
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
    def credentials_match_auth_mode(self) -> VolumeProviderConfig:
        validate_mount_auth(self.auth_mode, self.access_key, self.secret_key)
        return self

    def for_mount(self, *, read_only: bool) -> VolumeProviderConfig:
        return self.model_copy(update={"read_only": read_only or self.read_only})


class VolumeMountInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    mount_path: str
    config: VolumeProviderConfig


class VolumeConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    id: str = ""
    name: str = ""
    mount_path: str = ""
    read_only: bool = False
    config: VolumeProviderConfig | None = None

    def mount_input(self) -> VolumeMountInput:
        provider = self.config or VolumeProviderConfig()
        return VolumeMountInput(
            id=self.name or self.id,
            mount_path=self.mount_path,
            config=provider.for_mount(read_only=self.read_only),
        )


class ContainerResourceConfig(BaseModel):
    """The resource request every workload kind states the same way.

    Each kind adds its own scheduling fields on top; what a container asks the
    platform for — CPU, memory, disk, GPU, runtime — does not vary by kind, so
    one reading of `cpu=0.25` or `memory="512Mi"` has to hold for all of them.
    """

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
    pool_selector: str | None = None
    runtime: str = OciRuntimeName.Runsc.value
    runtime_class: str | None = None
    docker_enabled: bool = False
    preemptible: bool = False
    workspace_gpu_quota: int = Field(default=0, ge=0)
    workspace_cpu_quota_millicores: int = Field(default=0, ge=0)

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
