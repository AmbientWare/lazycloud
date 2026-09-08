from __future__ import annotations

from shared.enums import StringEnum


class ProviderKind(StringEnum):
    Aws = "aws"
    Hetzner = "hetzner"
    Hyperstack = "hyperstack"
    Ovh = "ovh"


__all__ = ["ProviderKind"]
