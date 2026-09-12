from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from uuid import uuid4

import grpc
import grpc.aio
from compute.tunnel_authority import AgentTunnelAuthority
from coordination.agent_connections import RedisAgentConnectionDirectory
from cryptography import x509
from identity.tunnel_certificates import (
    agent_identity_from_verified_certificate,
    service_identity_from_verified_certificate,
)
from redis.exceptions import RedisError
from shared.agent_connections import AgentConnectionRecord
from shared.errors import DomainError
from shared.http.agent_identity import AgentTunnelIdentity, TunnelServiceRole
from shared.http.agent_tunnel import (
    TUNNEL_HEARTBEAT_SECONDS,
    TUNNEL_MAX_STREAMS,
    TUNNEL_OPEN_TIMEOUT_SECONDS,
    TunnelCommand,
    TunnelCommandKind,
    TunnelPacket,
    TunnelPacketKind,
    TunnelRouteRequest,
)
from sqlalchemy.exc import SQLAlchemyError

from networking.dialer import split_host_port
from networking.tunnel_protocol import (
    ATTACH_METHOD,
    CONNECT_METHOD,
    CONTROL_METHOD,
    ROUTE_METHOD,
    TUNNEL_GRPC_OPTIONS,
    GrpcPacketStream,
    bridge_packets,
    bridge_socket,
)
from networking.tunnel_tls import TunnelCredentials

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class _PendingStream:
    stream: asyncio.Future[GrpcPacketStream]
    completed: asyncio.Future[None]
    owner: asyncio.Task[None]


@dataclass(slots=True)
class _AgentSession:
    record: AgentConnectionRecord
    context: grpc.aio.ServicerContext[bytes, bytes]
    commands: asyncio.Queue[TunnelCommand] = field(default_factory=lambda: asyncio.Queue(128))
    pending: dict[str, _PendingStream] = field(default_factory=dict)
    streams: dict[asyncio.Task[None], grpc.aio.ServicerContext[bytes, bytes]] = field(
        default_factory=dict
    )
    connected: bool = True


@dataclass(slots=True)
class AgentTunnelGateway:
    authority: AgentTunnelAuthority
    directory: RedisAgentConnectionDirectory
    address: str
    control_address: str
    gateway_id: str = field(default_factory=lambda: str(uuid4()))
    accepting: bool = True
    _sessions: dict[str, _AgentSession] = field(default_factory=dict, init=False)
    _maintenance: set[asyncio.Task[None]] = field(default_factory=set, init=False)

    def server(self, credentials: TunnelCredentials, *, listen_address: str) -> grpc.aio.Server:
        server = grpc.aio.server(options=TUNNEL_GRPC_OPTIONS, maximum_concurrent_rpcs=4096)
        server.add_generic_rpc_handlers(
            (
                grpc.method_handlers_generic_handler(
                    "lazycloud.AgentTunnel",
                    {
                        name.rsplit("/", 1)[1]: grpc.stream_stream_rpc_method_handler(
                            partial(self._handle, handler)
                        )
                        for name, handler in (
                            (CONNECT_METHOD, self.connect),
                            (ATTACH_METHOD, self.attach),
                            (ROUTE_METHOD, self.route),
                            (CONTROL_METHOD, self.control),
                        )
                    },
                ),
            )
        )
        if not server.add_secure_port(listen_address, credentials.server()):
            raise OSError("Agent tunnel listener could not bind its address")
        return server

    async def connect(
        self, incoming: AsyncIterator[bytes], context: grpc.aio.ServicerContext[bytes, bytes]
    ) -> None:
        if not self.accepting:
            await context.abort(grpc.StatusCode.UNAVAILABLE, "Gateway is draining")
        identity, expiry = await self._agent(context)
        stream = GrpcPacketStream(incoming, context.write)
        previous = await asyncio.to_thread(
            self.directory.get, identity.workspace_id, identity.enrollment_id
        )
        record = AgentConnectionRecord(
            identity=identity,
            gateway_id=self.gateway_id,
            connection_id=str(uuid4()),
            gateway_address=self.address,
            expires_at=expiry,
        )
        claimed = await asyncio.to_thread(
            self.directory.claim, record, previous.connection_id if previous else None
        )
        if not claimed:
            await context.abort(grpc.StatusCode.ABORTED, "Agent connection ownership changed")
        session = _AgentSession(record, context)
        self._sessions[record.connection_id] = session
        maintenance = asyncio.create_task(self._maintain(session))
        self._maintenance.add(maintenance)
        maintenance.add_done_callback(self._maintenance_finished)

        async def receive() -> None:
            while session.connected:
                async with asyncio.timeout(TUNNEL_HEARTBEAT_SECONDS * 3):
                    message = await stream.recv_message()
                if message is None:
                    return
                command = TunnelCommand.model_validate_json(message)
                if command.kind is not TunnelCommandKind.Heartbeat:
                    raise ValueError("Agent control stream accepts only heartbeats")
                session.commands.put_nowait(TunnelCommand(kind=TunnelCommandKind.Heartbeat))

        async def send() -> None:
            while session.connected:
                command = await session.commands.get()
                await stream.send_message(command.model_dump_json().encode())
                if command.kind is TunnelCommandKind.Drain:
                    return

        receiving = asyncio.create_task(receive())
        sending = asyncio.create_task(send())
        try:
            await stream.send_message(
                TunnelCommand(kind=TunnelCommandKind.Connected, connection_id=record.connection_id)
                .model_dump_json()
                .encode()
            )
            done, _ = await asyncio.wait((receiving, sending), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            session.connected = False
            receiving.cancel()
            sending.cancel()
            await asyncio.gather(receiving, sending, return_exceptions=True)
            await self._release(record)

    async def attach(
        self, incoming: AsyncIterator[bytes], context: grpc.aio.ServicerContext[bytes, bytes]
    ) -> None:
        identity, expiry = await self._agent(context)
        stream = GrpcPacketStream(incoming, context.write)
        async with asyncio.timeout(TUNNEL_OPEN_TIMEOUT_SECONDS):
            message = await stream.recv_message()
        command = TunnelCommand.model_validate_json(message or b"")
        session = self._sessions.get(command.connection_id)
        if session is None or session.record.identity != identity:
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED, "Agent connection is unavailable"
            )
            return
        pending = session.pending.get(command.stream_id)
        if pending is None or pending.stream.done():
            await context.abort(grpc.StatusCode.NOT_FOUND, "Requested stream is unavailable")
            return
        pending.stream.set_result(stream)
        try:
            async with asyncio.timeout(max(0, (expiry - datetime.now(UTC)).total_seconds())):
                await asyncio.shield(pending.completed)
        finally:
            if not pending.completed.done():
                pending.owner.cancel()

    async def route(
        self, incoming: AsyncIterator[bytes], context: grpc.aio.ServicerContext[bytes, bytes]
    ) -> None:
        expiry = await self._service(context)
        stream = GrpcPacketStream(incoming, context.write)
        async with asyncio.timeout(TUNNEL_OPEN_TIMEOUT_SECONDS):
            request = TunnelRouteRequest.model_validate_json(await stream.recv_message() or b"")
        record = await asyncio.to_thread(
            self.directory.get, request.workspace_id, request.enrollment_id
        )
        session = self._sessions.get(record.connection_id) if record else None
        if session is None or not session.connected:
            await context.abort(grpc.StatusCode.UNAVAILABLE, "Agent connection is unavailable")
            return
        try:
            async with asyncio.timeout(TUNNEL_OPEN_TIMEOUT_SECONDS):
                await asyncio.to_thread(
                    self.authority.authorize_route, session.record.identity, request.route_id
                )
        except DomainError as exc:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, exc.message)
        owner = _current_task()
        if len(session.streams) >= TUNNEL_MAX_STREAMS:
            await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "Agent stream limit reached")
        session.streams[owner] = context
        loop = asyncio.get_running_loop()
        pending = _PendingStream(loop.create_future(), loop.create_future(), owner)
        stream_id = str(uuid4())
        session.pending[stream_id] = pending
        try:
            session.commands.put_nowait(
                TunnelCommand(
                    kind=TunnelCommandKind.Open,
                    connection_id=session.record.connection_id,
                    stream_id=stream_id,
                    route_id=request.route_id,
                )
            )
            async with asyncio.timeout(TUNNEL_OPEN_TIMEOUT_SECONDS):
                agent = await pending.stream
            await stream.send_message(TunnelPacket(kind=TunnelPacketKind.Opened).to_wire())
            async with asyncio.timeout(max(0, (expiry - datetime.now(UTC)).total_seconds())):
                await bridge_packets(stream, agent)
        finally:
            session.pending.pop(stream_id, None)
            session.streams.pop(owner, None)
            if not pending.completed.done():
                pending.completed.set_result(None)

    async def control(
        self, incoming: AsyncIterator[bytes], context: grpc.aio.ServicerContext[bytes, bytes]
    ) -> None:
        identity, expiry = await self._agent(context)
        record = await asyncio.to_thread(
            self.directory.get, identity.workspace_id, identity.enrollment_id
        )
        session = self._sessions.get(record.connection_id) if record else None
        if session is None or not session.connected or session.record.identity != identity:
            await context.abort(grpc.StatusCode.UNAVAILABLE, "Agent connection is unavailable")
            return
        if len(session.streams) >= TUNNEL_MAX_STREAMS:
            await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "Agent stream limit reached")
        owner = _current_task()
        session.streams[owner] = context
        try:
            host, port = split_host_port(self.control_address)
            async with asyncio.timeout(TUNNEL_OPEN_TIMEOUT_SECONDS):
                reader, writer = await asyncio.open_connection(host, port)
            try:
                stream = GrpcPacketStream(incoming, context.write)
                await stream.send_message(TunnelPacket(kind=TunnelPacketKind.Opened).to_wire())
                async with asyncio.timeout(max(0, (expiry - datetime.now(UTC)).total_seconds())):
                    await bridge_socket(stream, reader, writer)
            finally:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()
        finally:
            session.streams.pop(owner, None)

    async def drain(self) -> None:
        self.accepting = False
        for session in tuple(self._sessions.values()):
            if session.connected:
                self._request_drain(session)

    async def close(self) -> None:
        await self.drain()
        for task in tuple(self._maintenance):
            task.cancel()
        await asyncio.gather(*self._maintenance, return_exceptions=True)

    def _maintenance_finished(self, task: asyncio.Task[None]) -> None:
        self._maintenance.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            LOGGER.error("Agent tunnel session cleanup failed", exc_info=error)

    async def _maintain(self, session: _AgentSession) -> None:
        status = grpc.StatusCode.UNAVAILABLE
        try:
            while session.connected or session.streams:
                if datetime.now(UTC) >= session.record.expires_at:
                    status = grpc.StatusCode.UNAUTHENTICATED
                    return
                async with asyncio.timeout(TUNNEL_HEARTBEAT_SECONDS):
                    await asyncio.to_thread(self.authority.validate_agent, session.record.identity)
                    if session.connected and not await asyncio.to_thread(
                        self.directory.renew, session.record
                    ):
                        self._request_drain(session)
                await asyncio.sleep(TUNNEL_HEARTBEAT_SECONDS)
        except (DomainError, OSError, TimeoutError, RedisError, SQLAlchemyError) as exc:
            if isinstance(exc, DomainError):
                status = grpc.StatusCode.PERMISSION_DENIED
            LOGGER.warning("Agent tunnel authorization closed the session: %s", type(exc).__name__)
        finally:
            session.connected = False
            streams = tuple(session.streams.items())
            try:
                for _, context in streams:
                    await _end_session_rpc(context, status)
                await _end_session_rpc(session.context, status)
            finally:
                for task, _ in streams:
                    task.cancel()
                self._sessions.pop(session.record.connection_id, None)
                await self._release(session.record)

    async def _release(self, record: AgentConnectionRecord) -> None:
        try:
            await asyncio.to_thread(self.directory.release, record)
        except RedisError:
            LOGGER.warning(
                "Agent connection lease cleanup could not reach Redis; lease expires automatically"
            )

    async def _handle(
        self,
        handler: Callable[
            [AsyncIterator[bytes], grpc.aio.ServicerContext[bytes, bytes]], Awaitable[None]
        ],
        incoming: AsyncIterator[bytes],
        context: grpc.aio.ServicerContext[bytes, bytes],
    ) -> None:
        try:
            await handler(incoming, context)
        except DomainError as exc:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, exc.message)
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Invalid tunnel request")
        except asyncio.QueueFull:
            await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "Agent command queue is full")
        except (OSError, TimeoutError, RedisError, SQLAlchemyError):
            await context.abort(grpc.StatusCode.UNAVAILABLE, "Tunnel connection is unavailable")
        except ExceptionGroup as exc:
            _, unexpected = exc.split((OSError, TimeoutError, grpc.RpcError))
            if unexpected is not None:
                raise unexpected from exc
            await context.abort(grpc.StatusCode.UNAVAILABLE, "Tunnel stream was interrupted")

    @staticmethod
    def _request_drain(session: _AgentSession) -> None:
        while not session.commands.empty():
            session.commands.get_nowait()
        for pending in session.pending.values():
            if not pending.stream.done():
                pending.owner.cancel()
        session.commands.put_nowait(TunnelCommand(kind=TunnelCommandKind.Drain))

    async def _agent(
        self, context: grpc.aio.ServicerContext[bytes, bytes]
    ) -> tuple[AgentTunnelIdentity, datetime]:
        certificate = await _certificate(context)
        try:
            identity = agent_identity_from_verified_certificate(certificate)
            async with asyncio.timeout(TUNNEL_HEARTBEAT_SECONDS):
                await asyncio.to_thread(self.authority.validate_agent, identity)
        except (ValueError, DomainError) as exc:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, str(exc))
            raise
        return identity, x509.load_pem_x509_certificate(certificate.encode()).not_valid_after_utc

    async def _service(self, context: grpc.aio.ServicerContext[bytes, bytes]) -> datetime:
        certificate = await _certificate(context)
        try:
            identity = service_identity_from_verified_certificate(certificate)
            if identity.role is not TunnelServiceRole.ControlPlane:
                raise ValueError("Backend routes require a control plane identity")
        except ValueError as exc:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, str(exc))
        return x509.load_pem_x509_certificate(certificate.encode()).not_valid_after_utc


async def _certificate(context: grpc.aio.ServicerContext[bytes, bytes]) -> str:
    certificates = tuple(context.auth_context().get("x509_pem_cert", ()))
    if len(certificates) != 1:
        await context.abort(
            grpc.StatusCode.UNAUTHENTICATED, "A verified peer certificate is required"
        )
    certificate = certificates[0].decode("ascii")
    if x509.load_pem_x509_certificate(certificate.encode()).not_valid_after_utc <= datetime.now(
        UTC
    ):
        await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Peer certificate expired")
    return certificate


async def _end_session_rpc(
    context: grpc.aio.ServicerContext[bytes, bytes], status: grpc.StatusCode
) -> None:
    if context.done() or context.cancelled():
        return
    with suppress(grpc.aio.AbortError):
        await context.abort(status, "Agent session is no longer available")


def _current_task() -> asyncio.Task[None]:
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("Tunnel stream requires an active task")
    return task
