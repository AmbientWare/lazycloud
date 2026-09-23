from __future__ import annotations

from shared.enums import StringEnum


class ProviderKind(StringEnum):
    Aws = "aws"


__all__ = ["ProviderKind"]
