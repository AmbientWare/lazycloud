from __future__ import annotations

from pydantic import Field

from shared.contracts import ContractModel
from shared.enums import StringEnum
from shared.image_building.constants import DEFAULT_IMAGE_BASE


class ImageBuildStepKind(StringEnum):
    Shell = "shell"
    Pip = "pip"
    UvProject = "uv-project"
    PoetryProject = "poetry-project"
    Pyproject = "pyproject"
    MicromambaEnvironment = "micromamba-environment"
    Micromamba = "micromamba"
    Apt = "apt"


class PythonVersion(StringEnum):
    Py310 = "3.10"
    Py311 = "3.11"
    Py312 = "3.12"
    Py313 = "3.13"
    Py314 = "3.14"


PROJECT_BUILD_STEP_KINDS = frozenset(
    {
        ImageBuildStepKind.UvProject,
        ImageBuildStepKind.PoetryProject,
        ImageBuildStepKind.Pyproject,
        ImageBuildStepKind.MicromambaEnvironment,
    }
)


class LinuxArchitecture(StringEnum):
    Amd64 = "amd64"
    Arm64 = "arm64"


class ImageBuildStep(ContractModel):
    kind: ImageBuildStepKind
    args: list[str] = Field(default_factory=list)
    command: str | None = None
    groups: list[str] = Field(default_factory=list)


class ImageFilesystemSource(ContractModel):
    container_id: str
    worker_id: str


class ImageSpec(ContractModel):
    architecture: LinuxArchitecture = LinuxArchitecture.Amd64
    base: str = DEFAULT_IMAGE_BASE
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
    filesystem_source: ImageFilesystemSource | None = None


__all__ = [
    "PROJECT_BUILD_STEP_KINDS",
    "ImageBuildStep",
    "ImageBuildStepKind",
    "ImageFilesystemSource",
    "ImageSpec",
    "LinuxArchitecture",
    "PythonVersion",
]
