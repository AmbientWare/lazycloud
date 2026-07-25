from __future__ import annotations

from datetime import datetime

from pydantic import Field

from shared.contracts import ContractModel
from shared.timestamps import utc_now


class VolumeRecord(ContractModel):
    id: str
    name: str
    created_at: datetime = Field(default_factory=utc_now)
