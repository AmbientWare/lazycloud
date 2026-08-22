from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator
from shared.workload_config import absolute_health_check_path

from execution.config import (
    ContainerResourceConfig,
    VolumeConfig,
    VolumeMountInput,
)


class PodImageConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    image_id: str | None = None
    entrypoint: list[str] = Field(default_factory=list)


class PodRuntimeConfig(ContainerResourceConfig):
    gpu_request: list[str] = Field(default_factory=list)
    requires_gpu: bool = False
    keep_warm: int = Field(default=0, ge=-1)
    checkpoint_enabled: bool = False
    checkpoint_readiness_path: str = ""
    checkpoint_readiness_port: int = Field(default=0, ge=0, le=65535)
    checkpoint_readiness_timeout_seconds: int = Field(default=600, ge=1)
    checkpoint_readiness_interval_seconds: float = Field(default=1.0, gt=0)
    health_check_path: str = ""
    health_check_port: int = Field(default=0, ge=0, le=65535)

    @field_validator("health_check_path")
    @classmethod
    def health_check_path_is_absolute(cls, value: str) -> str:
        """Also enforced here, on the model the pod proxy reads.

        The write model validates what the SDK and deploy path send; a persisted
        config that reached storage any other way is read through this one, and a
        relative path makes every container permanently unready while the proxy
        can only report having nothing to route to.
        """

        return absolute_health_check_path(value)

    block_network: bool = False
    allow_list: list[str] = Field(default_factory=list)

    @property
    def gpu_required(self) -> bool:
        return self.requires_gpu or bool(self.requested_gpu_type) or self.gpu_count > 0


class PodStubConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    object_id: str = ""
    image: PodImageConfig = Field(default_factory=PodImageConfig)
    runtime: PodRuntimeConfig = Field(default_factory=PodRuntimeConfig)
    env: dict[str, str | None] = Field(default_factory=dict)
    secrets: list[str] = Field(default_factory=list)
    command: list[str] = Field(default_factory=list)
    ports: dict[str, int] = Field(default_factory=dict)
    volumes: list[VolumeConfig] = Field(default_factory=list)

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
    def volume_inputs(self) -> list[VolumeMountInput]:
        return [volume.mount_input() for volume in self.volumes]


__all__ = ["PodStubConfig"]
