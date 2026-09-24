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
            try:
                yield True
            finally:
                # A release cancelled while it waits has still been sent, so a
                # second finish would take back a connection it never held.
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


async def release_pod_connection(
    connections: PodProxyConnectionRepository,
    *,
    workspace_id: str,
    stub_id: str,
    container_id: str | None,
    keep_warm_seconds: int | None,
) -> None:
    """Give back one connection, on the container too when it had reached one.

    Every release goes through here because a client that goes away cancels
    the task releasing for it, and a count left behind keeps the pod running
    with nobody connected. The decrements are scheduled before the first await
    and shielded from that cancellation, so they land whether or not the
    caller is still there to see them.
    """
    total = connections.decrement_total_connections(workspace_id, stub_id)
    if container_id is None:
        await asyncio.shield(total)
        return
    await asyncio.shield(
        asyncio.gather(
            connections.decrement_container_connections(
                workspace_id, stub_id, container_id, keep_warm_seconds=keep_warm_seconds
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
    """Count one connection to a running container for as long as the block runs.

    The same two counters a proxied connection moves, so the autoscaler keeps
    the container while it is held and starts the idle window when it is let go.
    """
    await connections.increment_total_connections(workspace_id, stub_id)
    try:
        await connections.increment_container_connections(
            workspace_id, stub_id, container_id, keep_warm_seconds=keep_warm_seconds
        )
    except BaseException:
        await release_pod_connection(
            connections,
            workspace_id=workspace_id,
            stub_id=stub_id,
            container_id=None,
            keep_warm_seconds=keep_warm_seconds,
        )
        raise
    try:
        yield
    finally:
        await release_pod_connection(
            connections,
            workspace_id=workspace_id,
            stub_id=stub_id,
            container_id=container_id,
            keep_warm_seconds=keep_warm_seconds,
        )


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
