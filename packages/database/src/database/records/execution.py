from __future__ import annotations

from datetime import datetime

from pydantic import Field
from shared.contracts import ContractModel


class PodUrlRecord(ContractModel):
    id: str
    container_id: str
    port: int = Field(ge=1, le=65535)
    url: str
    created_at: datetime
    updated_at: datetime


__all__ = ["PodUrlRecord"]
