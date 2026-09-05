from __future__ import annotations

from uuid import UUID, uuid4

from pydantic import Field

from shared.http.base import HttpModel
from shared.image_building.authoring import LinuxArchitecture
from shared.image_building.records import BuildStatus, ImageBuildPhase


class BuildStep(HttpModel):
    type: str = ""
    command: str = ""


class VerifyImageBuildRequest(HttpModel):
    architecture: LinuxArchitecture = LinuxArchitecture.Amd64
    python_version: str = "3.12"
    python_packages: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    force_rebuild: bool = False
    existing_image_uri: str = ""
    existing_image_creds: dict[str, str] = Field(default_factory=dict, repr=False)
    build_steps: list[BuildStep] = Field(default_factory=list)
    env_vars: list[str] = Field(default_factory=list)
    dockerfile: str = ""
    build_ctx_object: str = ""
    build_ctx_digest: str = ""
    secrets: list[str] = Field(default_factory=list)
    gpu: str = ""
    ignore_python: bool = False
    image_id: str | None = None


class VerifyImageBuildResponse(HttpModel):
    image_id: str
    valid: bool
    exists: bool
    build_id: str = ""
    cache_key: str = ""
    reason: str = ""


class BuildImageRequest(HttpModel):
    request_id: UUID = Field(default_factory=uuid4)
    architecture: LinuxArchitecture = LinuxArchitecture.Amd64
    python_version: str = "3.12"
    python_packages: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    existing_image_uri: str = ""
    existing_image_creds: dict[str, str] = Field(default_factory=dict, repr=False)
    build_steps: list[BuildStep] = Field(default_factory=list)
    env_vars: list[str] = Field(default_factory=list)
    dockerfile: str = ""
    build_ctx_object: str = ""
    build_ctx_digest: str = ""
    secrets: list[str] = Field(default_factory=list)
    gpu: str = ""
    ignore_python: bool = False


class BuildImageResponse(HttpModel):
    image_id: str = ""
    build_id: str = ""
    msg: str = ""
    done: bool = False
    success: bool = False
    python_version: str = ""
    warning: bool = False
    status: BuildStatus = BuildStatus.Running
    phase: ImageBuildPhase = ImageBuildPhase.Submitted
    error: str = ""


class BuildImageEvent(HttpModel):
    sequence: int = Field(ge=0)
    response: BuildImageResponse


__all__ = [
    "BuildImageEvent",
    "BuildImageRequest",
    "BuildImageResponse",
    "BuildStep",
    "VerifyImageBuildRequest",
    "VerifyImageBuildResponse",
]
