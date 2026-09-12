from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import grpc
import grpc.aio
from shared.http.agent_tunnel import (
    TUNNEL_CHUNK_BYTES,
    TUNNEL_MAX_STREAMS,
    TunnelPacket,
    TunnelPacketKind,
)

CONNECT_METHOD = "/lazycloud.AgentTunnel/Connect"
ATTACH_METHOD = "/lazycloud.AgentTunnel/Attach"
ROUTE_METHOD = "/lazycloud.AgentTunnel/Route"
CONTROL_METHOD = "/lazycloud.AgentTunnel/Control"


TUNNEL_GRPC_OPTIONS: tuple[tuple[str, int], ...] = (
    ("grpc.max_receive_message_length", TUNNEL_CHUNK_BYTES + 1),
    ("grpc.max_send_message_length", TUNNEL_CHUNK_BYTES + 1),
    ("grpc.max_concurrent_streams", TUNNEL_MAX_STREAMS + 1),
    ("grpc.http2.bdp_probe", 0),
    ("grpc.http2.write_buffer_size", TUNNEL_CHUNK_BYTES),
    ("grpc.enable_retries", 0),
)


def tunnel_method(
    channel: grpc.aio.Channel, name: str
) -> grpc.aio.StreamStreamMultiCallable[bytes, bytes]:
    return channel.stream_stream(name)


class PacketStream(Protocol):
    async def send_message(self, message: bytes) -> None: ...

    async def recv_message(self) -> bytes | None: ...


@dataclass(slots=True)
class GrpcPacketStream:
    incoming: AsyncIterator[bytes]
    write: Callable[[bytes], Awaitable[None]]

    async def send_message(self, message: bytes) -> None:
        await self.write(message)

    async def recv_message(self) -> bytes | None:
        return await anext(self.incoming, None)


async def copy_packets(source: PacketStream, destination: PacketStream) -> None:
    while (message := await source.recv_message()) is not None:
        packet = TunnelPacket.from_wire(message)
        if packet.kind is TunnelPacketKind.Opened:
            raise ValueError("Unexpected stream-open acknowledgement")
        await destination.send_message(message)
        if packet.kind is TunnelPacketKind.Eof:
            return
    raise ConnectionError("Tunnel closed without a directional EOF")


async def bridge_packets(left: PacketStream, right: PacketStream) -> None:
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(copy_packets(left, right))
        tasks.create_task(copy_packets(right, left))


async def bridge_socket(
    stream: PacketStream, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    async def send() -> None:
        while data := await reader.read(TUNNEL_CHUNK_BYTES):
            await stream.send_message(TunnelPacket(kind=TunnelPacketKind.Data, data=data).to_wire())
        await stream.send_message(TunnelPacket(kind=TunnelPacketKind.Eof).to_wire())

    async def receive() -> None:
        while (message := await stream.recv_message()) is not None:
            packet = TunnelPacket.from_wire(message)
            if packet.kind is TunnelPacketKind.Eof:
                writer.write_eof()
                await writer.drain()
                return
            if packet.kind is not TunnelPacketKind.Data:
                raise ValueError("Unexpected stream-open acknowledgement")
            writer.write(packet.data)
            await writer.drain()
        raise ConnectionError("Tunnel closed without a directional EOF")

    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(send())
            tasks.create_task(receive())
    except BaseException:
        writer.transport.abort()
        raise
    finally:
        writer.close()
        await writer.wait_closed()
