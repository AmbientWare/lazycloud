from __future__ import annotations

from datetime import datetime

from pydantic import Field

from shared.app_lifecycle import AppLifecycleState
from shared.http.base import HttpModel
from shared.http.deployments import DeploymentResponse
from shared.http.stubs import StubConfigResponse, StubResponse


class AppCreateRequest(HttpModel):
    name: str
    stub_id: str
    workspace: str = "default"
    version: int = 1
    public: bool = False


class AppActionCapabilitiesResponse(HttpModel):
    can_pause: bool = False
    can_resume: bool = False
    can_delete: bool = False


class AppResponse(HttpModel):
    id: str
    workspace_id: str
    stub_id: str | None = None
    name: str
    version: int = 1
    public: bool = False
    lifecycle_state: AppLifecycleState = AppLifecycleState.Active
    lifecycle_failure: str | None = None
    active: bool = True
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
    actions: AppActionCapabilitiesResponse = Field(default_factory=AppActionCapabilitiesResponse)


class AppSummaryResponse(HttpModel):
    app: AppResponse
    latest_workload: StubResponse | None = None
    latest_deployment: DeploymentResponse | None = None
    workload_kinds: dict[str, int] = Field(default_factory=dict)
    workload_count: int = 0
    active_versions: int = 0
    running_containers: int = 0
    runs_24h: int = 0
    failed_runs_24h: int = 0
    pending_runs_24h: int = 0
    activity_24h: list[int] = Field(default_factory=list)
    failures_24h: list[int] = Field(default_factory=list)
    pending_24h: list[int] = Field(default_factory=list)
    last_deployed_at: datetime | None = None


class AppListResponse(HttpModel):
    data: list[AppResponse] = Field(default_factory=list)
    next: str = ""


class AppSummaryListResponse(HttpModel):
    items: list[AppSummaryResponse] = Field(default_factory=list)


class StubCloneResponse(HttpModel):
    source_stub: StubResponse
    cloned_stub: StubResponse
    app: AppResponse
    copied_config: StubConfigResponse = Field(default_factory=StubConfigResponse)
    copied_objects: list[str] = Field(default_factory=list)


__all__ = [
    "AppActionCapabilitiesResponse",
    "AppCreateRequest",
    "AppListResponse",
    "AppResponse",
    "AppSummaryListResponse",
    "AppSummaryResponse",
    "StubCloneResponse",
]
