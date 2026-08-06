"""Move bytes between a caller and a control-plane replica, understanding neither.

Forwarding at layer 4 is the point rather than a shortcut. The traffic here is
HTTP, WebSocket upgrades, server-sent event streams, and TLS whose SNI the
control plane routes on itself; a proxy that parsed any of them would have to
keep parsing all of them correctly forever, and would terminate the TLS the
destination needs intact.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import socket
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

FORWARD_BUFFER_BYTES = 64 * 1024


async def forward_stream(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        while True:
            chunk = await reader.read(FORWARD_BUFFER_BYTES)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, TimeoutError):
        return
    finally:
        if not writer.is_closing():
            with_suppressed_close(writer)


def with_suppressed_close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.write_eof()
    except (OSError, RuntimeError):
        return


async def resolve_upstreams(host: str, port: int) -> list[tuple[str, int]]:
    """Every replica currently answering to the upstream name.

    Resolved per connection, not once: a replica that arrives or leaves changes
    this answer, and a set captured at startup would keep sending callers to a
    container that has gone.
    """
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses: list[tuple[str, int]] = []
    for info in infos:
        address = info[4]
        candidate = (str(address[0]), int(address[1]))
        if candidate not in addresses:
            addresses.append(candidate)
    return addresses


def rotate(addresses: list[tuple[str, int]], offset: int) -> list[tuple[str, int]]:
    """Start each connection at a different replica, in a stable order.

    DNS hands back every replica but the order is effectively fixed — the
    resolver sorts it — so connecting to the first address every time pins the
    whole deployment to one container. Rotating spreads the load; keeping the
    rest of the list as fallback is what turns a replica going away into a
    retry rather than a failed request.
    """
    if not addresses:
        return []
    start = offset % len(addresses)
    return addresses[start:] + addresses[:start]


async def open_upstream(
    addresses: list[tuple[str, int]],
    *,
    connect_timeout_seconds: float,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
    for host, port in addresses:
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=connect_timeout_seconds,
            )
        except (OSError, TimeoutError) as exc:
            logger.warning("control plane at %s:%s did not answer: %s", host, port, exc)
    return None


async def proxy_connection(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    *,
    upstream_host: str,
    upstream_port: int,
    connect_timeout_seconds: float,
    attempt: int,
) -> None:
    """Join one caller to one replica for as long as either keeps talking."""
    addresses = rotate(await resolve_upstreams(upstream_host, upstream_port), attempt)
    upstream = await open_upstream(addresses, connect_timeout_seconds=connect_timeout_seconds)
    if upstream is None:
        logger.error("no control plane answered at %s:%s", upstream_host, upstream_port)
        client_writer.close()
        return
    upstream_reader, upstream_writer = upstream

    try:
        await asyncio.gather(
            forward_stream(client_reader, upstream_writer),
            forward_stream(upstream_reader, client_writer),
        )
    finally:
        for writer in (client_writer, upstream_writer):
            if not writer.is_closing():
                writer.close()


async def serve_forwarder(
    *,
    listen_host: str,
    listen_port: int,
    upstream_host: str,
    upstream_port: int,
    connect_timeout_seconds: float,
) -> asyncio.Server:
    attempts = itertools.count()

    async def handle(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        await proxy_connection(
            reader,
            writer,
            upstream_host=upstream_host,
            upstream_port=upstream_port,
            connect_timeout_seconds=connect_timeout_seconds,
            attempt=next(attempts),
        )

    server = await asyncio.start_server(handle, listen_host, listen_port)
    logger.info(
        "forwarding %s:%s to %s:%s",
        listen_host,
        listen_port,
        upstream_host,
        upstream_port,
    )
    return server


ForwarderFactory = Callable[[], Awaitable[asyncio.Server]]


__all__ = [
    "FORWARD_BUFFER_BYTES",
    "ForwarderFactory",
    "forward_stream",
    "proxy_connection",
    "serve_forwarder",
]
