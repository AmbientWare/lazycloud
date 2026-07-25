from __future__ import annotations

from enum import StrEnum

from pydantic import Field
from shared.contracts import ContractModel
from shared.image_building.authoring import LinuxArchitecture

IMAGE_BUILD_REQUEST_KIND = "image-build"


class ImageBuildSchedulerCredentialSource(StrEnum):
    None_ = "none"
    EphemeralPrivateInputs = "ephemeral-private-inputs"


class ImageBuildCredentialAction(StrEnum):
    Skip = "skip"
    UseSourcePullCredentials = "use-source-pull-credentials"


class ImageRegistryCredentialKind(StrEnum):
    Public = "public"
    Basic = "basic"
    Aws = "aws"
    Gcp = "gcp"
    Azure = "azure"
    Token = "token"
    Unknown = "unknown"


class ImageBuildContainerBuildOptions(ContractModel):
    architecture: LinuxArchitecture = LinuxArchitecture.Amd64
    managed_package_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    source_image: str = ""
    dockerfile: str = ""
    build_context_object: str = ""
    build_context_path: str = ""
    build_context_digest: str = ""
    build_secret_names: list[str] = Field(default_factory=list)
    build_arg_names: list[str] = Field(default_factory=list)


class ImageBuildContainerCredentialMetadata(ContractModel):
    action: ImageBuildCredentialAction = ImageBuildCredentialAction.Skip
    registry: str = ""
    repository: str = ""
    kind: ImageRegistryCredentialKind = ImageRegistryCredentialKind.Public
    credential_keys: list[str] = Field(default_factory=list)
    source: ImageBuildSchedulerCredentialSource = ImageBuildSchedulerCredentialSource.None_
    cache_key: str = ""
    ttl_seconds: int = 0
    reason: str = ""
