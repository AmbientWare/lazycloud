from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime

import grpc
import grpc.aio
from shared.http.agent_tunnel import (
    TUNNEL_HEARTBEAT_SECONDS,
    TUNNEL_MAX_STREAMS,
    TUNNEL_OPEN_TIMEOUT_SECONDS,
    TunnelCommand,
    TunnelCommandKind,
    TunnelPacket,
    TunnelPacketKind,
)
from shared.timestamps import utc_now

from networking.tunnel_protocol import (
    ATTACH_METHOD,
    CONNECT_METHOD,
    CONTROL_METHOD,
    TUNNEL_GRPC_OPTIONS,
    GrpcPacketStream,
    bridge_socket,
    tunnel_method,
)
from networking.tunnel_tls import TunnelCredentials

LOGGER = logging.getLogger(__name__)


class AgentTunnelRevokedError(ConnectionError):
    pass


@dataclass(slots=True)
class AgentTunnelClient:
    address: str
    credentials: TunnelCredentials
    expires_at: datetime
    resolve_route: Callable[[str], tuple[str, int] | None]
    streams: set[asyncio.Task[None]] = field(default_factory=set, repr=False)
    connection_id: str = field(default="", init=False)
    accepting: bool = field(default=False, init=False)
    _channel: grpc.aio.Channel | None = field(default=None, init=False)
    _connect: grpc.aio.StreamStreamCall[bytes, bytes] | None = field(default=None, init=False)
    _commands: asyncio.Task[None] | None = field(default=None, init=False)
    _expiry: asyncio.Task[None] | None = field(default=None, init=False)
    _streams: set[asyncio.Task[None]] = field(default_factory=set, init=False)
    _closed: bool = field(default=False, init=False)

    async def start(self) -> None:
        if self._channel is not None:
            raise RuntimeError("Agent tunnel session already started")
        if self.expires_at <= utc_now():
            raise AgentTunnelRevokedError("Agent tunnel certificate expired")
        self._channel = grpc.aio.secure_channel(
            self.address, self.credentials.client().credentials, options=TUNNEL_GRPC_OPTIONS
        )
        self._connect = tunnel_method(self._channel, CONNECT_METHOD)()
        incoming = self._connect.__aiter__()
        try:
            async with asyncio.timeout(TUNNEL_OPEN_TIMEOUT_SECONDS):
                command = TunnelCommand.model_validate_json(await anext(incoming))
            if command.kind is not TunnelCommandKind.Connected or not command.connection_id:
                raise ConnectionError("Gateway did not acknowledge the agent connection")
            self.connection_id = command.connection_id
            self.accepting = True
            self._commands = asyncio.create_task(self._run_commands(incoming))
            self._expiry = asyncio.create_task(self._expire())
        except grpc.aio.AioRpcError as exc:
            await self.close()
            if exc.code() in {grpc.StatusCode.UNAUTHENTICATED, grpc.StatusCode.PERMISSION_DENIED}:
                raise AgentTunnelRevokedError("Gateway rejected the agent identity") from exc
            if exc.code() in {
                grpc.StatusCode.UNAVAILABLE,
                grpc.StatusCode.DEADLINE_EXCEEDED,
                grpc.StatusCode.ABORTED,
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                grpc.StatusCode.CANCELLED,
            }:
                raise ConnectionError("Agent tunnel connection is temporarily unavailable") from exc
            raise
        except BaseException:
            await self.close()
            raise

    async def wait_disconnected(self) -> None:
        if self._commands is None:
            raise RuntimeError("Agent tunnel session has not started")
        try:
            await asyncio.shield(self._commands)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if self._closed and current is not None and not current.cancelling():
                raise ConnectionError("Agent tunnel session expired") from None
            raise

    async def retire(self) -> None:
        self.accepting = False
        if self._connect is not None:
            self._connect.cancel()
        if self._commands is not None:
            self._commands.cancel()
            await asyncio.gather(self._commands, return_exceptions=True)
        # Accepted streams keep this channel until their directional EOFs or certificate expiry.
        await asyncio.gather(*self._streams, return_exceptions=True)
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.accepting = False
        if self._connect is not None:
            self._connect.cancel()
        tasks = [
            task for task in (self._commands, self._expiry, *self._streams) if task is not None
        ]
        current = asyncio.current_task()
        tasks = [task for task in tasks if task is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._channel is not None:
            await self._channel.close()

    def forward_control(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if not self.accepting or len(self.streams) >= TUNNEL_MAX_STREAMS:
            writer.close()
            return
        self._track(asyncio.create_task(self._control(reader, writer)))

    def _track(self, task: asyncio.Task[None]) -> None:
        self._streams.add(task)
        self.streams.add(task)
        task.add_done_callback(self._stream_finished)

    def _stream_finished(self, task: asyncio.Task[None]) -> None:
        self._streams.discard(task)
        self.streams.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        try:
            raise error
        except* (OSError, grpc.RpcError):
            LOGGER.debug("Agent tunnel stream disconnected")
        except* Exception as unexpected:
            LOGGER.error("Agent tunnel stream failed", exc_info=unexpected)

    async def _run_commands(self, incoming: AsyncIterator[bytes]) -> None:
        async def heartbeat() -> None:
            assert self._connect is not None
            while True:
                await self._connect.write(
                    TunnelCommand(kind=TunnelCommandKind.Heartbeat).model_dump_json().encode()
                )
                await asyncio.sleep(TUNNEL_HEARTBEAT_SECONDS)

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            while True:
                async with asyncio.timeout(TUNNEL_HEARTBEAT_SECONDS * 3):
                    message = await anext(incoming, None)
                if message is None:
                    raise ConnectionError("Gateway closed the agent command stream")
                command = TunnelCommand.model_validate_json(message)
                if command.kind is TunnelCommandKind.Drain:
                    return
                if command.kind is TunnelCommandKind.Heartbeat:
                    continue
                if (
                    command.kind is not TunnelCommandKind.Open
                    or command.connection_id != self.connection_id
                    or not command.stream_id
                    or not command.route_id
                ):
                    raise ConnectionError("Gateway sent an invalid agent command")
                if len(self.streams) < TUNNEL_MAX_STREAMS:
                    self._track(asyncio.create_task(self._attach(command)))
        except grpc.aio.AioRpcError as exc:
            if exc.code() in {grpc.StatusCode.UNAUTHENTICATED, grpc.StatusCode.PERMISSION_DENIED}:
                await self._cancel_streams()
                raise AgentTunnelRevokedError("Gateway rejected the agent identity") from exc
            raise
        finally:
            self.accepting = False
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    async def _attach(self, command: TunnelCommand) -> None:
        target = self.resolve_route(command.route_id)
        if target is None or self._channel is None:
            raise ConnectionError("Agent route is not registered")
        async with asyncio.timeout(TUNNEL_OPEN_TIMEOUT_SECONDS):
            reader, writer = await asyncio.open_connection(*target)
        call = tunnel_method(self._channel, ATTACH_METHOD)()
        try:
            await call.write(command.model_dump_json().encode())
            await bridge_socket(GrpcPacketStream(call.__aiter__(), call.write), reader, writer)
            await call.done_writing()
        finally:
            call.cancel()
            writer.close()
            await writer.wait_closed()

    async def _control(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._channel is None:
            writer.close()
            return
        call = tunnel_method(self._channel, CONTROL_METHOD)()
        incoming = call.__aiter__()
        try:
            async with asyncio.timeout(TUNNEL_OPEN_TIMEOUT_SECONDS):
                opened = TunnelPacket.from_wire(await anext(incoming))
                if opened.kind is not TunnelPacketKind.Opened:
                    raise ConnectionError("Gateway did not open the control connection")
            await bridge_socket(GrpcPacketStream(incoming, call.write), reader, writer)
            await call.done_writing()
        finally:
            call.cancel()
            writer.close()
            await writer.wait_closed()

    async def _cancel_streams(self) -> None:
        tasks = tuple(self._streams)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _expire(self) -> None:
        await asyncio.sleep(max(0, (self.expires_at - utc_now()).total_seconds()))
        await self.close()
