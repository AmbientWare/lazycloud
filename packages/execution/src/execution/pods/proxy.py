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
PINNED_CONTAINER_CONNECT_TIMEOUT_SECONDS = 1.0


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
    target: PodProxyTarget
    hold: PodConnectionHold
    pinned: bool = False


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


@dataclass(slots=True)
class PodConnectionHold:
    """One connection's share of a pod's connection counts, taken once and given back once.

    The stub total rises first: it is what makes the autoscaler start a
    container for a pod that has none, so a connection records it before it
    waits. The container's own count rises once the connection reaches one,
    and is what holds that container's idle stop off.
    """

    connections: PodProxyConnectionRepository
    workspace_id: str
    stub_id: str
    keep_warm_seconds: int | None
    _demanded: bool = field(default=False, init=False)
    _container_id: str | None = field(default=None, init=False)
    _released: bool = field(default=False, init=False)

    async def demand(self) -> None:
        if self._demanded:
            return
        await self.connections.increment_total_connections(self.workspace_id, self.stub_id)
        self._demanded = True

    async def attach(self, container_id: str) -> None:
        await self.demand()
        await self.connections.increment_container_connections(
            self.workspace_id,
            self.stub_id,
            container_id,
            keep_warm_seconds=self.keep_warm_seconds,
        )
        self._container_id = container_id

    async def release(self) -> None:
        """Give back what was taken; later calls, and calls before anything was taken, do nothing.

        A client that goes away cancels the task releasing for it, and a count
        left behind keeps the pod running with nobody connected. The decrements
        are scheduled before the first await and shielded from that
        cancellation, so they land whether or not the caller is still there.
        """
        if self._released or not self._demanded:
            return
        self._released = True
        total = self.connections.decrement_total_connections(self.workspace_id, self.stub_id)
        if self._container_id is None:
            await asyncio.shield(total)
            return
        await asyncio.shield(
            asyncio.gather(
                self.connections.decrement_container_connections(
                    self.workspace_id,
                    self.stub_id,
                    self._container_id,
                    keep_warm_seconds=self.keep_warm_seconds,
                ),
                total,
            )
        )


@asynccontextmanager
async def held_container_connection(
    connections: PodProxyConnectionRepository,
    *,
    workspace_id: str,
    stub_id: str,
    container_id: str,
    keep_warm_seconds: int | None,
) -> AsyncIterator[None]:
    """Count one connection to a running container for as long as the block runs."""
    hold = PodConnectionHold(connections, workspace_id, stub_id, keep_warm_seconds)
    try:
        await hold.attach(container_id)
        yield
    finally:
        await hold.release()


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
