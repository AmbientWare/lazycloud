from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated

from pydantic import Field, JsonValue, field_validator, model_validator

from shared.autoscaling import Autoscaler
from shared.contracts import ContractModel
from shared.custom_domains import normalize_assignable_hostname
from shared.deployments import DEFAULT_ENDPOINT_METHODS, DeploymentKind, PodRole
from shared.disks import (
    DiskMount,
    parse_disk_size_bytes,
    require_one_writer,
    validate_disk_mounts,
    validate_disk_name,
)
from shared.http.client_manifests import ClientContract
from shared.image_building.authoring import ImageSpec
from shared.lifecycle import LifecycleHooks
from shared.placement import (
    AvailabilityZone,
    Placement,
    ProductRegion,
    validate_placement_machine,
)
from shared.resources import parse_memory_mib
from shared.tasks import RetryPolicy
from shared.timestamps import utc_now

# A per-container ceiling rather than an allocation, so one value serves every
# workload kind; workloads that genuinely need more raise it explicitly.
DEFAULT_DISK = "100Gi"
# The list arm is not an alternative spelling anyone writes. JSON has no tuples,
# so the pair a decorator states in Python reaches a strict model as a list once
# it has crossed the wire, and both have to validate as the same thing.
CpuRequest = int | float | tuple[int | float, int | float] | list[int | float]
"""Cores to reserve, or a `(reserve, throttle at)` pair."""

MemoryRequest = str | int | tuple[str | int, str | int] | list[str | int]
"""Memory to reserve, or a `(reserve, kill at)` pair."""

DEFAULT_FUNCTION_CPU = 0.125
DEFAULT_FUNCTION_AUTHORIZED = True
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
DEFAULT_WORKLOAD_PREEMPTIBLE = True
DEFAULT_DEVBOX_KEEP_WARM_SECONDS = 1800
DEFAULT_DEVBOX_PREEMPTIBLE = False
"""A reclaimed node would cut an SSH session off mid-command."""

DEVBOX_COMMAND = ("sleep", "infinity")
"""What a devbox runs when it names no command: nothing, for as long as it is kept."""


class Resources(ContractModel):
    region: ProductRegion | None = None
    availability_zone: AvailabilityZone = ""
    cpu: CpuRequest | None = None
    memory: MemoryRequest | None = None
    disk: str = DEFAULT_DISK
    gpu: list[str] = Field(default_factory=list)
    """Models this workload accepts, best first; empty asks for no GPU."""

    gpu_count: int = 0
    timeout_seconds: int | None = None
    concurrency: int = 1
    keep_warm: int | None = None
    preemptible: bool | None = None
    """Unset resolves by role when the deployment is registered."""

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
    def cpu_must_be_positive(cls, value: CpuRequest | None) -> CpuRequest | None:
        # Both halves of a pair, because a ceiling of zero throttles a container
        # to nothing and is as wrong as a reservation of zero.
        request, limit = request_and_limit(value)
        for part in (request, limit):
            if part is not None and float(part) <= 0:
                msg = "cpu must be greater than zero"
                raise ValueError(msg)
        if limit is not None and request is not None and float(limit) < float(request):
            msg = "a cpu limit cannot sit below its request"
            raise ValueError(msg)
        return value

    @field_validator("memory")
    @classmethod
    def memory_limit_cannot_sit_below_its_request(
        cls, value: MemoryRequest | None
    ) -> MemoryRequest | None:
        request, limit = request_and_limit(value)
        if request is None or limit is None:
            return value
        if (parse_memory_mib(limit) or 0) < (parse_memory_mib(request) or 0):
            msg = "a memory limit cannot sit below its request"
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


def default_keep_warm_seconds(kind: DeploymentKind | str, role: PodRole | None = None) -> int:
    """Idle seconds a workload's container survives for, by kind and a pod's role.

    A function's container outlives the invocation that started it, so a second
    call arriving inside this window reaches an interpreter that has already
    imported the handler and already run `on_start`. Short, because the window is
    also what an idle caller pays for.

    A devbox waits longer than a service pod: the person who left it is usually
    coming back, and every return inside the window skips a restore of its disk.

    A schedule is answered by `resolve_keep_warm_seconds` rather than here: it is
    a property of one deployment, not of a kind, and the value it wants is zero.
    """
    deployment_kind = _deployment_kind(kind)
    if deployment_kind is DeploymentKind.Pod and role is PodRole.Devbox:
        return DEFAULT_DEVBOX_KEEP_WARM_SECONDS
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
    return Autoscaler.model_validate(raw).min_containers


def resolve_keep_warm_seconds(
    kind: DeploymentKind | str,
    value: int | float | None,
    *,
    min_containers: int = 0,
    scheduled: bool = False,
    role: PodRole | None = None,
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
    return default_keep_warm_seconds(kind, role)


def resolve_pod_role(kind: DeploymentKind | str, role: PodRole | None) -> PodRole | None:
    """A pod's role, `Service` when it names none; every other kind has none."""
    if _deployment_kind(kind) is not DeploymentKind.Pod:
        return None
    return role or PodRole.Service


def validate_pod_role(
    kind: DeploymentKind | str,
    role: PodRole | None,
    *,
    name: str,
    ssh: bool | None,
    disks: list[DiskMount],
    root_disk_bytes: int | None,
    max_containers: int | None,
) -> None:
    """Refuse a role its other settings contradict, before anything resolves it.

    Shared by the two owners that accept a workload spec, the deployment record
    and the gateway's stub request, so a devbox means the same thing on both.
    """
    if role is not None and _deployment_kind(kind) is not DeploymentKind.Pod:
        raise ValueError("role is only supported for pod workloads")
    if role is not PodRole.Devbox:
        if root_disk_bytes is not None:
            raise ValueError("root_disk_bytes is only supported for devboxes")
        return
    if ssh is False:
        raise ValueError("a devbox is reached over SSH and cannot turn ssh off")
    if max_containers is not None:
        # Checked before the root disk exists, so a spec fails where it is written.
        require_one_writer(max_containers)
    has_root = any(disk.is_root for disk in disks)
    if has_root and root_disk_bytes is not None:
        raise ValueError("a devbox has one root disk: size it or declare a disk at /, not both")
    if not has_root and root_disk_bytes is None:
        raise ValueError("a devbox needs a root disk; give its size")
    if root_disk_bytes is not None:
        try:
            validate_disk_name(name)
        except ValueError as exc:
            raise ValueError(f"a devbox's root disk takes its name, so {exc}") from exc


def resolve_pod_ssh(role: PodRole | None, ssh: bool | None) -> bool:
    if ssh is not None:
        return ssh
    return role is PodRole.Devbox


def resolve_pod_command(role: PodRole | None, command: list[str]) -> list[str]:
    if command or role is not PodRole.Devbox:
        return list(command)
    return list(DEVBOX_COMMAND)


def resolve_pod_disks(
    role: PodRole | None,
    *,
    name: str,
    disks: list[DiskMount],
    root_disk_bytes: int | None,
) -> list[DiskMount]:
    """A pod's disks, with the root disk a devbox sized but did not declare."""
    if role is not PodRole.Devbox or root_disk_bytes is None:
        return list(disks)
    return [DiskMount(name=name, size_bytes=root_disk_bytes), *disks]


def resolve_preemptible(role: PodRole | None, value: bool | None) -> bool:
    if value is not None:
        return value
    if role is PodRole.Devbox:
        return DEFAULT_DEVBOX_PREEMPTIBLE
    return DEFAULT_WORKLOAD_PREEMPTIBLE


def request_and_limit(
    value: CpuRequest | MemoryRequest | None,
) -> tuple[str | int | float | None, str | int | float | None]:
    """A resource field read as `(request, limit)`, whichever form was written.

    Every reader that only wants the reservation goes through here rather than
    testing the shape itself, so a pair cannot reach arithmetic that assumes a
    scalar and silently multiply a tuple.
    """
    if isinstance(value, (tuple, list)):
        if len(value) != 2:
            raise ValueError("a resource pair states exactly a request and a limit")
        return value[0], value[1]
    return value, None


def resolve_cpu(kind: DeploymentKind | str, value: CpuRequest | None) -> CpuRequest | None:
    # A pair passes through whole. The default only answers an absent value, and
    # a limit its author stated is not something to resolve away.
    if isinstance(value, (tuple, list)):
        return value
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


def resolve_memory(kind: DeploymentKind | str, value: MemoryRequest | None) -> MemoryRequest | None:
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
    disks: list[DiskMount] = Field(default_factory=list)
    route: str | None = None
    methods: list[str] = Field(default_factory=lambda: list(DEFAULT_ENDPOINT_METHODS))
    role: PodRole | None = None
    """What a pod is for; unset resolves to a service. Only a pod may carry one."""

    root_disk_bytes: int | None = None
    """Size of the root disk a devbox gets when it declares none at ``/``.

    Resolved into `disks` under the devbox's own name when the deployment is
    registered, so a stored spec never carries it.
    """

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

    @field_validator("disks")
    @classmethod
    def disks_are_distinct(cls, value: list[DiskMount]) -> list[DiskMount]:
        return validate_disk_mounts(value)

    @field_validator("domain")
    @classmethod
    def normalize_domain(cls, value: str | None) -> str | None:
        return None if value is None else normalize_assignable_hostname(value)

    @field_validator("root_disk_bytes")
    @classmethod
    def root_disk_is_bounded(cls, value: int | None) -> int | None:
        return None if value is None else parse_disk_size_bytes(value)

    @model_validator(mode="after")
    def workload_configuration_is_canonical(self) -> DeploymentSpec:
        machine = self.metadata.get("machine")
        validate_placement_machine(
            self.resources.region,
            self.resources.availability_zone,
            machine if isinstance(machine, str) else None,
        )
        # A schedule fires an invocation, and a function is the only kind that
        # has one. Refused here rather than at the tick, where the schedule
        # exists, fires against a stub that cannot serve it, and records the
        # same failure every minute for as long as the deployment lives.
        if (self.disks or self.metadata.get("ssh") is True) and self.kind is not DeploymentKind.Pod:
            msg = "ssh and disks are only supported for pod workloads"
            raise ValueError(msg)
        if self.cron and self.kind is not DeploymentKind.Function:
            msg = "cron is only supported for function workloads"
            raise ValueError(msg)
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
        autoscaler_config = (
            Autoscaler.model_validate(autoscaler) if autoscaler is not None else None
        )
        if (
            autoscaler_config is not None
            and self.resources.keep_warm == -1
            and autoscaler_config.max_containers == 0
        ):
            msg = "keep_warm=-1 requires max_containers to be greater than zero"
            raise ValueError(msg)
        ssh = self.metadata.get("ssh")
        validate_pod_role(
            self.kind,
            self.role,
            name=self.name,
            ssh=ssh if isinstance(ssh, bool) else None,
            disks=self.disks,
            root_disk_bytes=self.root_disk_bytes,
            max_containers=autoscaler_config.max_containers if autoscaler_config else None,
        )
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
    placement: Placement = Placement.platform()
    """Where this deployment was pinned when it was created.

    Derived, never chosen: the workspace's location, or the named machine.
    Resolved once at deploy time so running workloads never move.
    """
    machine: str = ""
    """The joined machine this deployment is pinned to by name, or empty."""
    active: bool = True
    deleted_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


__all__ = [
    "DEFAULT_DEVBOX_KEEP_WARM_SECONDS",
    "DEFAULT_DEVBOX_PREEMPTIBLE",
    "DEFAULT_DISK",
    "DEFAULT_FUNCTION_AUTHORIZED",
    "DEFAULT_FUNCTION_CPU",
    "DEFAULT_FUNCTION_KEEP_WARM_SECONDS",
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
    "DEFAULT_WORKLOAD_PREEMPTIBLE",
    "DEVBOX_COMMAND",
    "CpuRequest",
    "Deployment",
    "DeploymentSpec",
    "MemoryRequest",
    "Resources",
    "VolumeMount",
    "declared_min_containers",
    "default_keep_warm_seconds",
    "request_and_limit",
    "resolve_authorized",
    "resolve_cpu",
    "resolve_disk",
    "resolve_http_wait_timeout_seconds",
    "resolve_keep_warm_seconds",
    "resolve_max_pending_tasks",
    "resolve_memory",
    "resolve_pod_command",
    "resolve_pod_disks",
    "resolve_pod_role",
    "resolve_pod_ssh",
    "resolve_preemptible",
    "resolve_retries",
    "resolve_timeout_seconds",
    "validate_pod_role",
]
