from __future__ import annotations

import os
import re
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)
from shared.app_identity import (
    OBJECT_STORE_ACCESS_KEY_ID,
    OBJECT_STORE_BUCKET,
    OBJECT_STORE_SECRET_ACCESS_KEY,
)
from shared.capacity import CAPACITY_OWNER_ID_PATTERN
from shared.compute_policy import LAZYCLOUD_MACHINE_POOL, MachinePool
from shared.env import (
    GATEWAY_HTTP_URL_ENV,
    WORKER_REPOSITORY_URL_ENV,
)
from worker.configuration import (
    WORKER_CONFIG_PATH_ENV,
    WorkerConfiguration,
)
from worker.image_lifecycle import DEFAULT_IMAGE_ARCHIVE_EXTENSION
from worker.retention import (
    DEFAULT_WORKER_CHECKPOINT_CACHE_MAX_BYTES,
    DEFAULT_WORKER_IMAGE_CACHE_MAX_BYTES,
    DEFAULT_WORKER_IMAGE_MATERIALIZATION_MAX_BYTES,
)
from worker.status import DEFAULT_WORKER_SPINDOWN_SECONDS

DEFAULT_CHECKPOINT_CACHE_NAMESPACE = "checkpoints"


class WorkerSettings(BaseSettings):
    """What this worker is, layered over the configuration it was given.

    `configuration` is the whole worker configuration document and the only home
    for capacity, runtimes, paths, and monitoring; the flat fields beside it are
    the per-instance identity and credentials the launcher knows and the document
    cannot. Sources layer in `settings_customise_sources` order, and a partial
    `configuration` mapping merges into the file key by key, so a caller can
    override one nested value without restating the document.
    """

    model_config = SettingsConfigDict(
        extra="forbid",
        populate_by_name=True,
    )

    configuration: WorkerConfiguration = Field(default_factory=WorkerConfiguration)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        del cls, dotenv_settings
        # No default path: the agent and Compose both name the file explicitly,
        # and an unset variable has to mean "read no YAML" so a stray file at a
        # well-known path cannot decide a worker's capacity.
        config_path = os.environ.get(WORKER_CONFIG_PATH_ENV, "").strip()
        yaml_settings = YamlConfigSettingsSource(
            settings_cls,
            yaml_file=config_path or None,
        )
        return init_settings, env_settings, yaml_settings, file_secret_settings

    worker_id: str = Field(
        default="",
        validation_alias="WORKER_ID",
    )
    worker_token: str = Field(
        default="",
        validation_alias="WORKER_TOKEN",
    )
    worker_repository_url: str = Field(
        default="",
        validation_alias=WORKER_REPOSITORY_URL_ENV,
    )

    gateway_runtime_http_url: str = Field(
        default="",
        validation_alias=GATEWAY_HTTP_URL_ENV,
    )
    worker_repository_timeout_seconds: float = Field(
        default=30.0,
        validation_alias="WORKER_REPOSITORY_TIMEOUT_SECONDS",
    )
    pool: MachinePool = Field(
        default=MachinePool(LAZYCLOUD_MACHINE_POOL),
        validation_alias="WORKER_POOL",
    )
    capacity_owner_id: str = Field(
        default="",
        validation_alias="WORKER_CAPACITY_OWNER_ID",
    )
    machine_id: str = Field(
        default="",
        validation_alias="WORKER_MACHINE",
    )
    pod_address: str = Field(
        default="",
        validation_alias=AliasChoices(
            "WORKER_POD_ADDRESS",
            "WORKER_POD_IP",
            "WORKER_POD_HOST",
        ),
    )
    container_service_port: int = Field(
        default=0,
        validation_alias="WORKER_CONTAINER_SERVICE_PORT",
    )
    container_service_token: str = Field(
        default="",
        validation_alias="WORKER_CONTAINER_SERVICE_TOKEN",
    )
    worker_spindown_seconds: float = Field(
        default=DEFAULT_WORKER_SPINDOWN_SECONDS,
        validation_alias="WORKER_SPINDOWN_SECONDS",
    )
    route_local_target_host: str = Field(
        default="",
        validation_alias="WORKER_ROUTE_TARGET",
    )
    network_prefix: str = Field(
        default="",
        validation_alias="WORKER_NETWORK_PREFIX",
    )
    host_port_bind_address: str = Field(
        default="",
        validation_alias="WORKER_HOST_PORT_BIND_ADDRESS",
    )
    source_cache_storage_id: str = Field(
        default="",
        validation_alias="WORKER_SOURCE_CACHE_STORAGE_ID",
    )
    image_archive_extension: str = Field(
        default=DEFAULT_IMAGE_ARCHIVE_EXTENSION,
        validation_alias="WORKER_IMAGE_ARCHIVE_EXTENSION",
    )
    cache_endpoint: str = Field(
        default="",
        validation_alias="WORKER_CACHE_ENDPOINT",
    )
    cache_service_token: str = Field(
        default="",
        validation_alias="LAZYCLOUD_CACHE_SERVICE_TOKEN",
    )
    cache_service_token_file: Path | None = Field(
        default=None,
        validation_alias="LAZYCLOUD_CACHE_SERVICE_TOKEN_FILE",
    )
    checkpoint_bucket: str = Field(
        default="",
        validation_alias="WORKER_CHECKPOINT_BUCKET",
    )
    checkpoint_cache_namespace: str = Field(
        default=DEFAULT_CHECKPOINT_CACHE_NAMESPACE,
        validation_alias="WORKER_CHECKPOINT_CACHE_NAMESPACE",
    )
    retention_enabled: bool = Field(
        default=True,
        validation_alias="WORKER_RETENTION_ENABLED",
    )
    retention_interval_seconds: float = Field(
        default=5 * 60,
        gt=0,
        validation_alias="WORKER_RETENTION_INTERVAL_SECONDS",
    )
    image_cache_max_bytes: int = Field(
        default=DEFAULT_WORKER_IMAGE_CACHE_MAX_BYTES,
        validation_alias="WORKER_IMAGE_CACHE_MAX_BYTES",
    )
    image_materialization_max_bytes: int = Field(
        default=DEFAULT_WORKER_IMAGE_MATERIALIZATION_MAX_BYTES,
        validation_alias="WORKER_IMAGE_MATERIALIZATION_MAX_BYTES",
    )
    checkpoint_cache_max_bytes: int = Field(
        default=DEFAULT_WORKER_CHECKPOINT_CACHE_MAX_BYTES,
        validation_alias="WORKER_CHECKPOINT_CACHE_MAX_BYTES",
    )
    retention_low_watermark_pct: float = Field(
        default=0.75,
        validation_alias="WORKER_RETENTION_LOW_WATERMARK_PCT",
    )
    retention_recent_guard_seconds: int = Field(
        default=60 * 60,
        validation_alias="WORKER_RETENTION_RECENT_GUARD_SECONDS",
    )
    image_materialization_retention_seconds: int = Field(
        default=24 * 60 * 60,
        validation_alias="WORKER_IMAGE_MATERIALIZATION_RETENTION_SECONDS",
    )
    checkpoint_retention_seconds: int = Field(
        default=7 * 24 * 60 * 60,
        validation_alias="WORKER_CHECKPOINT_RETENTION_SECONDS",
    )
    data_storage_bucket: str = Field(
        default=OBJECT_STORE_BUCKET,
        validation_alias=AliasChoices("DATA_STORAGE_BUCKET", "LAZYCLOUD_OBJECT_STORE_BUCKET"),
    )
    data_storage_endpoint_url: str = Field(
        default="http://localhost:9000",
        validation_alias=AliasChoices(
            "DATA_STORAGE_ENDPOINT_URL",
            "LAZYCLOUD_OBJECT_STORE_ENDPOINT_URL",
        ),
    )
    data_storage_region_name: str = Field(
        default="us-east-1",
        validation_alias=AliasChoices(
            "DATA_STORAGE_REGION_NAME",
            "LAZYCLOUD_OBJECT_STORE_REGION_NAME",
        ),
    )
    data_storage_access_key_id: str = Field(
        default=OBJECT_STORE_ACCESS_KEY_ID,
        validation_alias=AliasChoices(
            "DATA_STORAGE_ACCESS_KEY_ID",
            "LAZYCLOUD_OBJECT_STORE_ACCESS_KEY_ID",
        ),
    )
    data_storage_secret_access_key: str = Field(
        default=OBJECT_STORE_SECRET_ACCESS_KEY,
        validation_alias=AliasChoices(
            "DATA_STORAGE_SECRET_ACCESS_KEY",
            "LAZYCLOUD_OBJECT_STORE_SECRET_ACCESS_KEY",
        ),
    )
    data_storage_force_path_style: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "DATA_STORAGE_FORCE_PATH_STYLE",
            "LAZYCLOUD_OBJECT_STORE_FORCE_PATH_STYLE",
        ),
    )
    workspace_storage_base_mount_path: str = Field(
        default="/workspace",
        validation_alias="WORKER_WORKSPACE_STORAGE_BASE_MOUNT_PATH",
    )
    workspace_storage_geesefs_binary: str = Field(
        default="geesefs",
        validation_alias="WORKER_WORKSPACE_STORAGE_GEESEFS_BINARY",
    )
    workspace_storage_geesefs_memory_limit_mb: int = Field(
        default=1024,
        validation_alias="WORKER_WORKSPACE_STORAGE_GEESEFS_MEMORY_LIMIT_MB",
    )
    workspace_storage_mountpoint_binary: str = Field(
        default="ms3",
        validation_alias="WORKER_WORKSPACE_STORAGE_MOUNTPOINT_BINARY",
    )
    gpu_devices: str = Field(
        default="",
        validation_alias=AliasChoices("WORKER_GPU_DEVICES", "NVIDIA_VISIBLE_DEVICES"),
    )
    nvidia_cdi_enabled: bool = Field(
        default=True,
        validation_alias="WORKER_NVIDIA_CDI_ENABLED",
    )

    @field_validator("container_service_port")
    @classmethod
    def port_must_be_valid(cls, value: int) -> int:
        if value < 0 or value > 65535:
            msg = "container service port must be between 0 and 65535"
            raise ValueError(msg)
        return value

    @field_validator("capacity_owner_id")
    @classmethod
    def capacity_owner_id_must_be_canonical(cls, value: str) -> str:
        normalized = value.strip()
        if normalized and re.fullmatch(CAPACITY_OWNER_ID_PATTERN, normalized) is None:
            raise ValueError("worker capacity owner id must be a canonical UUID")
        return normalized

    @field_validator(
        "image_cache_max_bytes",
        "image_materialization_max_bytes",
        "checkpoint_cache_max_bytes",
        "retention_recent_guard_seconds",
        "image_materialization_retention_seconds",
        "checkpoint_retention_seconds",
    )
    @classmethod
    def retention_values_cannot_be_negative(cls, value: int) -> int:
        if value < 0:
            msg = "worker retention values cannot be negative"
            raise ValueError(msg)
        return value

    @field_validator("retention_low_watermark_pct")
    @classmethod
    def artifact_low_watermark_must_be_valid(cls, value: float) -> float:
        if not 0 < value <= 1:
            msg = "worker artifact retention low watermark must be in (0, 1]"
            raise ValueError(msg)
        return value

    @property
    def worker_repository_endpoint(self) -> str:
        return self.worker_repository_url.rstrip("/")

    @property
    def gateway_runtime_http_endpoint(self) -> str:
        return self.gateway_runtime_http_url.rstrip("/")
