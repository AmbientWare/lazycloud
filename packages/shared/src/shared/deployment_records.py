from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated

from pydantic import Field, JsonValue, field_validator, model_validator

from shared.autoscaling import QueueDepthAutoscaler
from shared.compute_policy import LAZYCLOUD_MACHINE_POOL, MachinePool
from shared.contracts import ContractModel
from shared.custom_domains import normalize_assignable_hostname
from shared.deployments import DeploymentKind
from shared.http.client_manifests import ClientContract
from shared.image_building.authoring import ImageSpec
from shared.lifecycle import LifecycleHooks
from shared.tasks import RetryPolicy
from shared.timestamps import utc_now

# A per-container ceiling rather than an allocation, so one value serves every
# workload kind; workloads that genuinely need more raise it explicitly.
DEFAULT_DISK = "100Gi"
DEFAULT_FUNCTION_CPU = 0.125
DEFAULT_FUNCTION_AUTHORIZED = True
DEFAULT_FUNCTION_MAX_PENDING_TASKS = 100
DEFAULT_FUNCTION_MEMORY = "128Mi"
DEFAULT_FUNCTION_RETRIES = 3
DEFAULT_FUNCTION_KEEP_WARM_SECONDS = 10
DEFAULT_FUNCTION_TIMEOUT_SECONDS = 3600
DEFAULT_HTTP_CPU = 1.0
DEFAULT_HTTP_KEEP_WARM_SECONDS = 180
DEFAULT_HTTP_MEMORY = "128Mi"
DEFAULT_HTTP_TIMEOUT_SECONDS = 180
DEFAULT_HTTP_UNBOUNDED_WAIT_TIMEOUT_SECONDS = 600
DEFAULT_MAX_PENDING_TASKS = 100
DEFAULT_POD_CPU = 1.0
DEFAULT_POD_MEMORY = "128Mi"
DEFAULT_POD_KEEP_WARM_SECONDS = 600


class Resources(ContractModel):
    cpu: float | None = None
    memory: str | None = None
    disk: str = DEFAULT_DISK
    gpu: str | None = None
    gpu_count: int = 0
    timeout_seconds: int | None = None
    concurrency: int = 1
    keep_warm: int | None = None
    preemptible: bool = False

    @field_validator("disk", mode="before")
    @classmethod
    def disk_defaults_to_the_platform_ceiling(cls, value: object) -> object:
        # A record written without a ceiling reads back as the platform one.
        # Every container has a limit, so an absent value is the default rather
        # than an error or an unbounded container.
        if value is None or value == "":
            return DEFAULT_DISK
        return value

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
    """Idle seconds a workload's container survives for, by kind.

    A function's container outlives the invocation that started it, so a second
    call arriving inside this window reaches an interpreter that has already
    imported the handler and already run `on_start`. Short, because the window is
    also what an idle caller pays for.

    A schedule is answered by `resolve_keep_warm_seconds` rather than here: it is
    a property of one deployment, not of a kind, and the value it wants is zero.
    """
    deployment_kind = _deployment_kind(kind)
    if deployment_kind in {DeploymentKind.Endpoint, DeploymentKind.Asgi}:
        return DEFAULT_HTTP_KEEP_WARM_SECONDS
    if deployment_kind is DeploymentKind.Pod:
        return DEFAULT_POD_KEEP_WARM_SECONDS
    if deployment_kind is DeploymentKind.Function:
        return DEFAULT_FUNCTION_KEEP_WARM_SECONDS
    return 0


def declared_min_containers(metadata: Mapping[str, JsonValue]) -> int:
    """The warm floor a deployment asked for, before anything resolves it."""

    raw = metadata.get("autoscaler")
    if raw is None:
        return 0
    return QueueDepthAutoscaler.model_validate(raw).min_containers


def resolve_keep_warm_seconds(
    kind: DeploymentKind | str,
    value: int | float | None,
    *,
    min_containers: int = 0,
    scheduled: bool = False,
) -> int:
    """The idle seconds a container survives for, given what else was asked for.

    A function container retires itself when this window passes with no work, so
    a warm floor and a finite window contradict each other: the floor would
    start, idle out, and start again on the next tick — a count that is right
    whenever it is read and warm at no point. A declared floor therefore means
    the container does not retire itself, and the autoscaler is what removes one.

    A schedule says the opposite. Its next run is minutes or hours away, so a
    window held open after each one is paid for and reaches nothing; a scheduled
    workload keeps zero unless its author asked for a window by name.

    Answered here because two owners build a stub config — a deployment
    registration and the gateway's get-or-create — and a rule about the window
    that lived in one of them would hold on one deploy path and not the other.
    """

    if _deployment_kind(kind) is DeploymentKind.Function and min_containers > 0:
        return -1
    if value is not None:
        return int(value)
    if scheduled:
        return 0
    return default_keep_warm_seconds(kind)


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
    return None


def resolve_disk(value: str | int | None) -> str | int | None:
    """Per-container disk ceiling.

    Unlike cpu and memory this does not vary by workload kind: it is a runaway
    guard, not a resource allocation, so the same ceiling applies everywhere.
    """
    if value is not None:
        return value
    return DEFAULT_DISK


def resolve_timeout_seconds(kind: DeploymentKind | str, value: int | None) -> int | None:
    if value is not None:
        return value
    deployment_kind = _deployment_kind(kind)
    if deployment_kind is DeploymentKind.Function:
        return DEFAULT_FUNCTION_TIMEOUT_SECONDS
    if deployment_kind in {DeploymentKind.Endpoint, DeploymentKind.Asgi}:
        return DEFAULT_HTTP_TIMEOUT_SECONDS
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
    return 0


def resolve_max_pending_tasks(kind: DeploymentKind | str, value: int | None) -> int | None:
    if value is not None:
        return max(int(value), 0)
    if _deployment_kind(kind) in {
        DeploymentKind.Function,
        DeploymentKind.Endpoint,
        DeploymentKind.Asgi,
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
    domain: str | None = None
    """Hostname this resource should serve, under a domain the workspace registered.

    Declared beside the resource because that is where every other deployed fact
    lives, and because it says which resource the hostname reaches without a
    second place to look.
    """
    cron: str | None = None
    command: list[str] = Field(default_factory=list)
    ports: dict[str, Annotated[int, Field(strict=True, ge=1, le=65535)]] = Field(
        default_factory=dict
    )
    retry_policy: RetryPolicy | None = None
    lifecycle_hooks: LifecycleHooks = Field(default_factory=LifecycleHooks)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    client_contract: ClientContract | None = None

    @field_validator("methods")
    @classmethod
    def normalize_methods(cls, values: list[str]) -> list[str]:
        return [value.upper() for value in values]

    @field_validator("domain")
    @classmethod
    def normalize_domain(cls, value: str | None) -> str | None:
        return None if value is None else normalize_assignable_hostname(value)

    @model_validator(mode="after")
    def workload_configuration_is_canonical(self) -> DeploymentSpec:
        # A container that never retires itself has to be one something else
        # removes. A pod deployment is that by construction; a function is only
        # that when it declares a warm floor, which is what puts its count under
        # the autoscaler. Without one, the container would simply never go away.
        if (
            self.resources.keep_warm == -1
            and self.kind is not DeploymentKind.Pod
            and not (
                self.kind is DeploymentKind.Function and declared_min_containers(self.metadata) > 0
            )
        ):
            msg = "keep_warm=-1 is only supported for pod workloads and functions with a warm floor"
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
    subdomain: str
    """DNS label this resource answers on, shared by every one of its versions.

    Minted from the resource's identity at deploy time and never recomputed: it is
    published in URLs, so deriving it per request would let a later rename move a
    hostname a customer already handed out.
    """
    custom_hostname: str | None = None
    """Registered hostname this resource also answers on, if the spec claimed one.

    Null rather than empty when unclaimed, so the unique index that stops two
    resources sharing a hostname does not treat every unclaimed resource as sharing
    one.
    """
    pool: MachinePool = MachinePool(LAZYCLOUD_MACHINE_POOL)
    """Pool this deployment was pinned to when it was created.

    Resolved once at deploy time: a workspace that later changes its default
    must not move workloads already running.
    """
    active: bool = True
    deleted_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


__all__ = [
    "DEFAULT_DISK",
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
    "Deployment",
    "DeploymentSpec",
    "Resources",
    "VolumeMount",
    "declared_min_containers",
    "default_keep_warm_seconds",
    "resolve_authorized",
    "resolve_cpu",
    "resolve_disk",
    "resolve_http_wait_timeout_seconds",
    "resolve_keep_warm_seconds",
    "resolve_max_pending_tasks",
    "resolve_memory",
    "resolve_retries",
    "resolve_timeout_seconds",
]
