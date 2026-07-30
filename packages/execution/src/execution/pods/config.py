from __future__ import annotations

from foundation.resources import parse_memory_mib
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from shared.mounts import MountAuthMode, validate_mount_auth


class PodImageConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    image_id: str | None = None
    entrypoint: list[str] = Field(default_factory=list)


class PodRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    cpu: int | float | None = Field(default=None, ge=0)
    cpu_millicores: int = Field(default=0, ge=0)
    memory: str | int | None = None
    memory_mib: int = Field(default=0, ge=0)
    disk: str | int | None = None
    gpu: str | None = None
    gpu_type: str | None = None
    gpu_count: int = Field(default=0, ge=0)
    gpu_request: list[str] = Field(default_factory=list)
    requires_gpu: bool = False
    image_id: str | None = None
    keep_warm: int = Field(default=0, ge=-1)
    checkpoint_enabled: bool = False
    checkpoint_readiness_path: str = ""
    checkpoint_readiness_port: int = Field(default=0, ge=0, le=65535)
    checkpoint_readiness_timeout_seconds: int = Field(default=600, ge=1)
    checkpoint_readiness_interval_seconds: float = Field(default=1.0, gt=0)
    pool_selector: str | None = None
    runtime: str = "runc"
    runtime_class: str | None = None
    docker_enabled: bool = False
    block_network: bool = False
    allow_list: list[str] = Field(default_factory=list)
    preemptible: bool = False
    gpu_limit: int = Field(default=0, ge=0)
    cpu_limit_millicores: int = Field(default=0, ge=0)

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


class PodVolumeProviderConfig(BaseModel):
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
    def credentials_match_auth_mode(self) -> PodVolumeProviderConfig:
        validate_mount_auth(self.auth_mode, self.access_key, self.secret_key)
        return self

    def for_mount(self, *, read_only: bool) -> PodVolumeProviderConfig:
        return self.model_copy(update={"read_only": read_only or self.read_only})


class PodVolumeMountInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    mount_path: str
    config: PodVolumeProviderConfig


class PodVolumeConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    id: str = ""
    name: str = ""
    mount_path: str = ""
    read_only: bool = False
    config: PodVolumeProviderConfig | None = None

    def mount_input(self) -> PodVolumeMountInput:
        provider = self.config or PodVolumeProviderConfig()
        return PodVolumeMountInput(
            id=self.name or self.id,
            mount_path=self.mount_path,
            config=provider.for_mount(read_only=self.read_only),
        )


class PodStubConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    object_id: str = ""
    image: PodImageConfig = Field(default_factory=PodImageConfig)
    runtime: PodRuntimeConfig = Field(default_factory=PodRuntimeConfig)
    env: dict[str, str | None] = Field(default_factory=dict)
    secrets: list[str] = Field(default_factory=list)
    command: list[str] = Field(default_factory=list)
    ports: dict[str, int] = Field(default_factory=dict)
    volumes: list[PodVolumeConfig] = Field(default_factory=list)

    @property
    def effective_image_id(self) -> str:
        return self.image.image_id or self.runtime.image_id or ""

    @property
    def effective_entrypoint(self) -> list[str]:
        return self.command or self.image.entrypoint

    @property
    def env_list(self) -> list[str]:
        return [name if value is None else f"{name}={value}" for name, value in self.env.items()]

    @property
    def exposed_ports(self) -> list[int]:
        return list(self.ports.values())

    @property
    def volume_inputs(self) -> list[PodVolumeMountInput]:
        return [volume.mount_input() for volume in self.volumes]


__all__ = ["PodStubConfig"]
