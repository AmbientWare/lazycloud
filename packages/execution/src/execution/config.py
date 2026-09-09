from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from shared.container_requests import OciRuntimeName
from shared.deployment_records import (
    DEFAULT_DISK,
    CpuRequest,
    MemoryRequest,
    request_and_limit,
)
from shared.enums import StringEnum
from shared.image_building.authoring import PythonVersion
from shared.mounts import MountAuthMode, validate_mount_auth
from shared.placement import ProductRegion
from shared.resources import parse_memory_mib
from shared.workload_config import (
    cpu_limit_at_or_above_request,
    memory_limit_at_or_above_request,
)

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
    region: ProductRegion | None = Field(default=None, strict=False)
    availability_zone: str = ""

    cpu: CpuRequest | None = Field(default=None)
    """Cores to reserve, or a `(reserve, throttle at)` pair.

    The pair is kept as written rather than split into two fields, the same way
    a bare value is kept and resolved by `requested_cpu_millicores`. What the
    author stated and what the platform computed from it stay distinguishable.
    """

    cpu_millicores: int = Field(default=0, ge=0)
    cpu_limit_millicores: int = Field(default=0, ge=0)
    memory: MemoryRequest | None = None
    """Memory to reserve, or a `(reserve, kill at)` pair."""

    memory_mib: int = Field(default=0, ge=0)
    memory_limit_mib: int = Field(default=0, ge=0)
    disk: str | int = DEFAULT_DISK
    gpu: list[str] = Field(default_factory=list)
    """Models this workload accepts, best first; empty asks for no GPU."""

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

    @field_validator("cpu")
    @classmethod
    def a_cpu_limit_cannot_sit_below_its_request(
        cls, value: CpuRequest | None
    ) -> CpuRequest | None:
        return cpu_limit_at_or_above_request(value)

    @field_validator("memory")
    @classmethod
    def a_memory_limit_cannot_sit_below_its_request(
        cls, value: MemoryRequest | None
    ) -> MemoryRequest | None:
        return memory_limit_at_or_above_request(value)

    @property
    def requested_cpu_millicores(self) -> int:
        if self.cpu_millicores:
            return self.cpu_millicores
        request, _ = request_and_limit(self.cpu)
        return int(float(request) * 1000) if request is not None else 0

    @property
    def limit_cpu_millicores(self) -> int:
        """Where this container is throttled, or zero to take the default."""
        if self.cpu_limit_millicores:
            return self.cpu_limit_millicores
        _, limit = request_and_limit(self.cpu)
        return int(float(limit) * 1000) if limit is not None else 0

    @property
    def requested_memory_mib(self) -> int:
        if self.memory_mib:
            return self.memory_mib
        request, _ = request_and_limit(self.memory)
        return parse_memory_mib(request) or 0

    @property
    def limit_memory_mib(self) -> int:
        """Where this container is killed, or zero to take the default."""
        if self.memory_limit_mib:
            return self.memory_limit_mib
        _, limit = request_and_limit(self.memory)
        return parse_memory_mib(limit) or 0

    @property
    def requested_disk_mib(self) -> int:
        # Reuses the memory parser: the units are the same and disk accepts the
        # same "10Gi" strings users already write for memory.
        return parse_memory_mib(self.disk) or 0
