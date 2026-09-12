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
    Py313 = "3.13"
    Py314 = "3.14"


class LinuxArchitecture(StringEnum):
    Amd64 = "amd64"
    Arm64 = "arm64"


class ImageBuildStep(ContractModel):
    kind: ImageBuildStepKind
    args: list[str] = Field(default_factory=list)
    command: str | None = None


class FilesystemSnapshotSource(ContractModel):
    object_id: str = Field(min_length=1)
    ownership_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)


class FilesystemSnapshotMetadata(ContractModel):
    env: list[str] = Field(default_factory=list)
    workdir: str
    architecture: LinuxArchitecture


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
    filesystem_snapshot: FilesystemSnapshotSource | None = None


__all__ = [
    "FilesystemSnapshotMetadata",
    "FilesystemSnapshotSource",
    "ImageBuildStep",
    "ImageBuildStepKind",
    "ImageSpec",
    "LinuxArchitecture",
    "PythonVersion",
]
