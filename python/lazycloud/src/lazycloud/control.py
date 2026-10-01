from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Generic, TypeVar
from urllib.parse import urlencode

from typing_extensions import Self

from lazycloud.clients.api import ApiClient
from lazycloud.clients.storage import StorageClient
from lazycloud.exceptions import ConfigurationError

# Set by the platform in every workload container.
CONTAINER_API_ENV = "LAZYCLOUD_CONTAINER_API"

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
    # The container API socket, inside a workload container without a token.
    container_api: str | None = None


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
    """Explicit arguments, then the workspace scope, then the profile and its env overrides."""
    from lazycloud.config import get_profile

    profile = get_profile()
    selected_workspace = workspace if workspace is not None else _CONTROL_WORKSPACE.get()
    selected_token = token if token is not None else profile.token or None
    # Inside a workload container the platform serves the API on a socket and
    # knows the caller; a configured token or endpoint still takes precedence.
    container_api = os.environ.get(CONTAINER_API_ENV, "").strip() or None
    if selected_token is not None or (endpoint or "").strip():
        container_api = None
    return ControlClientConfig(
        endpoint=endpoint_url(
            (endpoint or "").strip() or profile.resolved_endpoint(), tls=profile.tls
        ),
        token=selected_token,
        workspace=(selected_workspace if selected_workspace is not None else profile.workspace),
        timeout_seconds=timeout_seconds,
        container_api=container_api,
    )


def endpoint_url(endpoint: str, *, tls: bool) -> str:
    """The endpoint as a base URL; a bare host takes its scheme from `tls`."""
    selected = endpoint.strip()
    if not selected:
        raise ConfigurationError("the control plane endpoint cannot be empty")
    if "://" in selected:
        return selected.rstrip("/")
    scheme = "https" if tls else "http"
    return f"{scheme}://{selected}".rstrip("/")


def api_client(config: ControlClientConfig) -> ApiClient:
    if config.container_api:
        return ApiClient(
            endpoint=config.endpoint,
            container_api=config.container_api,
            timeout_seconds=config.timeout_seconds,
        )
    if not config.token:
        raise ConfigurationError(
            "no access token is configured; run `lazycloud login --token <token>`"
        )
    return ApiClient(
        endpoint=config.endpoint, token=config.token, timeout_seconds=config.timeout_seconds
    )


def storage_client(config: ControlClientConfig) -> StorageClient:
    return StorageClient(api_client(config), require_workspace(config))


def require_workspace(config: ControlClientConfig) -> str:
    workspace = config.workspace.strip()
    if not workspace:
        raise ConfigurationError(
            "no workspace is selected; pass --workspace or run "
            "`lazycloud login --token <token> --workspace <name>`"
        )
    return workspace


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
    "api_client",
    "control_workspace_scope",
    "endpoint_url",
    "require_workspace",
    "resolve_control_client_config",
    "storage_client",
    "workspace_path",
    "workspace_query",
]
