from __future__ import annotations

import pytest
from shared.shell_protocol import (
    ShellFrame,
    ShellFrameDecoder,
    ShellFrameError,
    ShellFrameType,
    encode_shell_frame,
)


def test_shell_frame_codec_roundtrip() -> None:
    decoder = ShellFrameDecoder()
    encoded = encode_shell_frame(ShellFrameType.Auth.value, b'{"username":"u"}')
    encoded += encode_shell_frame(ShellFrameType.Data.value, b"ls -la\n")

    frames = decoder.feed(encoded)

    assert [frame.frame_type for frame in frames] == [b"A", b"D"]
    assert frames[1].payload == b"ls -la\n"


def test_shell_frame_codec_handles_split_and_partial_delivery() -> None:
    decoder = ShellFrameDecoder()
    encoded = encode_shell_frame(ShellFrameType.Data.value, b"hello world")

    assert decoder.feed(encoded[:3]) == []
    assert decoder.feed(encoded[3:7]) == []
    assert decoder.feed(encoded[7:]) == [ShellFrame(frame_type=b"D", payload=b"hello world")]


def test_shell_frame_codec_rejects_oversized_frames() -> None:
    decoder = ShellFrameDecoder()
    header = b"D" + (2 * 1024 * 1024).to_bytes(4, "big")

    with pytest.raises(ShellFrameError):
        decoder.feed(header)
