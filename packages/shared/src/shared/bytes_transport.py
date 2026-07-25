from __future__ import annotations

import base64
from typing import TypeVar

from shared.contracts import ContractModel

_EncodedBytesBodyT = TypeVar("_EncodedBytesBodyT", bound="EncodedBytesBody")


class EncodedBytesBody(ContractModel):
    value_base64: str = ""

    @classmethod
    def from_bytes(cls: type[_EncodedBytesBodyT], value: bytes) -> _EncodedBytesBodyT:
        return cls(value_base64=encode_bytes(value))

    def bytes_value(self) -> bytes:
        return decode_bytes(self.value_base64)


def encode_bytes(value: bytes) -> str:
    if not value:
        return ""
    return base64.b64encode(value).decode("ascii")


def decode_bytes(value: str) -> bytes:
    if not value:
        return b""
    return base64.b64decode(value.encode("ascii"), validate=True)


__all__ = [
    "EncodedBytesBody",
    "decode_bytes",
    "encode_bytes",
]
