from __future__ import annotations

from typing import Literal

from shared.bytes_transport import EncodedBytesBody
from shared.enums import StringEnum


class FunctionResultPresentationEncoding(StringEnum):
    Bytes = "bytes"


class FunctionBytesResultPresentation(EncodedBytesBody):
    encoding: Literal[FunctionResultPresentationEncoding.Bytes] = (
        FunctionResultPresentationEncoding.Bytes
    )


__all__ = [
    "FunctionBytesResultPresentation",
    "FunctionResultPresentationEncoding",
]
