from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
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
    _finalization_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _finished: bool = field(default=False, init=False, repr=False)

    @asynccontextmanager
    async def finalization(self) -> AsyncIterator[bool]:
        async with self._finalization_lock:
            if self._finished:
                yield False
                return
            yield True
            self._finished = True


class PodProxySocketClient(Protocol):
    def open_socket(
        self,
        target: PodProxyTarget,
        *,
        timeout_seconds: float = DEFAULT_POD_PROXY_TIMEOUT_SECONDS,
    ) -> socket.socket: ...


class PodProxyConnectionRepository(Protocol):
    async def container_connections(
        self,
        workspace_id: str,
        stub_id: str,
        container_id: str,
    ) -> int: ...

    async def increment_container_connections(
        self,
        workspace_id: str,
        stub_id: str,
        container_id: str,
        *,
        keep_warm_seconds: int | None,
    ) -> int: ...

    async def decrement_container_connections(
        self,
        workspace_id: str,
        stub_id: str,
        container_id: str,
        *,
        keep_warm_seconds: int | None,
    ) -> int: ...

    async def increment_total_connections(self, workspace_id: str, stub_id: str) -> int: ...

    async def decrement_total_connections(self, workspace_id: str, stub_id: str) -> int: ...


class PodProxyResponseStream(Protocol):
    status_code: int
    headers: dict[str, list[str]]

    def iter_chunks(self) -> AsyncIterator[bytes]: ...

    async def close(self) -> None: ...


class AsyncPodProxyForwardClient(Protocol):
    async def open_stream(
        self,
        target: PodProxyTarget,
        request: PodProxyHttpRequest,
        *,
        timeout_seconds: float = DEFAULT_POD_PROXY_TIMEOUT_SECONDS,
        connect_timeout_seconds: float | None = None,
    ) -> PodProxyResponseStream: ...
