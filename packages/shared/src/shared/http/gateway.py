from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from shared.app_slug import validate_app_slug
from shared.bytes_transport import EncodedBytesBody
from shared.compute_enrollment import AgentCapacityState
from shared.compute_policy import MachinePool
from shared.deployments import DeploymentKind
from shared.enums import StringEnum
from shared.http.base import HttpModel
from shared.http.client_manifests import ClientContract
from shared.lifecycle import LifecycleHooks
from shared.tasks import RetryPolicy


class GatewayUrlKind(StringEnum):
    Stub = "stub"
    Deployment = "deployment"
    Shell = "shell"


class StringList(HttpModel):
    values: list[str] = Field(default_factory=list)


class TaskCostState(StringEnum):
    Available = "available"
    Unavailable = "unavailable"


class AgentCapacityInterruptionRequest(HttpModel):
    agent_token: str = Field(min_length=1, repr=False)
    machine_id: str = Field(min_length=1)
    credential_id: str = Field(min_length=1)
    credential_generation: int = Field(ge=1)
    state: AgentCapacityState
    reason: str = Field(min_length=1, max_length=240)
    observed_at: datetime
    notice_at: datetime | None = None

    @field_validator("state")
    @classmethod
    def state_must_interrupt_capacity(cls, value: AgentCapacityState) -> AgentCapacityState:
        if value is AgentCapacityState.Available:
            msg = "capacity interruption state must be preempting or cordoned"
            raise ValueError(msg)
        return value

    @field_validator("reason")
    @classmethod
    def reason_must_be_normalized(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            msg = "capacity interruption reason is required"
            raise ValueError(msg)
        return normalized


class AgentCapacityInterruptionResponse(HttpModel):
    machine_id: str
    credential_id: str
    credential_generation: int = Field(ge=1)
    state: AgentCapacityState
    reason: str
    observed_at: datetime
    notice_at: datetime | None = None
    changed: bool = False


class ContainerWorkspaceSyncOperation(StringEnum):
    Delete = "delete"
    Write = "write"
    Move = "move"


class SyncContainerWorkspaceBody(EncodedBytesBody):
    container_id: str
    operation: ContainerWorkspaceSyncOperation
    path: str = ""
    new_path: str = ""
    mode: int = 0o644
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @property
    def data(self) -> bytes:
        return self.bytes_value()


class SyncContainerWorkspaceResponse(HttpModel):
    path: str = ""


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
    exit_code: int = 0
    error_msg: str = ""
    input_supported: bool = False
    attach_contract: str = "sse-output-only"


class StubVolume(HttpModel):
    id: str = ""
    mount_path: str = ""
    config: dict[str, JsonValue] | None = None


class SecretVar(HttpModel):
    name: str = ""


class Autoscaler(HttpModel):
    type: Literal["queue_depth"] = "queue_depth"
    max_containers: int = Field(default=1, ge=0)
    tasks_per_container: int = Field(default=1, gt=0)
    min_containers: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def minimum_cannot_exceed_maximum(self) -> Autoscaler:
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
    cpu: float | None = None
    memory: str | int | None = None
    disk: str | int | None = None
    gpu: str = ""
    handler: str = ""
    route: str | None = None
    methods: list[str] = Field(default_factory=list)
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
    autoscaler: Autoscaler = Field(default_factory=Autoscaler)
    task_policy: GatewayTaskPolicy = Field(default_factory=GatewayTaskPolicy)
    concurrent_requests: int = Field(default=1, gt=0)
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
    block_network: bool = False
    allow_list: list[str] = Field(default_factory=list)
    docker_enabled: bool = False
    preemptible: bool = False
    pool: MachinePool = MachinePool(Field(default="", max_length=240))
    """Pool this workload lands in, empty to take the default."""
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    client_contract: ClientContract | None = None
    workspace: str = "default"

    @model_validator(mode="after")
    def never_keep_warm_is_pod_only(self) -> GetOrCreateStubRequest:
        if self.keep_warm_seconds == -1 and self.stub_type != DeploymentKind.Pod.value:
            msg = "keep_warm_seconds=-1 is only supported for pod workloads"
            raise ValueError(msg)
        if self.keep_warm_seconds == -1 and self.autoscaler.max_containers == 0:
            msg = "keep_warm_seconds=-1 requires max_containers to be greater than zero"
            raise ValueError(msg)
        return self


class GetOrCreateStubResponse(HttpModel):
    stub_id: str


class DeployStubRequest(HttpModel):
    stub_id: str
    name: str = ""
    workspace: str | None = None
    external_url: str = "http://127.0.0.1:9000"


class DeployStubResponse(HttpModel):
    stub_id: str = ""
    deployment_id: str = ""
    app_id: str | None = None
    version: int = 0
    invoke_url: str = ""


class GetUrlRequest(HttpModel):
    stub_id: str
    deployment_id: str = ""
    url_type: GatewayUrlKind = GatewayUrlKind.Stub
    is_shell: bool = False
    workspace: str | None = None
    external_url: str = "http://127.0.0.1:9000"
    port: int | None = None


class GetUrlResponse(HttpModel):
    url: str = ""


class ResolveDeploymentTargetRequest(HttpModel):
    kind: DeploymentKind
    name: str
    app: str = ""
    workspace: str = "default"
    deployment_version: int | None = None
    external_url: str = "http://127.0.0.1:9000"
    mode: str = "path"

    @field_validator("app")
    @classmethod
    def validate_app(cls, value: str) -> str:
        return validate_app_slug(value) if value else ""


class ResolveDeploymentTargetResponse(HttpModel):
    kind: DeploymentKind
    stub_id: str = ""
    deployment_id: str = ""
    deployment_name: str = ""
    deployment_version: int = 0
    url: str = ""


__all__ = [
    "AgentCapacityInterruptionRequest",
    "AgentCapacityInterruptionResponse",
    "AttachToContainerRequest",
    "AttachToContainerResponse",
    "Autoscaler",
    "CheckpointContainerRequest",
    "CheckpointContainerResponse",
    "ContainerWorkspaceSyncOperation",
    "DeployStubRequest",
    "DeployStubResponse",
    "GatewayTaskPolicy",
    "GatewayUrlKind",
    "GetOrCreateStubRequest",
    "GetOrCreateStubResponse",
    "GetUrlRequest",
    "GetUrlResponse",
    "ResolveDeploymentTargetRequest",
    "ResolveDeploymentTargetResponse",
    "Schema",
    "SchemaField",
    "SecretVar",
    "StringList",
    "StubVolume",
    "SyncContainerWorkspaceBody",
    "SyncContainerWorkspaceResponse",
    "TaskCostState",
]
