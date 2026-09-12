from __future__ import annotations

import asyncio
import logging
import socket
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

import grpc.aio
from coordination.agent_connections import RedisAgentConnectionDirectory
from cryptography.hazmat.primitives import hashes
from shared.http.agent_tunnel import TunnelPacket, TunnelPacketKind, TunnelRouteRequest

from networking.tunnel_protocol import (
    ROUTE_METHOD,
    TUNNEL_GRPC_OPTIONS,
    GrpcPacketStream,
    bridge_socket,
    tunnel_method,
)
from networking.tunnel_tls import TunnelCredentials

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class TunnelRouteClient:
    directory: RedisAgentConnectionDirectory
    credentials: TunnelCredentials
    hostname: str
    _loop: asyncio.AbstractEventLoop = field(default_factory=asyncio.new_event_loop, init=False)
    _channels: dict[tuple[str, bytes], grpc.aio.Channel] = field(default_factory=dict, init=False)
    _channel_expirations: set[asyncio.Task[None]] = field(default_factory=set, init=False)
    _streams: set[asyncio.Task[None]] = field(default_factory=set, init=False)
    _pending: set[asyncio.Task[socket.socket]] = field(default_factory=set, init=False)
    _lifecycle: threading.Lock = field(default_factory=threading.Lock, init=False)
    _thread: threading.Thread = field(init=False)
    _closed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self._thread = threading.Thread(target=self._run, name="agent-tunnel-routes", daemon=True)
        self._thread.start()

    def connect(self, request: TunnelRouteRequest, timeout_seconds: float) -> socket.socket:
        with self._lifecycle:
            if self._closed:
                raise ConnectionError("Agent tunnel client is closed")
            future = asyncio.run_coroutine_threadsafe(
                self._dial(request, timeout_seconds), self._loop
            )
        return future.result()

    def close(self) -> None:
        with self._lifecycle:
            if self._closed:
                return
            self._closed = True
            closing = asyncio.run_coroutine_threadsafe(self._close(), self._loop)
        closing.result()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.run_until_complete(self._loop.shutdown_default_executor())
            self._loop.close()

    async def _dial(self, request: TunnelRouteRequest, timeout_seconds: float) -> socket.socket:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Tunnel dialing requires an active task")
        self._pending.add(task)
        try:
            return await self._open(request, timeout_seconds)
        finally:
            self._pending.discard(task)

    async def _open(self, request: TunnelRouteRequest, timeout_seconds: float) -> socket.socket:
        async with asyncio.timeout(timeout_seconds):
            record = await asyncio.to_thread(
                self.directory.get, request.workspace_id, request.enrollment_id
            )
            if record is None:
                raise ConnectionError("Agent has no active tunnel connection")
            identity = self.credentials.client()
            certificate = identity.certificate
            channel_key = record.gateway_address, certificate.fingerprint(hashes.SHA256())
            channel = self._channels.get(channel_key)
            if channel is None:
                # The directory selects a pod; the hostname authenticates its certificate.
                channel = grpc.aio.secure_channel(
                    record.gateway_address,
                    identity.credentials,
                    options=(*TUNNEL_GRPC_OPTIONS, ("grpc.default_authority", self.hostname)),
                )
                self._channels[channel_key] = channel
                expiration = asyncio.create_task(
                    self._expire_channel(channel_key, channel, certificate.not_valid_after_utc)
                )
                self._channel_expirations.add(expiration)
                expiration.add_done_callback(self._channel_expirations.discard)
            call = tunnel_method(channel, ROUTE_METHOD)()
            try:
                await call.write(request.model_dump_json().encode())
                incoming = call.__aiter__()
                opened = TunnelPacket.from_wire(await anext(incoming))
                if opened.kind is not TunnelPacketKind.Opened:
                    raise ConnectionError("Agent did not acknowledge opening the backend")
                client, peer = socket.socketpair()
                peer.setblocking(False)
                try:
                    reader, writer = await asyncio.open_connection(sock=peer)
                except BaseException:
                    client.close()
                    peer.close()
                    raise
                task = asyncio.create_task(self._bridge(call, incoming, reader, writer))
                self._streams.add(task)
                task.add_done_callback(self._streams.discard)
                return client
            except BaseException:
                call.cancel()
                raise

    async def _bridge(
        self,
        call: grpc.aio.StreamStreamCall[bytes, bytes],
        incoming: AsyncIterator[bytes],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            await bridge_socket(GrpcPacketStream(incoming, call.write), reader, writer)
            await call.done_writing()
        except (OSError, grpc.RpcError):
            LOGGER.debug("Agent backend stream disconnected")
        except ExceptionGroup as exc:
            _, unexpected = exc.split((OSError, grpc.RpcError))
            if unexpected is not None:
                LOGGER.error("Agent backend stream failed", exc_info=unexpected)
        finally:
            call.cancel()

    async def _close(self) -> None:
        for task in tuple(self._pending):
            task.cancel()
        await asyncio.gather(*self._pending, return_exceptions=True)
        for task in tuple(self._streams):
            task.cancel()
        await asyncio.gather(*self._streams, return_exceptions=True)
        await asyncio.gather(*(channel.close() for channel in self._channels.values()))
        for task in tuple(self._channel_expirations):
            task.cancel()
        await asyncio.gather(*self._channel_expirations, return_exceptions=True)
        self._channels.clear()

    async def _expire_channel(
        self, key: tuple[str, bytes], channel: grpc.aio.Channel, expires_at: datetime
    ) -> None:
        try:
            await asyncio.sleep(max(0, (expires_at - datetime.now(UTC)).total_seconds()))
        finally:
            self._channels.pop(key, None)
            await channel.close()
