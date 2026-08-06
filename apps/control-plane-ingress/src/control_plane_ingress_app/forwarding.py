"""Move bytes between a caller and a control-plane replica, understanding neither.

Forwarding at layer 4 is the point rather than a shortcut. The traffic here is
HTTP, WebSocket upgrades, server-sent event streams, and TLS whose SNI the
control plane routes on itself; a proxy that parsed any of them would have to
keep parsing all of them correctly forever, and would terminate the TLS the
destination needs intact.
"""

from __future__ import annotations

import asyncio
import logging
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


async def proxy_connection(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    *,
    upstream_host: str,
    upstream_port: int,
    connect_timeout_seconds: float,
) -> None:
    """Join one caller to one replica for as long as either keeps talking.

    The upstream address is a service name, so the platform's own DNS decides
    which replica answers. Resolving it per connection rather than once is what
    lets a replica leave without draining every future caller with it.
    """
    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(upstream_host, upstream_port),
            timeout=connect_timeout_seconds,
        )
    except (OSError, TimeoutError) as exc:
        logger.warning("no control plane answered at %s:%s: %s", upstream_host, upstream_port, exc)
        client_writer.close()
        return

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
