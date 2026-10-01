from __future__ import annotations

from datetime import datetime

from pydantic import ConfigDict, Field, JsonValue, field_serializer

from shared.contracts import ContractModel
from shared.deployment_records import CpuRequest, MemoryRequest
from shared.deployments import StubKind
from shared.http.base import HttpModel
from shared.placement import AvailabilityZone, ProductRegion
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
    region: ProductRegion | None = None
    availability_zone: AvailabilityZone = ""
    preemptible: bool = False

    cpu: CpuRequest | None = None
    memory: MemoryRequest | None = None
    disk: int | str | None = None
    gpu: list[str] = Field(default_factory=list)
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


__all__ = [
    "StubConfigResponse",
    "StubResponse",
    "StubRuntimeConfigResponse",
]
