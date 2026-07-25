from __future__ import annotations

from typing import Protocol

from pydantic import JsonValue
from shared.events import Event, EventLevel


class ControlEventEmitter(Protocol):
    def emit(
        self,
        action: str,
        *,
        resource_type: str,
        resource_id: str,
        message: str,
        level: EventLevel = EventLevel.Info,
        data: dict[str, JsonValue] | None = None,
        workspace_id: str | None = None,
    ) -> Event: ...
