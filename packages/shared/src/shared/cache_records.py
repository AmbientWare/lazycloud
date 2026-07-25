from __future__ import annotations

from datetime import datetime

from pydantic import Field

from shared.contracts import ContractModel
from shared.timestamps import utc_now


class CacheEntry(ContractModel):
    key: str
    path: str
    size: int = Field(ge=0)
    sha256: str
    hits: int = Field(default=0, ge=0)
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
