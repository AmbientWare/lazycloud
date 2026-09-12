from __future__ import annotations

from enum import IntEnum, StrEnum
from uuid import UUID

from pydantic import Field, field_validator

from shared.http.base import HttpModel

TUNNEL_CHUNK_BYTES = 64 * 1024
TUNNEL_HEARTBEAT_SECONDS = 2.0
TUNNEL_OPEN_TIMEOUT_SECONDS = 5.0
TUNNEL_MAX_STREAMS = 128


class TunnelCommandKind(StrEnum):
    Connected = "connected"
    Heartbeat = "heartbeat"
    Open = "open"
    Drain = "drain"


class TunnelCommand(HttpModel):
    kind: TunnelCommandKind
    connection_id: str = ""
    stream_id: str = ""
    route_id: str = ""


class TunnelRouteRequest(HttpModel):
    workspace_id: str
    enrollment_id: str
    route_id: str = Field(min_length=1, max_length=256)

    @field_validator("workspace_id", "enrollment_id")
    @classmethod
    def normalize_workspace(cls, value: str) -> str:
        return str(UUID(value))


class TunnelPacketKind(IntEnum):
    Data = 0
    Eof = 1
    Opened = 2


class TunnelPacket(HttpModel):
    kind: TunnelPacketKind
    data: bytes = Field(default=b"", repr=False, max_length=TUNNEL_CHUNK_BYTES)

    def to_wire(self) -> bytes:
        if self.kind is not TunnelPacketKind.Data and self.data:
            raise ValueError("Only tunnel data packets can contain a body")
        return bytes((self.kind,)) + self.data

    @classmethod
    def from_wire(cls, data: bytes) -> TunnelPacket:
        if not data or len(data) > TUNNEL_CHUNK_BYTES + 1:
            raise ValueError("Invalid tunnel packet length")
        packet = cls(kind=TunnelPacketKind(data[0]), data=data[1:])
        if packet.kind is not TunnelPacketKind.Data and packet.data:
            raise ValueError("Only tunnel data packets can contain a body")
        return packet
