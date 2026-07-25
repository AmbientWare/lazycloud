from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol

from database.types import DatabaseSession

from database import DatabaseClient


class ImagePaths(Protocol):
    def build_path(self, build_id: str) -> Path: ...


class ImageContext(Protocol):
    @property
    def database(self) -> DatabaseClient: ...

    @property
    def paths(self) -> ImagePaths: ...

    def default_workspace_id(self, session: DatabaseSession) -> str: ...


class ImageSecretStore(Protocol):
    def set(
        self,
        name: str,
        value: str,
        *,
        workspace: str = "default",
    ) -> ImageSecretValue: ...


class ImageSecretValue(Protocol):
    value: str
    updated_at: datetime


class ImageSecretReader(Protocol):
    def get(self, name: str, *, workspace: str = "default") -> ImageSecretValue: ...
