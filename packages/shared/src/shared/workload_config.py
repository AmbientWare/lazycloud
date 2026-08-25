from __future__ import annotations

from typing import Literal

from foundation.resources import parse_memory_mib
from pydantic import ConfigDict, Field, JsonValue, TypeAdapter, field_validator, model_validator

from shared.callbacks import normalize_callback_url
from shared.compute_policy import MachinePool
from shared.container_requests import OciRuntimeName
from shared.contracts import ContractModel
from shared.deployment_records import CpuRequest, MemoryRequest, request_and_limit
from shared.http.client_manifests import ClientContract
from shared.image_building.authoring import ImageBuildStep
from shared.lifecycle import LifecycleHooks
from shared.mounts import MountAuthMode, validate_mount_auth
from shared.tasks import RetryPolicy

_JSON_MAPPING_ADAPTER: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


def cpu_limit_at_or_above_request(value: CpuRequest | None) -> CpuRequest | None:
    # Compared as cores, not through the memory parser: that parser returns
    # whole mebibytes, so every CPU figure below one core would collapse to zero
    # and an inverted fractional pair would compare equal.
    request, limit = request_and_limit(value)
    for part in (request, limit):
        if part is not None and float(part) < 0:
            raise ValueError("cpu must be non-negative")
    if request is not None and limit is not None and float(limit) < float(request):
        # A ceiling under the reservation is a container guaranteed more than
        # it may use, which the kernel resolves by killing it.
        raise ValueError("a cpu limit cannot sit below its request")
    return value


def memory_limit_at_or_above_request(value: MemoryRequest | None) -> MemoryRequest | None:
    request, limit = request_and_limit(value)
    for part in (request, limit):
        if part is not None and (parse_memory_mib(part) or 0) < 0:
            raise ValueError("memory must be non-negative")
    if (
        request is not None
        and limit is not None
        and (parse_memory_mib(limit) or 0) < (parse_memory_mib(request) or 0)
    ):
        raise ValueError("a memory limit cannot sit below its request")
    return value


def absolute_health_check_path(value: str) -> str:
    if value and not value.startswith("/"):
        msg = "health_check_path must be absolute"
        raise ValueError(msg)
    return value


class StubImageConfig(ContractModel):
    image_id: str | None = None
    python_version: str = "3.12"
    base: str = "python:3.12-slim"
    packages: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    build_steps: list[ImageBuildStep] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    workdir: str = "/workspace"
    dockerfile: str | None = None
    context_path: str | None = None
    context_digest: str | None = None
    context_object_id: str | None = None
    include_files_patterns: list[str] = Field(default_factory=list)
    credential_keys: list[str] = Field(default_factory=list)
    secrets: list[str] = Field(default_factory=list)
    gpu: str | None = None
    ignore_python: bool = False
    entrypoint: list[str] = Field(default_factory=list)


class StubRuntimeConfig(ContractModel):
    cpu: CpuRequest | None = Field(default=None)
    cpu_millicores: int = Field(default=0, ge=0)
    memory: MemoryRequest | None = None
    disk: str | int | None = None
    memory_mib: int = Field(default=0, ge=0)
    gpu: list[str] = Field(default_factory=list)
    """Models this workload accepts, best first; empty asks for no GPU.

    One field where there were three. `gpu` and `gpu_type` said the same thing
    and were reconciled by whichever consumer remembered to, and `gpu_request`
    was read by two workload kinds out of four, so a preference written on a
    function was dropped without a word.
    """

    gpu_count: int = Field(default=0, ge=0)
    requires_gpu: bool = False
    image_id: str | None = None
    timeout_seconds: int | float | None = Field(default=None, ge=0)
    retries: int = Field(default=0, ge=0)
    keep_warm: int = Field(default=0, ge=-1)
    concurrency: int = Field(default=1, gt=0)
    in_process: bool = False
    workers: int = Field(default=0, ge=0)
    checkpoint_enabled: bool = False
    checkpoint_readiness_path: str = ""
    checkpoint_readiness_port: int = Field(default=0, ge=0, le=65535)
    checkpoint_readiness_timeout_seconds: int = Field(default=600, ge=1)
    checkpoint_readiness_interval_seconds: float = Field(default=1.0, gt=0)
    health_check_path: str = ""
    health_check_port: int = Field(default=0, ge=0, le=65535)

    @field_validator("cpu")
    @classmethod
    def cpu_states_a_limit_above_its_request(cls, value: CpuRequest | None) -> CpuRequest | None:
        # Every writer of a stub config goes through this model, so refusing here
        # is refusing at stub creation rather than at container start on a worker
        # an hour later.
        return cpu_limit_at_or_above_request(value)

    @field_validator("memory")
    @classmethod
    def memory_states_a_limit_above_its_request(
        cls, value: MemoryRequest | None
    ) -> MemoryRequest | None:
        return memory_limit_at_or_above_request(value)

    @field_validator("health_check_path")
    @classmethod
    def health_check_path_is_absolute(cls, value: str) -> str:
        """Refuse here rather than let every probe of it quietly fail.

        A relative path makes each container permanently unready, which the proxy
        can only report as having no container to route to — naming neither the
        path nor that it was the reason.
        """

        return absolute_health_check_path(value)

    pool_selector: str | None = None
    runtime: str = OciRuntimeName.Runsc.value
    runtime_class: str | None = None
    docker_enabled: bool = False
    block_network: bool = False
    allow_list: list[str] = Field(default_factory=list)
    preemptible: bool = False
    workspace_gpu_quota: int = Field(default=0, ge=0)
    workspace_cpu_quota_millicores: int = Field(default=0, ge=0)
    ports: dict[str, int] = Field(default_factory=dict)


class StubTaskPolicy(ContractModel):
    timeout: int | float = Field(default=0, ge=0)
    timeout_seconds: int | float = Field(default=0, ge=0)
    ttl: int = Field(default=0, ge=0)
    ttl_seconds: int = Field(default=0, ge=0)


class StubAutoscalerConfig(ContractModel):
    type: Literal["queue_depth"] = "queue_depth"
    max_containers: int = Field(default=1, ge=0)
    min_containers: int = Field(default=0, ge=0)
    tasks_per_container: int = Field(default=1, gt=0)
    failed_container_threshold: int | None = Field(default=None, ge=0)
    max_failed_containers: int | None = Field(default=None, ge=0)
    failure_threshold: int | None = Field(default=None, ge=0)
    failed_container_window_seconds: int | None = Field(default=None, ge=0)
    failure_window_seconds: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def minimum_cannot_exceed_maximum(self) -> StubAutoscalerConfig:
        if self.min_containers > self.max_containers:
            msg = "min_containers cannot exceed max_containers"
            raise ValueError(msg)
        return self


class StubVolumeProviderConfig(ContractModel):
    model_config = ConfigDict(extra="allow", validate_assignment=True)

    read_only: bool = False
    bucket_name: str = ""
    prefix: str = ""
    auth_mode: MountAuthMode = MountAuthMode.Ambient
    access_key: str = ""
    secret_key: str = ""
    endpoint_url: str = ""
    region: str = ""
    force_path_style: bool = False

    @model_validator(mode="before")
    @classmethod
    def provider_fields_must_be_json(cls, value: JsonValue) -> dict[str, JsonValue]:
        return _JSON_MAPPING_ADAPTER.validate_python(value)

    @model_validator(mode="after")
    def credentials_match_auth_mode(self) -> StubVolumeProviderConfig:
        validate_mount_auth(
            self.auth_mode,
            self.access_key,
            self.secret_key,
        )
        return self


class StubMountCredentialConfig(StubVolumeProviderConfig):
    mount_path: str = ""

    @property
    def credential_config(self) -> StubVolumeProviderConfig:
        return self


class StubVolumeConfig(ContractModel):
    id: str = ""
    name: str = ""
    mount_path: str = ""
    path: str = ""
    read_only: bool = False
    config: StubVolumeProviderConfig | None = None

    @property
    def credential_config(self) -> StubVolumeProviderConfig:
        return self.config or StubVolumeProviderConfig()


class StubSchemaConfig(ContractModel):
    inputs: dict[str, JsonValue] = Field(default_factory=dict)
    outputs: dict[str, JsonValue] = Field(default_factory=dict)


class StubConfig(ContractModel):
    """Typed persisted configuration shared by all stub lifecycle owners."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        validate_by_name=True,
        serialize_by_alias=True,
    )
    object_id: str = ""
    image: StubImageConfig = Field(default_factory=StubImageConfig)
    runtime: StubRuntimeConfig = Field(default_factory=StubRuntimeConfig)
    env: dict[str, str | None] = Field(default_factory=dict)
    route: str | None = None
    domain: str | None = None
    """Registered hostname this resource claims, carried so a deploy from a stub
    reconstructs the same claim the spec declared."""

    methods: list[str] = Field(default_factory=list)
    cron: str | None = None
    """Schedule this resource fires on, carried for the same reason `domain` is:
    a deploy built from a stub has to reconstruct what the spec declared, and the
    schedule is declared once and then persisted with everything else."""

    command: list[str] = Field(default_factory=list)
    ports: dict[str, int] = Field(default_factory=dict)
    volumes: list[StubVolumeConfig] = Field(default_factory=list)
    mount_credentials: list[StubMountCredentialConfig] = Field(default_factory=list)
    mounts: list[StubMountCredentialConfig] = Field(default_factory=list)
    secrets: list[str] = Field(default_factory=list)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    retry_policy: RetryPolicy | None = None
    client_contract: ClientContract | None = None
    lifecycle_hooks: LifecycleHooks = Field(default_factory=LifecycleHooks)
    autoscaler: StubAutoscalerConfig = Field(default_factory=StubAutoscalerConfig)
    task_policy: StubTaskPolicy = Field(default_factory=StubTaskPolicy)
    callback_url: str | None = None
    max_pending_tasks: int | None = Field(default=None, ge=0)
    extra: JsonValue = None
    schema_config: StubSchemaConfig = Field(
        default_factory=StubSchemaConfig,
        validation_alias="schema",
        serialization_alias="schema",
    )
    tcp: bool = False
    pool: MachinePool = MachinePool("")
    inputs: dict[str, JsonValue] = Field(default_factory=dict)
    outputs: dict[str, JsonValue] = Field(default_factory=dict)
    python_version: str | None = None
    handler: str | None = None
    status: str | None = None
    on_start: str = ""
    on_deploy: str = ""
    on_deploy_stub_id: str = ""

    @field_validator("callback_url", mode="before")
    @classmethod
    def callback_target_must_be_http(cls, value: object) -> object:
        if value is None or isinstance(value, str):
            return normalize_callback_url(value)
        return value

    @model_validator(mode="after")
    def always_on_requires_capacity(self) -> StubConfig:
        if self.runtime.keep_warm == -1 and self.autoscaler.max_containers == 0:
            msg = "keep_warm=-1 requires max_containers to be greater than zero"
            raise ValueError(msg)
        return self


__all__ = [
    "StubAutoscalerConfig",
    "StubConfig",
    "StubImageConfig",
    "StubMountCredentialConfig",
    "StubRuntimeConfig",
    "StubSchemaConfig",
    "StubTaskPolicy",
    "StubVolumeConfig",
    "StubVolumeProviderConfig",
]
