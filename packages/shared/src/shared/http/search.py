from __future__ import annotations

from pydantic import model_validator

from shared.enums import StringEnum
from shared.http.base import HttpModel


class ResourceSearchKind(StringEnum):
    App = "app"
    Workload = "workload"
    Task = "task"
    Sandbox = "sandbox"


class ResourceSearchResult(HttpModel):
    kind: ResourceSearchKind
    id: str
    name: str
    app_id: str | None = None

    @model_validator(mode="after")
    def workload_has_app(self) -> ResourceSearchResult:
        if self.kind is ResourceSearchKind.Workload and not self.app_id:
            raise ValueError("A workload search result must name its app")
        return self


class ResourceSearchResponse(HttpModel):
    data: list[ResourceSearchResult]
    next: str = ""


__all__ = ["ResourceSearchKind", "ResourceSearchResponse", "ResourceSearchResult"]
