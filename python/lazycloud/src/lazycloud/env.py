from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from typing import TypeVar, overload

from shared.enums import StringEnum
from shared.env import (
    CONTAINER_ID_ENV,
    GATEWAY_HTTP_URL_ENV,
    GATEWAY_TOKEN_ENV,
    IMPORTING_USER_CODE_ENV,
    WORKSPACE_ID_ENV,
    WORKSPACE_NAME_ENV,
    importing_user_code_now,
    truthy_env_value,
)

EnvScalar = str | int | float | bool
R = TypeVar("R")


class SdkEnvVar(StringEnum):
    ImportingUserCode = IMPORTING_USER_CODE_ENV
    ContainerId = CONTAINER_ID_ENV
    GatewayHttpUrl = GATEWAY_HTTP_URL_ENV
    GatewayToken = GATEWAY_TOKEN_ENV
    WorkspaceId = WORKSPACE_ID_ENV
    WorkspaceName = WORKSPACE_NAME_ENV
    JupyterParentPid = "JPY_PARENT_PID"
    VscodePid = "VSCODE_PID"


def called_on_import(env: Mapping[str, str] | None = None) -> bool:
    return importing_user_code_now(env)


def is_local(env: Mapping[str, str] | None = None) -> bool:
    source = _environment(env)
    return not source.get(SdkEnvVar.ContainerId.value, "").strip()


def is_remote(env: Mapping[str, str] | None = None) -> bool:
    return not is_local(env)


def local_entrypoint(func: Callable[[], R]) -> Callable[[], R]:
    if is_local():
        func()
    return func


@overload
def env_value(
    name: str | StringEnum, default: bool, env: Mapping[str, str] | None = None
) -> bool: ...


@overload
def env_value(
    name: str | StringEnum, default: int, env: Mapping[str, str] | None = None
) -> int: ...


@overload
def env_value(
    name: str | StringEnum,
    default: float,
    env: Mapping[str, str] | None = None,
) -> float: ...


@overload
def env_value(
    name: str | StringEnum, default: str, env: Mapping[str, str] | None = None
) -> str: ...


def env_value(
    name: str | StringEnum,
    default: EnvScalar,
    env: Mapping[str, str] | None = None,
) -> EnvScalar:
    raw = _environment(env).get(_env_name(name))
    if raw is None or raw == "":
        return default
    if isinstance(default, bool):
        return truthy_env_value(raw)
    try:
        if isinstance(default, int) and not isinstance(default, bool):
            return int(raw)
        if isinstance(default, float):
            return float(raw)
    except ValueError:
        return default
    return raw


def is_notebook_environment(env: Mapping[str, str] | None = None) -> bool:
    source = _environment(env)
    if source.get(SdkEnvVar.JupyterParentPid.value) or source.get(SdkEnvVar.VscodePid.value):
        return True
    loaded_modules = set(sys.modules)
    return bool(
        {"google.colab", "marimo"}.intersection(loaded_modules)
        or any(module.startswith("ipykernel") for module in loaded_modules)
    )


def _env_name(name: str | StringEnum) -> str:
    if isinstance(name, StringEnum):
        return name.value
    return name


def _environment(env: Mapping[str, str] | None = None) -> Mapping[str, str]:
    return env if env is not None else os.environ


__all__ = [
    "EnvScalar",
    "SdkEnvVar",
    "called_on_import",
    "env_value",
    "is_local",
    "is_notebook_environment",
    "is_remote",
    "local_entrypoint",
]
