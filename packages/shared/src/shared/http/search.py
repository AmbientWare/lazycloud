from __future__ import annotations

from pydantic import model_validator

from shared.deployments import DeploymentKind
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
    workload_kind: DeploymentKind | None = None

    @model_validator(mode="after")
    def workload_has_app(self) -> ResourceSearchResult:
        if self.kind is ResourceSearchKind.Workload and (
            not self.app_id or self.workload_kind is None
        ):
            raise ValueError("A workload search result must name its app and deployment kind")
        return self


class ResourceSearchResponse(HttpModel):
    data: list[ResourceSearchResult]
    next: str = ""


__all__ = ["ResourceSearchKind", "ResourceSearchResponse", "ResourceSearchResult"]
