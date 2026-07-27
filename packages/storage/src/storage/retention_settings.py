from __future__ import annotations

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX

from storage.retention import (
    DEFAULT_ARTIFACT_BUILD_RETENTION_SECONDS,
    DEFAULT_ARTIFACT_IMAGE_RETENTION_SECONDS,
    DEFAULT_ARTIFACT_MAX_ITEMS_PER_CYCLE,
    DEFAULT_ARTIFACT_RETENTION_INTERVAL_SECONDS,
    DEFAULT_ARTIFACT_SOURCE_GRACE_SECONDS,
    ArtifactRetentionConfig,
)

DEFAULT_ARTIFACT_RETENTION_RETRY_INITIAL_SECONDS = 30.0
DEFAULT_ARTIFACT_RETENTION_RETRY_MAX_SECONDS = 15 * 60.0
DEFAULT_ARTIFACT_RECENT_STUB_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_ARTIFACT_CHECKPOINT_RETENTION_SECONDS = 7 * 24 * 60 * 60


class ArtifactRetentionSettings(BaseSettings):
    enabled: bool = True
    interval_seconds: float = Field(
        default=DEFAULT_ARTIFACT_RETENTION_INTERVAL_SECONDS,
        gt=0,
    )
    retry_initial_seconds: float = Field(
        default=DEFAULT_ARTIFACT_RETENTION_RETRY_INITIAL_SECONDS,
        gt=0,
    )
    retry_max_seconds: float = Field(
        default=DEFAULT_ARTIFACT_RETENTION_RETRY_MAX_SECONDS,
        gt=0,
    )
    recent_stub_ttl_seconds: int = Field(
        default=DEFAULT_ARTIFACT_RECENT_STUB_TTL_SECONDS,
        gt=0,
    )
    source_grace_seconds: int = Field(
        default=DEFAULT_ARTIFACT_SOURCE_GRACE_SECONDS,
        gt=0,
    )
    checkpoint_seconds: int = Field(
        default=DEFAULT_ARTIFACT_CHECKPOINT_RETENTION_SECONDS,
        gt=0,
    )
    build_seconds: int = Field(
        default=DEFAULT_ARTIFACT_BUILD_RETENTION_SECONDS,
        gt=0,
    )
    image_seconds: int = Field(
        default=DEFAULT_ARTIFACT_IMAGE_RETENTION_SECONDS,
        gt=0,
    )
    max_items_per_cycle: int = Field(
        default=DEFAULT_ARTIFACT_MAX_ITEMS_PER_CYCLE,
        gt=0,
    )

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_ARTIFACT_RETENTION_",
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_retry_window(self) -> ArtifactRetentionSettings:
        if self.retry_max_seconds < self.retry_initial_seconds:
            raise ValueError(
                "artifact retention retry max seconds cannot be less than initial seconds"
            )
        return self

    def service_config(
        self,
        *,
        image_archive_bucket: str,
        checkpoint_bucket: str,
        image_archive_prefix: str = "",
    ) -> ArtifactRetentionConfig:
        return ArtifactRetentionConfig(
            image_archive_bucket=image_archive_bucket,
            checkpoint_bucket=checkpoint_bucket,
            image_archive_prefix=image_archive_prefix,
            source_grace_seconds=self.source_grace_seconds,
            build_retention_seconds=self.build_seconds,
            image_retention_seconds=self.image_seconds,
            max_items_per_cycle=self.max_items_per_cycle,
        )


__all__ = [
    "DEFAULT_ARTIFACT_CHECKPOINT_RETENTION_SECONDS",
    "DEFAULT_ARTIFACT_RECENT_STUB_TTL_SECONDS",
    "DEFAULT_ARTIFACT_RETENTION_RETRY_INITIAL_SECONDS",
    "DEFAULT_ARTIFACT_RETENTION_RETRY_MAX_SECONDS",
    "ArtifactRetentionSettings",
]
