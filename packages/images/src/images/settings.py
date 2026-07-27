from __future__ import annotations

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX

from images.execution import (
    ImageBuildExecutor,
    ImageBuildExecutorKind,
    create_image_build_executor,
)
from images.publication import RegistryImageBuildPublicationPublisher
from images.registry import DockerRegistryPushClient, SkopeoBaseImageDigestInspector
from images.scheduling import (
    DEFAULT_IMAGE_BUILD_CONTAINER_ADDRESS_POLL_SECONDS,
    DEFAULT_IMAGE_BUILD_CONTAINER_ADDRESS_WAIT_SECONDS,
    DEFAULT_IMAGE_BUILD_CONTAINER_CPU_MILLICORES,
    DEFAULT_IMAGE_BUILD_CONTAINER_MEMORY_MIB,
)

DEFAULT_IMAGE_BUILD_REGISTRY_INSPECT_TIMEOUT_SECONDS = 30


class ImageBuildExecutionSettings(BaseSettings):
    executor: ImageBuildExecutorKind = ImageBuildExecutorKind.Manifest
    docker_binary: str = Field(default="docker", min_length=1)

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_IMAGE_BUILD_",
        extra="ignore",
        str_strip_whitespace=True,
    )

    def create_executor(self) -> ImageBuildExecutor:
        return create_image_build_executor(
            self.executor,
            docker_binary=self.docker_binary,
        )


class ImageBuildRegistrySettings(BaseSettings):
    push_enabled: bool = False
    target_ref: str = ""
    docker_binary: str = ""
    inspect_binary: str = Field(default="skopeo", min_length=1)
    inspect_timeout_seconds: int = Field(
        default=DEFAULT_IMAGE_BUILD_REGISTRY_INSPECT_TIMEOUT_SECONDS,
        gt=0,
    )
    inspect_tls_verify: bool = True

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_IMAGE_BUILD_REGISTRY_",
        extra="ignore",
        str_strip_whitespace=True,
    )

    def create_inspector(self) -> SkopeoBaseImageDigestInspector:
        return SkopeoBaseImageDigestInspector(
            binary=self.inspect_binary,
            timeout_seconds=self.inspect_timeout_seconds,
            tls_verify=self.inspect_tls_verify,
        )

    def create_publication_publisher(
        self,
        *,
        default_docker_binary: str = "docker",
    ) -> RegistryImageBuildPublicationPublisher | None:
        if not self.push_enabled:
            return None
        docker_binary = self.docker_binary or default_docker_binary.strip()
        if not docker_binary:
            raise ValueError("image build registry docker binary is required")
        return RegistryImageBuildPublicationPublisher(
            DockerRegistryPushClient(docker_binary=docker_binary),
            target_ref=self.target_ref,
        )


class ImageBuildContainerSettings(BaseSettings):
    pool_selector: str = ""
    cpu_millicores: int = Field(
        default=DEFAULT_IMAGE_BUILD_CONTAINER_CPU_MILLICORES,
        gt=0,
    )
    memory_mib: int = Field(
        default=DEFAULT_IMAGE_BUILD_CONTAINER_MEMORY_MIB,
        gt=0,
    )
    address_wait_timeout_seconds: float = Field(
        default=DEFAULT_IMAGE_BUILD_CONTAINER_ADDRESS_WAIT_SECONDS,
        gt=0,
    )
    address_poll_interval_seconds: float = Field(
        default=DEFAULT_IMAGE_BUILD_CONTAINER_ADDRESS_POLL_SECONDS,
        gt=0,
    )

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_IMAGE_BUILD_CONTAINER_",
        extra="ignore",
        str_strip_whitespace=True,
    )

    @model_validator(mode="after")
    def validate_poll_interval(self) -> ImageBuildContainerSettings:
        if self.address_poll_interval_seconds > self.address_wait_timeout_seconds:
            raise ValueError(
                "image build container address poll interval cannot exceed the wait timeout"
            )
        return self


__all__ = [
    "DEFAULT_IMAGE_BUILD_CONTAINER_ADDRESS_POLL_SECONDS",
    "DEFAULT_IMAGE_BUILD_CONTAINER_ADDRESS_WAIT_SECONDS",
    "DEFAULT_IMAGE_BUILD_REGISTRY_INSPECT_TIMEOUT_SECONDS",
    "ImageBuildContainerSettings",
    "ImageBuildExecutionSettings",
    "ImageBuildRegistrySettings",
]
