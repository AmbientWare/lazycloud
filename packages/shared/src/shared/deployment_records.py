from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import Field, JsonValue, field_validator, model_validator

from shared.autoscaling import QueueDepthAutoscaler
from shared.compute_policy import (
    ComputePlacement,
    ComputePlacementSource,
    ComputePlacementTarget,
)
from shared.contracts import ContractModel
from shared.deployments import DeploymentKind
from shared.http.client_manifests import ClientContract
from shared.image_building.authoring import ImageSpec
from shared.lifecycle import LifecycleHooks
from shared.tasks import RetryPolicy
from shared.timestamps import utc_now

DEFAULT_FUNCTION_CPU = 0.125
DEFAULT_FUNCTION_AUTHORIZED = True
DEFAULT_FUNCTION_MAX_PENDING_TASKS = 100
DEFAULT_FUNCTION_MEMORY = "128Mi"
DEFAULT_FUNCTION_RETRIES = 3
DEFAULT_FUNCTION_TIMEOUT_SECONDS = 3600
DEFAULT_FUNCTION_KEEP_WARM_SECONDS = 10
DEFAULT_HTTP_CPU = 1.0
DEFAULT_HTTP_KEEP_WARM_SECONDS = 180
DEFAULT_HTTP_MEMORY = "128Mi"
DEFAULT_HTTP_TIMEOUT_SECONDS = 180
DEFAULT_HTTP_UNBOUNDED_WAIT_TIMEOUT_SECONDS = 600
DEFAULT_MAX_PENDING_TASKS = 100
DEFAULT_POD_CPU = 1.0
DEFAULT_POD_MEMORY = "128Mi"
DEFAULT_POD_KEEP_WARM_SECONDS = 600
DEFAULT_TASK_QUEUE_CPU = 1.0
DEFAULT_TASK_QUEUE_KEEP_WARM_SECONDS = 10
DEFAULT_TASK_QUEUE_MEMORY = "128Mi"
DEFAULT_TASK_QUEUE_RETRIES = 3
DEFAULT_TASK_QUEUE_TIMEOUT_SECONDS = 3600


class Resources(ContractModel):
    cpu: float | None = None
    memory: str | None = None
    gpu: str | None = None
    gpu_count: int = 0
    timeout_seconds: int | None = None
    concurrency: int = 1
    keep_warm: int | None = None
    preemptible: bool = False

    @field_validator("cpu")
    @classmethod
    def cpu_must_be_positive(cls, value: float | None) -> float | None:
        if value is not None and value <= 0:
            msg = "cpu must be greater than zero"
            raise ValueError(msg)
        return value

    @field_validator("gpu_count")
    @classmethod
    def gpu_count_cannot_be_negative(cls, value: int) -> int:
        if value < 0:
            msg = "gpu_count cannot be negative"
            raise ValueError(msg)
        return value

    @field_validator("concurrency")
    @classmethod
    def concurrency_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            msg = "concurrency must be greater than zero"
            raise ValueError(msg)
        return value

    @field_validator("keep_warm")
    @classmethod
    def keep_warm_allows_never_for_pods(cls, value: int | None) -> int | None:
        if value is not None and value < -1:
            msg = "keep_warm must be -1 or non-negative"
            raise ValueError(msg)
        return value


def default_keep_warm_seconds(kind: DeploymentKind | str) -> int:
    deployment_kind = _deployment_kind(kind)
    if deployment_kind is DeploymentKind.Function:
        return DEFAULT_FUNCTION_KEEP_WARM_SECONDS
    if deployment_kind in {DeploymentKind.Endpoint, DeploymentKind.Asgi}:
        return DEFAULT_HTTP_KEEP_WARM_SECONDS
    if deployment_kind is DeploymentKind.Pod:
        return DEFAULT_POD_KEEP_WARM_SECONDS
    if deployment_kind is DeploymentKind.TaskQueue:
        return DEFAULT_TASK_QUEUE_KEEP_WARM_SECONDS
    return 0


def resolve_keep_warm_seconds(kind: DeploymentKind | str, value: int | float | None) -> int:
    if value is None:
        return default_keep_warm_seconds(kind)
    return int(value)


def resolve_cpu(kind: DeploymentKind | str, value: int | float | None) -> float | None:
    if value is not None:
        return float(value)
    deployment_kind = _deployment_kind(kind)
    if deployment_kind is DeploymentKind.Function:
        return DEFAULT_FUNCTION_CPU
    if deployment_kind in {DeploymentKind.Endpoint, DeploymentKind.Asgi}:
        return DEFAULT_HTTP_CPU
    if deployment_kind is DeploymentKind.Pod:
        return DEFAULT_POD_CPU
    if deployment_kind is DeploymentKind.TaskQueue:
        return DEFAULT_TASK_QUEUE_CPU
    return None


def resolve_memory(kind: DeploymentKind | str, value: str | int | None) -> str | int | None:
    if value is not None:
        return value
    deployment_kind = _deployment_kind(kind)
    if deployment_kind is DeploymentKind.Function:
        return DEFAULT_FUNCTION_MEMORY
    if deployment_kind in {DeploymentKind.Endpoint, DeploymentKind.Asgi}:
        return DEFAULT_HTTP_MEMORY
    if deployment_kind is DeploymentKind.Pod:
        return DEFAULT_POD_MEMORY
    if deployment_kind is DeploymentKind.TaskQueue:
        return DEFAULT_TASK_QUEUE_MEMORY
    return None


def resolve_timeout_seconds(kind: DeploymentKind | str, value: int | None) -> int | None:
    if value is not None:
        return value
    deployment_kind = _deployment_kind(kind)
    if deployment_kind is DeploymentKind.Function:
        return DEFAULT_FUNCTION_TIMEOUT_SECONDS
    if deployment_kind in {DeploymentKind.Endpoint, DeploymentKind.Asgi}:
        return DEFAULT_HTTP_TIMEOUT_SECONDS
    if deployment_kind is DeploymentKind.TaskQueue:
        return DEFAULT_TASK_QUEUE_TIMEOUT_SECONDS
    return None


def resolve_http_wait_timeout_seconds(value: int | float | None) -> float:
    if value is None or value == 0:
        return float(DEFAULT_HTTP_UNBOUNDED_WAIT_TIMEOUT_SECONDS)
    return float(value)


def resolve_retries(kind: DeploymentKind | str, value: int | None) -> int:
    if value is not None:
        return max(int(value), 0)
    deployment_kind = _deployment_kind(kind)
    if deployment_kind is DeploymentKind.Function:
        return DEFAULT_FUNCTION_RETRIES
    if deployment_kind is DeploymentKind.TaskQueue:
        return DEFAULT_TASK_QUEUE_RETRIES
    return 0


def resolve_max_pending_tasks(kind: DeploymentKind | str, value: int | None) -> int | None:
    if value is not None:
        return max(int(value), 0)
    if _deployment_kind(kind) in {
        DeploymentKind.Function,
        DeploymentKind.Endpoint,
        DeploymentKind.Asgi,
        DeploymentKind.TaskQueue,
    }:
        return DEFAULT_MAX_PENDING_TASKS
    return None


def resolve_authorized(kind: DeploymentKind | str, value: bool | None) -> bool:
    if value is not None:
        return value
    return _deployment_kind(kind) in {
        DeploymentKind.Function,
        DeploymentKind.Endpoint,
        DeploymentKind.Asgi,
        DeploymentKind.TaskQueue,
    }


def _deployment_kind(value: DeploymentKind | str) -> DeploymentKind:
    if isinstance(value, DeploymentKind):
        return value
    try:
        return DeploymentKind(str(value))
    except ValueError:
        return DeploymentKind.Function


class VolumeMount(ContractModel):
    name: str
    mount_path: str
    read_only: bool = False
    config: dict[str, JsonValue] | None = None


class DeploymentSpec(ContractModel):
    name: str
    kind: DeploymentKind = DeploymentKind.Function
    handler: str | None = None
    image: ImageSpec = Field(default_factory=ImageSpec)
    resources: Resources = Field(default_factory=Resources)
    env: dict[str, str] = Field(default_factory=dict)
    secrets: list[str] = Field(default_factory=list)
    volumes: list[VolumeMount] = Field(default_factory=list)
    route: str | None = None
    methods: list[str] = Field(default_factory=lambda: ["GET", "POST"])
    cron: str | None = None
    command: list[str] = Field(default_factory=list)
    ports: dict[str, Annotated[int, Field(strict=True, ge=1, le=65535)]] = Field(
        default_factory=dict
    )
    retry_policy: RetryPolicy | None = None
    lifecycle_hooks: LifecycleHooks = Field(default_factory=LifecycleHooks)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    client_contract: ClientContract | None = None
    placement: ComputePlacementTarget | None = None

    @field_validator("methods")
    @classmethod
    def normalize_methods(cls, values: list[str]) -> list[str]:
        return [value.upper() for value in values]

    @model_validator(mode="after")
    def workload_configuration_is_canonical(self) -> DeploymentSpec:
        if self.resources.keep_warm == -1 and self.kind is not DeploymentKind.Pod:
            msg = "keep_warm=-1 is only supported for pod workloads"
            raise ValueError(msg)
        autoscaler = self.metadata.get("autoscaler")
        if autoscaler is not None:
            autoscaler_config = QueueDepthAutoscaler.model_validate(autoscaler)
            if self.resources.keep_warm == -1 and autoscaler_config.max_containers == 0:
                msg = "keep_warm=-1 requires max_containers to be greater than zero"
                raise ValueError(msg)
        return self


class Deployment(ContractModel):
    id: str
    name: str
    kind: DeploymentKind
    app_id: str | None = None
    stub_id: str | None = None
    version: int = 1
    spec: DeploymentSpec
    resolved_placement: ComputePlacement = Field(
        default_factory=lambda: ComputePlacement(
            target=ComputePlacementTarget.Managed,
            source=ComputePlacementSource.WorkspaceDefault,
            provider=ComputePlacementTarget.Managed.value,
        )
    )
    active: bool = True
    deleted_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


__all__ = [
    "DEFAULT_FUNCTION_AUTHORIZED",
    "DEFAULT_FUNCTION_CPU",
    "DEFAULT_FUNCTION_KEEP_WARM_SECONDS",
    "DEFAULT_FUNCTION_MAX_PENDING_TASKS",
    "DEFAULT_FUNCTION_MEMORY",
    "DEFAULT_FUNCTION_RETRIES",
    "DEFAULT_FUNCTION_TIMEOUT_SECONDS",
    "DEFAULT_HTTP_CPU",
    "DEFAULT_HTTP_KEEP_WARM_SECONDS",
    "DEFAULT_HTTP_MEMORY",
    "DEFAULT_HTTP_TIMEOUT_SECONDS",
    "DEFAULT_HTTP_UNBOUNDED_WAIT_TIMEOUT_SECONDS",
    "DEFAULT_MAX_PENDING_TASKS",
    "DEFAULT_POD_CPU",
    "DEFAULT_POD_KEEP_WARM_SECONDS",
    "DEFAULT_POD_MEMORY",
    "DEFAULT_TASK_QUEUE_CPU",
    "DEFAULT_TASK_QUEUE_KEEP_WARM_SECONDS",
    "DEFAULT_TASK_QUEUE_MEMORY",
    "DEFAULT_TASK_QUEUE_RETRIES",
    "DEFAULT_TASK_QUEUE_TIMEOUT_SECONDS",
    "Deployment",
    "DeploymentSpec",
    "Resources",
    "VolumeMount",
    "default_keep_warm_seconds",
    "resolve_authorized",
    "resolve_cpu",
    "resolve_http_wait_timeout_seconds",
    "resolve_keep_warm_seconds",
    "resolve_max_pending_tasks",
    "resolve_memory",
    "resolve_retries",
    "resolve_timeout_seconds",
]
