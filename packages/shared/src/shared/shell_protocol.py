"""Framed wire protocol for interactive container shells.

The gateway shell tunnel (``/api/v1/shells/id/{stub_id}/{container_id}``) is a
raw bidirectional byte pipe between a shell client and the worker-owned static
container helper mounted into every OCI container. Both ends speak this
framing so control messages (auth, resize, exit) can share the pipe with PTY
bytes without requiring Python or SDK packages in the user image.

Frame layout: 1-byte frame type + 4-byte big-endian payload length + payload.

Client -> server frames: ``Auth`` (JSON ``ShellAuthRequest``, must be first),
``Data`` (stdin bytes), ``Resize`` (JSON ``ShellResizeRequest``).
Server -> client frames: ``Ready`` (auth accepted), ``Error`` (UTF-8 message),
``Data`` (PTY output bytes), ``Exit`` (UTF-8 integer exit code).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, ConfigDict

SHELL_FRAME_HEADER_SIZE = 5
SHELL_FRAME_MAX_PAYLOAD_BYTES = 1024 * 1024
SHELL_DEFAULT_TERM = "xterm-256color"


class ShellFrameType(bytes, Enum):
    """Frame type bytes. ``Data`` carries stdin (client->server) or PTY
    output (server->client); the remaining types are direction-specific."""

    Auth = b"A"
    Data = b"D"
    Resize = b"R"
    Ready = b"O"
    Error = b"E"
    Exit = b"X"


class ShellAuthRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    username: str
    password: str
    term: str = SHELL_DEFAULT_TERM
    cols: int = 80
    rows: int = 24
    probe: bool = False


class ShellResizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    cols: int
    rows: int


@dataclass(frozen=True, slots=True)
class ShellFrame:
    frame_type: bytes
    payload: bytes


class ShellFrameError(ValueError):
    pass


def encode_shell_frame(frame_type: bytes, payload: bytes = b"") -> bytes:
    if len(frame_type) != 1:
        msg = "shell frame type must be a single byte"
        raise ShellFrameError(msg)
    if len(payload) > SHELL_FRAME_MAX_PAYLOAD_BYTES:
        msg = "shell frame payload exceeds maximum size"
        raise ShellFrameError(msg)
    return frame_type + len(payload).to_bytes(4, "big") + payload


class ShellFrameDecoder:
    """Incremental decoder for the framed shell byte stream."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[ShellFrame]:
        self._buffer.extend(data)
        frames: list[ShellFrame] = []
        while True:
            frame = self._next_frame()
            if frame is None:
                return frames
            frames.append(frame)

    def _next_frame(self) -> ShellFrame | None:
        if len(self._buffer) < SHELL_FRAME_HEADER_SIZE:
            return None
        length = int.from_bytes(self._buffer[1:SHELL_FRAME_HEADER_SIZE], "big")
        if length > SHELL_FRAME_MAX_PAYLOAD_BYTES:
            msg = "shell frame payload exceeds maximum size"
            raise ShellFrameError(msg)
        end = SHELL_FRAME_HEADER_SIZE + length
        if len(self._buffer) < end:
            return None
        frame_type = bytes(self._buffer[0:1])
        payload = bytes(self._buffer[SHELL_FRAME_HEADER_SIZE:end])
        del self._buffer[:end]
        return ShellFrame(frame_type=frame_type, payload=payload)


__all__ = [
    "SHELL_DEFAULT_TERM",
    "SHELL_FRAME_HEADER_SIZE",
    "SHELL_FRAME_MAX_PAYLOAD_BYTES",
    "ShellAuthRequest",
    "ShellFrame",
    "ShellFrameDecoder",
    "ShellFrameError",
    "ShellFrameType",
    "ShellResizeRequest",
    "encode_shell_frame",
]
