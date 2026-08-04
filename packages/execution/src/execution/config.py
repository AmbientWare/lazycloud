from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, model_validator
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
