from __future__ import annotations

from datetime import datetime

from pydantic import Field, JsonValue

from shared.contracts import ContractModel
from shared.enums import StringEnum
from shared.timestamps import utc_now


class EventLevel(StringEnum):
    Info = "info"
    Warning = "warning"
    Error = "error"


class Event(ContractModel):
    id: str
    action: str
    level: EventLevel = EventLevel.Info
    resource_type: str
    resource_id: str
    message: str
    data: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


__all__ = ["Event", "EventLevel"]
