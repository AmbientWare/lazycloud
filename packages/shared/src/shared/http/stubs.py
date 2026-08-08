from __future__ import annotations

from datetime import datetime

from pydantic import ConfigDict, Field, JsonValue, field_serializer

from shared.contracts import ContractModel
from shared.deployments import StubKind
from shared.http.base import HttpModel
from shared.serialization import to_json_value
from shared.workload_config import StubTaskPolicy, StubVolumeConfig


def _model_json_object(
    value: ContractModel,
    *,
    exclude_unset: bool = False,
    exclude_none: bool = False,
) -> dict[str, JsonValue]:
    serialized = to_json_value(
        value.model_dump(
            mode="json",
            exclude_unset=exclude_unset,
            exclude_none=exclude_none,
        )
    )
    if not isinstance(serialized, dict):
        raise ValueError("stub configuration model must serialize to a JSON object")
    return serialized


class StubRuntimeConfigResponse(HttpModel):
    """Lenient view over the runtime section of a stored stub config."""

    model_config = ConfigDict(extra="ignore")

    cpu: int | float | None = None
    memory: int | str | None = None
    disk: int | str | None = None
    gpu: str | None = None
    gpu_count: int | None = None
    keep_warm: int | None = Field(default=None, ge=-1)
    timeout_seconds: int | None = None
    concurrency: int | None = Field(default=None, gt=0)


class StubConfigResponse(HttpModel):
    """Lenient view over the heterogeneous stored stub config dict."""

    model_config = ConfigDict(extra="ignore")

    runtime: StubRuntimeConfigResponse | None = None
    inputs: dict[str, JsonValue] = Field(default_factory=dict)
    outputs: dict[str, JsonValue] = Field(default_factory=dict)
    task_policy: StubTaskPolicy = Field(default_factory=StubTaskPolicy)
    python_version: str | None = None
    object_id: str | None = None
    secrets: list[str | dict[str, JsonValue]] = Field(default_factory=list)
    volumes: list[StubVolumeConfig] = Field(default_factory=list)

    @field_serializer("task_policy")
    def serialize_task_policy(self, value: StubTaskPolicy) -> dict[str, JsonValue]:
        return _model_json_object(value, exclude_unset=True)

    @field_serializer("volumes")
    def serialize_volumes(
        self,
        values: list[StubVolumeConfig],
    ) -> list[dict[str, JsonValue]]:
        return [_model_json_object(value, exclude_unset=True) for value in values]


class StubResponse(HttpModel):
    id: str
    workspace_id: str
    name: str
    kind: StubKind = StubKind.Function
    handler: str | None = None
    deployment_id: str | None = None
    app_id: str | None = None
    public: bool = False
    config: StubConfigResponse = Field(default_factory=StubConfigResponse)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class StubListResponse(HttpModel):
    stubs: list[StubResponse] = Field(default_factory=list)


class StubCreateRequest(HttpModel):
    name: str
    workspace: str = "default"
    kind: StubKind = StubKind.Function
    handler: str | None = None
    deployment_id: str | None = None
    app_id: str | None = None
    public: bool = False
    config: StubConfigResponse = Field(default_factory=StubConfigResponse)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class StubConfigUpdateRequest(HttpModel):
    runtime: StubRuntimeConfigResponse | None = None
    inputs: dict[str, JsonValue] | None = None
    outputs: dict[str, JsonValue] | None = None
    task_policy: StubTaskPolicy | None = None
    python_version: str | None = None

    def fields(self) -> dict[str, JsonValue]:
        values: dict[str, JsonValue] = {}
        if self.runtime is not None:
            values["runtime"] = _model_json_object(self.runtime, exclude_none=True)
        if self.inputs is not None:
            values["inputs"] = self.inputs
        if self.outputs is not None:
            values["outputs"] = self.outputs
        if self.task_policy is not None:
            values["task_policy"] = _model_json_object(self.task_policy)
        if self.python_version is not None:
            values["python_version"] = self.python_version
        return values


class StubConfigUpdateResponse(HttpModel):
    stub: StubResponse
    updated_fields: list[str] = Field(default_factory=list)
    message: str


class StubCloneOverrideRequest(HttpModel):
    cpu: int | None = None
    memory: int | None = None
    gpu: str | None = None
    gpu_count: int | None = None


class StubCloneRequest(HttpModel):
    workspace: str = "default"
    overrides: StubCloneOverrideRequest = Field(default_factory=StubCloneOverrideRequest)


class StubUrlResponse(HttpModel):
    stub: StubResponse
    url: str
    external_url: str
    route_kind: StubKind
    deployment_id: str | None = None
    deployment_name: str | None = None
    deployment_version: int | None = None


class PublicStubConfigResponse(StubConfigResponse):
    pass


__all__ = [
    "PublicStubConfigResponse",
    "StubCloneOverrideRequest",
    "StubCloneRequest",
    "StubConfigResponse",
    "StubConfigUpdateRequest",
    "StubConfigUpdateResponse",
    "StubCreateRequest",
    "StubListResponse",
    "StubResponse",
    "StubRuntimeConfigResponse",
    "StubUrlResponse",
]
