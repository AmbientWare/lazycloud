from __future__ import annotations

from datetime import datetime

from pydantic import Field, JsonValue

from shared.contracts import ContractModel
from shared.enums import StringEnum
from shared.timestamps import utc_now


class ProviderKind(StringEnum):
    Aws = "aws"
    Hetzner = "hetzner"


class ProviderConfig(ContractModel):
    name: str
    kind: ProviderKind = ProviderKind.Aws
    enabled: bool = True
    priority: int = 100
    config: dict[str, JsonValue] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


__all__ = ["ProviderConfig", "ProviderKind"]
