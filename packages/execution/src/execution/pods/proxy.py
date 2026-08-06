from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock
from typing import Protocol

from pydantic import Field
from shared.contracts import ContractModel

DEFAULT_POD_PROXY_TIMEOUT_SECONDS = 175.0
PINNED_SANDBOX_CONNECT_TIMEOUT_SECONDS = 1.0


class PodProxyUnavailable(RuntimeError):
    pass


class PodProxyPortUnavailable(PodProxyUnavailable):
    pass


class PodProxyBackendError(RuntimeError):
    pass


class PodProxyHttpRequest(ContractModel):
    stub_id: str
    container_id: str | None = None
    port: int
    method: str
    path: str = "/"
    query_params: dict[str, list[str]] = Field(default_factory=dict)
    headers: dict[str, list[str]] = Field(default_factory=dict)
    body: bytes = b""


class PodProxyHttpResponse(ContractModel):
    status_code: int = 200
    headers: dict[str, list[str]] = Field(default_factory=dict)
    body: bytes = b""


@dataclass(frozen=True, slots=True)
class PodProxyTarget:
    container_id: str
    address: str
    route_id: str = ""


@dataclass(slots=True)
class PodProxySession:
    workspace_id: str
    stub_id: str
    target: PodProxyTarget
    keep_warm_seconds: int | None
    pinned: bool = False
    _finalization_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _finished: bool = field(default=False, init=False, repr=False)

    @contextmanager
    def finalization(self) -> Iterator[bool]:
        with self._finalization_lock:
            if self._finished:
                yield False
                return
            yield True
            self._finished = True


class PodProxyForwardClient(Protocol):
    def forward(
        self,
        target: PodProxyTarget,
        request: PodProxyHttpRequest,
        *,
        timeout_seconds: float = DEFAULT_POD_PROXY_TIMEOUT_SECONDS,
        connect_timeout_seconds: float | None = None,
    ) -> PodProxyHttpResponse: ...


class PodProxySocketClient(Protocol):
    def open_socket(
        self,
        target: PodProxyTarget,
        *,
        timeout_seconds: float = DEFAULT_POD_PROXY_TIMEOUT_SECONDS,
    ) -> socket.socket: ...


class PodProxyConnectionRepository(Protocol):
    def container_connections(self, workspace_id: str, stub_id: str, container_id: str) -> int: ...

    def increment_container_connections(
        self,
        workspace_id: str,
        stub_id: str,
        container_id: str,
        *,
        keep_warm_seconds: int | None,
    ) -> int: ...

    def decrement_container_connections(
        self,
        workspace_id: str,
        stub_id: str,
        container_id: str,
        *,
        keep_warm_seconds: int | None,
    ) -> int: ...

    def increment_total_connections(self, workspace_id: str, stub_id: str) -> int: ...

    def decrement_total_connections(self, workspace_id: str, stub_id: str) -> int: ...


@dataclass(slots=True)
class NullPodProxyConnectionRepository:
    def container_connections(self, workspace_id: str, stub_id: str, container_id: str) -> int:
        _ = workspace_id, stub_id, container_id
        return 0

    def increment_container_connections(
        self,
        workspace_id: str,
        stub_id: str,
        container_id: str,
        *,
        keep_warm_seconds: int | None,
    ) -> int:
        _ = workspace_id, stub_id, container_id, keep_warm_seconds
        return 0

    def decrement_container_connections(
        self,
        workspace_id: str,
        stub_id: str,
        container_id: str,
        *,
        keep_warm_seconds: int | None,
    ) -> int:
        _ = workspace_id, stub_id, container_id, keep_warm_seconds
        return 0

    def increment_total_connections(self, workspace_id: str, stub_id: str) -> int:
        _ = workspace_id, stub_id
        return 0

    def decrement_total_connections(self, workspace_id: str, stub_id: str) -> int:
        _ = workspace_id, stub_id
        return 0
