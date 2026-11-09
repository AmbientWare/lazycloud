from __future__ import annotations

from lazycloud._shared.enums import StringEnum


class FunctionPayloadEncoding(StringEnum):
    Json = "json"
    Cloudpickle = "cloudpickle"


__all__ = ["FunctionPayloadEncoding"]
