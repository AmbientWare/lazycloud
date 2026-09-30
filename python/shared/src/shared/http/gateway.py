from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from shared.deployment_records import CpuRequest, MemoryRequest, validate_pod_role
from shared.deployments import DeploymentKind, PodRole
from shared.disks import DiskMount, parse_disk_size_bytes, validate_disk_mounts
from shared.http.base import HttpModel
from shared.http.client_manifests import ClientContract
from shared.lifecycle import LifecycleHooks
from shared.placement import AvailabilityZone, ProductRegion, validate_placement_machine
from shared.tasks import RetryPolicy


class CheckpointContainerRequest(HttpModel):
    container_id: str
    checkpoint_id: str | None = None


class CheckpointContainerResponse(HttpModel):
    checkpoint_id: str = ""


class AttachToContainerRequest(HttpModel):
    container_id: str


class AttachToContainerResponse(HttpModel):
    output: str = ""
    done: bool = False
    exit_code: int | None = None
    error_msg: str = ""
    input_supported: bool = False
    attach_contract: str = "sse-output-only"


class StubVolume(HttpModel):
    id: str = ""
    mount_path: str = ""
    config: dict[str, JsonValue] | None = None


class SecretVar(HttpModel):
    name: str = ""


class GatewayAutoscaler(HttpModel):
    type: Literal["queue_depth"] = "queue_depth"
    max_containers: int = Field(default=1, ge=0)
    tasks_per_container: int = Field(default=1, gt=0)
    min_containers: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def minimum_cannot_exceed_maximum(self) -> GatewayAutoscaler:
        if self.min_containers > self.max_containers:
            msg = "min_containers cannot exceed max_containers"
            raise ValueError(msg)
        return self


class GatewayTaskPolicy(HttpModel):
    timeout: int = 0
    ttl: int = 0


class SchemaField(HttpModel):
    type: str = ""
    fields: dict[str, SchemaField] = Field(default_factory=dict)


class Schema(HttpModel):
    fields: dict[str, SchemaField] = Field(default_factory=dict)


class GetOrCreateStubRequest(HttpModel):
    object_id: str = ""
    image_id: str = ""
    stub_type: str = "function"
    name: str
    python_version: str = "3.12"
    image_base: str = ""
    python_packages: list[str] = Field(default_factory=list)
    image_commands: list[str] = Field(default_factory=list)
    image_build_steps: list[dict[str, JsonValue]] = Field(default_factory=list)
    image_env: list[str] = Field(default_factory=list)
    image_workdir: str = ""
    image_dockerfile: str = ""
    image_context_path: str = ""
    image_context_digest: str = ""
    image_context_object: str = ""
    image_include_patterns: list[str] = Field(default_factory=list)
    image_credential_keys: list[str] = Field(default_factory=list)
    image_secrets: list[str] = Field(default_factory=list)
    image_gpu: str = ""
    image_ignore_python: bool = False
    cpu: CpuRequest | None = None
    memory: MemoryRequest | None = None
    disk: str | int | None = None
    gpu: list[str] = Field(default_factory=list)
    handler: str = ""
    route: str | None = None
    domain: str | None = None
    methods: list[str] = Field(default_factory=list)
    cron: str = ""
    retries: int = 0
    retry_policy: RetryPolicy | None = None
    timeout: int = 0
    keep_warm_seconds: int | None = Field(default=None, ge=-1)
    workers: int = 0
    max_pending_tasks: int = 0
    volumes: list[StubVolume] = Field(default_factory=list)
    force_create: bool = False
    lifecycle_hooks: LifecycleHooks = Field(default_factory=LifecycleHooks)
    callback_url: str = ""
    authorized: bool = False
    secrets: list[SecretVar] = Field(default_factory=list)
    autoscaler: GatewayAutoscaler = Field(default_factory=GatewayAutoscaler)
    task_policy: GatewayTaskPolicy = Field(default_factory=GatewayTaskPolicy)
    concurrent_requests: int = Field(default=1, gt=0)
    in_process: bool = False
    extra: str = ""
    checkpoint_enabled: bool = False
    gpu_count: int = 0
    entrypoint: list[str] = Field(default_factory=list)
    ports: list[int] = Field(default_factory=list)
    env: list[str] = Field(default_factory=list)
    app_name: str = ""
    inputs: Schema = Field(default_factory=Schema)
    outputs: Schema = Field(default_factory=Schema)
    command: list[str] = Field(default_factory=list)
    tcp: bool = False
    ssh: bool | None = None
    """Unset resolves by role: a devbox serves SSH, a service pod does not."""

    disks: list[DiskMount] = Field(default_factory=list)
    role: PodRole | None = None
    root_disk_bytes: int | None = None
    """Size of the root disk a devbox gets when it declares none at ``/``."""

    block_network: bool = False
    allow_list: list[str] = Field(default_factory=list)
    docker_enabled: bool = False
    preemptible: bool | None = None
    """Unset resolves by role."""

    machine: str = Field(default="", max_length=63)
    """A joined machine this workload must run on, empty to run in the workspace."""
    region: ProductRegion | None = None
    availability_zone: AvailabilityZone = ""
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    client_contract: ClientContract | None = None
    workspace: str = "default"

    @field_validator("root_disk_bytes")
    @classmethod
    def root_disk_is_bounded(cls, value: int | None) -> int | None:
        return None if value is None else parse_disk_size_bytes(value)

    @model_validator(mode="after")
    def workload_configuration_is_canonical(self) -> GetOrCreateStubRequest:
        validate_placement_machine(self.region, self.availability_zone, self.machine)
        validate_disk_mounts(self.disks)
        # The rules the deployment record states, checked here too because this
        # is the other owner that builds a runtime-ready stub config.
        if (self.ssh or self.disks) and self.stub_type != DeploymentKind.Pod.value:
            msg = "ssh and disks are only supported for pod workloads"
            raise ValueError(msg)
        if self.cron and self.stub_type != DeploymentKind.Function.value:
            msg = "cron is only supported for function workloads"
            raise ValueError(msg)
        # A container that does not retire itself needs something that removes
        # it, which for a function is the autoscaler holding it to a declared
        # floor.
        if (
            self.keep_warm_seconds == -1
            and self.stub_type != DeploymentKind.Pod.value
            and not (
                self.stub_type == DeploymentKind.Function.value
                and self.autoscaler.min_containers > 0
            )
        ):
            msg = (
                "keep_warm_seconds=-1 is only supported for pod workloads and "
                "functions with a warm floor"
            )
            raise ValueError(msg)
        if self.keep_warm_seconds == -1 and self.autoscaler.max_containers == 0:
            msg = "keep_warm_seconds=-1 requires max_containers to be greater than zero"
            raise ValueError(msg)
        validate_pod_role(
            self.stub_type,
            self.role,
            name=self.name,
            ssh=self.ssh,
            disks=self.disks,
            root_disk_bytes=self.root_disk_bytes,
            max_containers=self.autoscaler.max_containers,
        )
        return self


__all__ = [
    "AttachToContainerRequest",
    "AttachToContainerResponse",
    "CheckpointContainerRequest",
    "CheckpointContainerResponse",
    "GatewayAutoscaler",
    "GatewayTaskPolicy",
    "GetOrCreateStubRequest",
    "Schema",
    "SchemaField",
    "SecretVar",
    "StubVolume",
]
