from __future__ import annotations

from shared.enums import StringEnum


class ProviderKind(StringEnum):
    Aws = "aws"
    Hetzner = "hetzner"


__all__ = ["ProviderKind"]
