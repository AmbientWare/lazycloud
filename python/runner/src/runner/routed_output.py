"""Per-attempt output when several attempts share one process.

Threads share stdout and stderr, so the supervisor cannot tell from the pipes
which attempt wrote a line. The runner replaces both streams with ones that
send each attempt's writes as `output` frames under its attempt id, held in a
context variable that each attempt's thread sets. Writes outside an attempt,
such as from threads the handler starts without copying its context, still go
to the real streams.
"""

from __future__ import annotations

import io
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TextIO

from runner.protocol_models import Stream

# Sends one output frame: attempt id, stream, text.
SendOutput = Callable[[str, Stream, str], None]

# A line longer than this is sent without waiting for its newline.
_MAX_BUFFER = 64 << 10
# The largest payload of one output frame.
MAX_FRAME_BYTES = 256 << 10


def utf8_chunks(text: str, limit: int = MAX_FRAME_BYTES) -> list[bytes]:
    """`text` as UTF-8 in chunks of at most `limit` bytes, split between
    characters so each chunk decodes on its own."""

    data = text.encode("utf-8", "replace")
    chunks: list[bytes] = []
    while data:
        cut = min(limit, len(data))
        # Back off from a continuation byte to the start of its character.
        while cut < len(data) and data[cut] & 0xC0 == 0x80:
            cut -= 1
        chunks.append(data[:cut])
        data = data[cut:]
    return chunks


class _AttemptOutput:
    """Line-buffered output of one attempt on both streams."""

    def __init__(self, attempt_id: str, send: SendOutput) -> None:
        self.attempt_id = attempt_id
        self._send = send
        self._buffers = {Stream.stdout: "", Stream.stderr: ""}

    def write(self, stream: Stream, text: str) -> None:
        buffered = self._buffers[stream] + text
        cut = buffered.rfind("\n") + 1
        if cut == 0 and len(buffered) >= _MAX_BUFFER:
            cut = len(buffered)
        if cut:
            self._send(self.attempt_id, stream, buffered[:cut])
        self._buffers[stream] = buffered[cut:]

    def flush(self) -> None:
        for stream, text in self._buffers.items():
            if text:
                self._send(self.attempt_id, stream, text)
                self._buffers[stream] = ""


_ATTEMPT: ContextVar[_AttemptOutput | None] = ContextVar("lazycloud_attempt_output", default=None)


class _RoutedBuffer(io.RawIOBase):
    """The `.buffer` of a routed stream: bytes written are its text."""

    def __init__(self, text: _RoutedStream) -> None:
        self._text = text

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        payload = bytes(data)
        self._text.write(payload.decode("utf-8", "replace"))
        return len(payload)

    def flush(self) -> None:
        self._text.flush()


class _RoutedStream(io.TextIOBase):
    """A text stream that writes to the current attempt, else to `real`."""

    def __init__(self, real: TextIO, stream: Stream) -> None:
        self._real = real
        self._stream = stream
        self._buffer = _RoutedBuffer(self)

    @property
    def buffer(self) -> _RoutedBuffer:
        return self._buffer

    def write(self, text: str) -> int:
        target = _ATTEMPT.get()
        if target is None:
            return self._real.write(text)
        target.write(self._stream, text)
        return len(text)

    def flush(self) -> None:
        target = _ATTEMPT.get()
        if target is None:
            self._real.flush()
        else:
            target.flush()

    def writable(self) -> bool:
        return True

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return self._real.encoding

    def fileno(self) -> int:
        return self._real.fileno()

    def isatty(self) -> bool:
        return False


def install() -> None:
    """Route sys.stdout and sys.stderr through the current attempt. Call it
    before user code loads, so loggers it configures hold routed streams."""

    sys.stdout = _RoutedStream(sys.stdout, Stream.stdout)
    sys.stderr = _RoutedStream(sys.stderr, Stream.stderr)


@contextmanager
def attempt_output(attempt_id: str, send: SendOutput) -> Iterator[None]:
    """Send the enclosed writes as the attempt's output, flushed on exit."""

    output = _AttemptOutput(attempt_id, send)
    token = _ATTEMPT.set(output)
    try:
        yield
    finally:
        _ATTEMPT.reset(token)
        output.flush()


__all__ = ["MAX_FRAME_BYTES", "SendOutput", "attempt_output", "install", "utf8_chunks"]
