from __future__ import annotations

from pydantic import Field

from shared.contracts import ContractModel
from shared.enums import StringEnum


class ImageBuildStepKind(StringEnum):
    Shell = "shell"
    Pip = "pip"
    UvProject = "uv-project"
    Micromamba = "micromamba"
    Apt = "apt"


class PythonVersion(StringEnum):
    Py310 = "3.10"
    Py311 = "3.11"
    Py312 = "3.12"


class LinuxArchitecture(StringEnum):
    Amd64 = "amd64"
    Arm64 = "arm64"


class ImageBuildStep(ContractModel):
    kind: ImageBuildStepKind
    args: list[str] = Field(default_factory=list)
    command: str | None = None


class ImageSpec(ContractModel):
    architecture: LinuxArchitecture = LinuxArchitecture.Amd64
    base: str = "python:3.12-slim"
    python_version: str = "3.12"
    packages: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    build_steps: list[ImageBuildStep] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    workdir: str = "/workspace"
    dockerfile: str | None = None
    context_path: str | None = None
    context_digest: str | None = None
    context_object_id: str | None = None
    include_files_patterns: list[str] = Field(default_factory=list)
    credential_keys: list[str] = Field(default_factory=list)
    secrets: list[str] = Field(default_factory=list)
    build_secret_versions: dict[str, str] = Field(default_factory=dict)
    gpu: str | None = None
    image_id: str | None = None
    ignore_python: bool = False


__all__ = [
    "ImageBuildStep",
    "ImageBuildStepKind",
    "ImageSpec",
    "LinuxArchitecture",
    "PythonVersion",
]
