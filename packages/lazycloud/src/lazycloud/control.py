from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from shared.env import (
    GATEWAY_HTTP_URL_ENV,
    GATEWAY_TOKEN_ENV,
    WORKSPACE_ID_ENV,
    WORKSPACE_NAME_ENV,
)

from lazycloud.config import DEFAULT_WORKSPACE, get_profile

_CONTROL_WORKSPACE: ContextVar[str | None] = ContextVar(
    "lazycloud_control_workspace",
    default=None,
)


@dataclass(frozen=True, slots=True)
class ControlClientConfig:
    endpoint: str
    token: str | None
    workspace: str
    timeout_seconds: float


class ControlClientConfigMixin:
    endpoint: str | None
    token: str | None
    workspace: str | None
    timeout_seconds: float

    def _config(self) -> ControlClientConfig:
        return resolve_control_client_config(
            endpoint=self.endpoint,
            token=self.token,
            workspace=self.workspace,
            timeout_seconds=self.timeout_seconds,
        )


def resolve_control_client_config(
    *,
    endpoint: str | None = None,
    token: str | None = None,
    workspace: str | None = None,
    timeout_seconds: float = 10.0,
) -> ControlClientConfig:
    profile = get_profile()
    gateway_endpoint = os.environ.get(GATEWAY_HTTP_URL_ENV, "").strip()
    # Resolution order: explicit argument (CLI flag) > in-container gateway env >
    # LAZYCLOUD_ENDPOINT env / stored profile (merged by get_profile) > packaged
    # default. `resolved_endpoint` supplies the packaged default when blank.
    selected_endpoint = (endpoint or "").strip() or gateway_endpoint or profile.resolved_endpoint()
    gateway_token = os.environ.get(GATEWAY_TOKEN_ENV, "").strip()
    gateway_workspace = (
        os.environ.get(WORKSPACE_ID_ENV, "").strip()
        or os.environ.get(WORKSPACE_NAME_ENV, "").strip()
    )
    return ControlClientConfig(
        endpoint=selected_endpoint,
        token=token if token is not None else gateway_token or profile.token or None,
        workspace=(
            workspace
            or _CONTROL_WORKSPACE.get()
            or gateway_workspace
            or profile.workspace
            or DEFAULT_WORKSPACE
        ),
        timeout_seconds=timeout_seconds,
    )


@contextmanager
def control_workspace_scope(workspace: str) -> Iterator[None]:
    selected_workspace = workspace.strip()
    if not selected_workspace:
        raise ValueError("workspace must not be empty")
    token = _CONTROL_WORKSPACE.set(selected_workspace)
    try:
        yield
    finally:
        _CONTROL_WORKSPACE.reset(token)


__all__ = [
    "DEFAULT_WORKSPACE",
    "ControlClientConfig",
    "ControlClientConfigMixin",
    "control_workspace_scope",
    "resolve_control_client_config",
]
