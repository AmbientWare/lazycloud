from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Generic, TypeVar
from urllib.parse import urlencode

from shared.env import (
    GATEWAY_HTTP_URL_ENV,
    GATEWAY_TOKEN_ENV,
    WORKSPACE_ID_ENV,
    WORKSPACE_NAME_ENV,
)
from typing_extensions import Self

from lazycloud.config import get_profile

_CONTROL_WORKSPACE: ContextVar[str | None] = ContextVar(
    "lazycloud_control_workspace",
    default=None,
)
ClientT = TypeVar("ClientT")


class ResourceControlBinding(Generic[ClientT]):
    __slots__ = ()

    client: ClientT | None
    workspace: str | None
    endpoint: str | None
    token: str | None
    timeout_seconds: float

    def _bind_control(
        self,
        client: ClientT | None = None,
        *,
        workspace: str | None = None,
        endpoint: str | None = None,
        token: str | None = None,
        timeout_seconds: float | None = None,
    ) -> Self:
        self.client = client
        if workspace is not None:
            self.workspace = workspace
        if endpoint is not None:
            self.endpoint = endpoint
        if token is not None:
            self.token = token
        if timeout_seconds is not None:
            self.timeout_seconds = timeout_seconds
        return self


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
        workspace=(workspace or _CONTROL_WORKSPACE.get() or gateway_workspace or profile.workspace),
        timeout_seconds=timeout_seconds,
    )


def workspace_query(workspace: str) -> dict[str, str]:
    """The workspace a request names, or nothing at all.

    Blank is not a missing value to be filled in with a guess: it means the
    caller chose none, and the control plane answers that with the account's
    own. Clients ask here rather than building the query themselves so the
    question has one answer.
    """
    selected = workspace.strip()
    return {"workspace": selected} if selected else {}


def workspace_path(path: str, workspace: str) -> str:
    """`path` with the workspace named on it, or unchanged when none was chosen."""
    query = workspace_query(workspace)
    if not query:
        return path
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}{urlencode(query)}"


@contextmanager
def control_workspace_scope(workspace: str) -> Iterator[None]:
    """Act in one workspace for the duration, or leave the choice unmade.

    Blank is a caller who named none, which the control plane resolves, so it
    is yielded through rather than refused.
    """
    selected_workspace = workspace.strip()
    if not selected_workspace:
        yield
        return
    token = _CONTROL_WORKSPACE.set(selected_workspace)
    try:
        yield
    finally:
        _CONTROL_WORKSPACE.reset(token)


__all__ = [
    "ControlClientConfig",
    "ControlClientConfigMixin",
    "control_workspace_scope",
    "resolve_control_client_config",
    "workspace_path",
    "workspace_query",
]
