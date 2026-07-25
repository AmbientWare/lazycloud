from __future__ import annotations

import io
import json
import pickle
from typing import Any

import cloudpickle

_PICKLE_PROTOCOL_MARKER = 0x80


def cloudpickle_bytes(value: Any) -> bytes:
    """Serialize an arbitrary Python value with the SDK's cloudpickle runtime."""

    stream = io.BytesIO()
    pickler = cloudpickle.CloudPickler(stream)
    pickle.Pickler.dump(pickler, value)
    return stream.getvalue()


def encode_value(value: Any) -> bytes:
    """Serialize a collection value JSON-first so stored data stays inspectable.

    Values that survive a JSON round-trip unchanged are stored as UTF-8 JSON;
    everything else (custom classes, tuples, non-string keys, bytes) falls back
    to cloudpickle. Pickle streams always begin with the 0x80 protocol marker,
    which JSON text never does, so `decode_value` needs no format tag.
    """
    try:
        encoded = json.dumps(value, separators=(",", ":"))
    except (TypeError, ValueError):
        return cloudpickle_bytes(value)
    if json.loads(encoded) != value or isinstance(value, tuple):
        return cloudpickle_bytes(value)
    return encoded.encode("utf-8")


def decode_value(data: bytes) -> Any:
    if not data:
        return None
    if data[0] == _PICKLE_PROTOCOL_MARKER:
        return pickle.loads(data)
    return json.loads(data.decode("utf-8"))


__all__ = ["cloudpickle_bytes", "decode_value", "encode_value"]
