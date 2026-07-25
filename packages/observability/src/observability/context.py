from __future__ import annotations

from typing import Protocol

from database import DatabaseClient


class ObservabilityContext(Protocol):
    @property
    def database(self) -> DatabaseClient: ...
