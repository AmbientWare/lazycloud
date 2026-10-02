from __future__ import annotations

from datetime import datetime

from pydantic import Field

from lazycloud._shared.timestamps import utc_now
from lazycloud.contracts import ContractModel


class SecretRecord(ContractModel):
    """Stored secret value; representations never reveal the value."""

    name: str
    value: str = Field(repr=False)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def masked(self) -> str:
        if not self.value:
            return ""
        return "*" * 8
