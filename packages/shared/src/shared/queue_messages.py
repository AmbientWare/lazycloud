from __future__ import annotations

from datetime import datetime

from pydantic import Field, JsonValue

from shared.contracts import ContractModel
from shared.timestamps import utc_now


class QueueMessage(ContractModel):
    """Durable JSON message a queue consumer claims."""

    id: str
    queue: str
    body: JsonValue
    attempts: int = Field(default=0, ge=0)
    available_at: datetime = Field(default_factory=utc_now)
    leased_until: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
