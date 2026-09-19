from __future__ import annotations

from datetime import datetime

from pydantic import (
    Field,
    JsonValue,
    SerializationInfo,
    TypeAdapter,
    computed_field,
    field_serializer,
)
from shared.app_lifecycle import (
    AppDeploymentIntentTarget,
    AppLifecycleState,
    AppLifecycleTarget,
)
from shared.contracts import ContractModel
from shared.deployment_records import CpuRequest
from shared.deployments import StubKind
from shared.timestamps import utc_now
from shared.workload_config import (
    StubAutoscalerConfig,
    StubConfig,
    StubTaskPolicy,
)

_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


class StubRecord(ContractModel):
    id: str
    workspace_id: str
    name: str
    kind: StubKind = StubKind.Function
    handler: str | None = None
    deployment_id: str | None = None
    app_id: str | None = None
    public: bool = False
    config: StubConfig = Field(default_factory=StubConfig)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_serializer("config")
    def serialize_config(
        self,
        config: StubConfig,
        _info: SerializationInfo,
    ) -> dict[str, JsonValue]:
        config_json = config.model_dump_json(exclude_unset=True)
        return _JSON_OBJECT_ADAPTER.validate_json(config_json)


class AutoscalingStubRuntimeConfig(ContractModel):
    cpu: CpuRequest | None = None
    cpu_millicores: int = Field(default=0, ge=0)
    gpu: list[str] = Field(default_factory=list)
    gpu_count: int = Field(default=0, ge=0)
    timeout_seconds: int | float | None = Field(default=None, ge=0)
    keep_warm: int = Field(default=0, ge=-1)
    workspace_gpu_quota: int = Field(default=0, ge=0)
    workspace_cpu_quota_millicores: int = Field(default=0, ge=0)


class AutoscalingStubConfig(ContractModel):
    runtime: AutoscalingStubRuntimeConfig = Field(default_factory=AutoscalingStubRuntimeConfig)
    autoscaler: StubAutoscalerConfig = Field(default_factory=StubAutoscalerConfig)
    task_policy: StubTaskPolicy = Field(default_factory=StubTaskPolicy)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class AutoscalingStubRecord(ContractModel):
    id: str
    workspace_id: str
    kind: StubKind
    deployment_id: str | None = None
    app_id: str | None = None
    config: AutoscalingStubConfig = Field(default_factory=AutoscalingStubConfig)


type AutoscalingStub = AutoscalingStubRecord | StubRecord


class AppRecord(ContractModel):
    id: str
    workspace_id: str
    stub_id: str | None = None
    name: str
    version: int = 1
    public: bool = False
    lifecycle_state: AppLifecycleState = AppLifecycleState.Active
    lifecycle_revision: int = 0
    lifecycle_target: AppLifecycleTarget | None = None
    lifecycle_operation_id: str | None = None
    lifecycle_failure: str | None = None
    reconcile_claim_id: str | None = None
    reconcile_claimed_at: datetime | None = None
    reconcile_attempt_count: int = 0
    lifecycle_event_id: str | None = None
    lifecycle_event_created_at: datetime | None = None
    lifecycle_change_published_at: datetime | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    deleted_at: datetime | None = None

    @computed_field
    @property
    def active(self) -> bool:
        return self.lifecycle_state is AppLifecycleState.Active


class AppDeploymentIntentRecord(ContractModel):
    app_id: str
    deployment_id: str
    operation_revision: int
    target: AppDeploymentIntentTarget
    event_id: str | None = None
    event_created_at: datetime | None = None
    workspace_change_published_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AppContainerShutdownIntentRecord(ContractModel):
    app_id: str
    container_id: str
    worker_id: str = ""
    operation_revision: int
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


__all__ = [
    "AppContainerShutdownIntentRecord",
    "AppDeploymentIntentRecord",
    "AppRecord",
    "AutoscalingStub",
    "AutoscalingStubConfig",
    "AutoscalingStubRecord",
    "AutoscalingStubRuntimeConfig",
    "StubKind",
    "StubRecord",
]
