from __future__ import annotations

from enum import Enum


class StringEnum(str, Enum):
    """String-valued enum with stable value-based string formatting."""

    __str__ = str.__str__


__all__ = ["StringEnum"]
