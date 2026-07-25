from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated, Literal

from pydantic import BeforeValidator
from shared.enums import StringEnum
from shared.image_building.authoring import PythonVersion

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
