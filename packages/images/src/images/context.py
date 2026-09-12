from __future__ import annotations

from datetime import datetime
from typing import Protocol

from database.types import DatabaseSession

from database import DatabaseClient


class ImageContext(Protocol):
    @property
    def database(self) -> DatabaseClient: ...

    def default_workspace_id(self, session: DatabaseSession) -> str: ...


class ImageSecretValue(Protocol):
    value: str
    updated_at: datetime


class ImageSecretReader(Protocol):
    def get(self, name: str, *, workspace: str = "default") -> ImageSecretValue: ...
