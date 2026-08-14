from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX

from observability.telemetry import (
    DEFAULT_EXPORT_TIMEOUT_SECONDS,
    DEFAULT_METER_INTERVAL_SECONDS,
    DEFAULT_OTLP_HTTP_ENDPOINT,
    DEFAULT_TRACE_INTERVAL_SECONDS,
    TelemetryConfig,
)


class TelemetrySettings(BaseSettings):
    enabled: bool = False
    endpoint: str = DEFAULT_OTLP_HTTP_ENDPOINT
    meter_interval_seconds: float = Field(default=DEFAULT_METER_INTERVAL_SECONDS, gt=0)
    trace_interval_seconds: float = Field(default=DEFAULT_TRACE_INTERVAL_SECONDS, gt=0)
    trace_sample_ratio: float = 1.0
    export_timeout_seconds: float = Field(default=DEFAULT_EXPORT_TIMEOUT_SECONDS, gt=0)
    export_traces: bool = True
    export_metrics: bool = True
    export_logs: bool = True

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_TELEMETRY_",
        extra="ignore",
    )

    def to_config(self, *, service_name: str) -> TelemetryConfig:
        return TelemetryConfig(
            enabled=self.enabled,
            service_name=service_name,
            endpoint=self.endpoint,
            meter_interval_seconds=self.meter_interval_seconds,
            trace_interval_seconds=self.trace_interval_seconds,
            trace_sample_ratio=self.trace_sample_ratio,
            export_timeout_seconds=self.export_timeout_seconds,
            export_traces=self.export_traces,
            export_metrics=self.export_metrics,
            export_logs=self.export_logs,
        )


class VolumeMeteringSettings(BaseSettings):
    interval_seconds: float = Field(default=60.0, gt=0)

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_VOLUME_METERING_",
        extra="ignore",
    )


class WorkspaceChangeStreamSettings(BaseSettings):
    max_length: int = Field(default=10_000, gt=0)

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_WORKSPACE_CHANGE_STREAM_",
        extra="ignore",
    )


__all__ = [
    "TelemetrySettings",
    "VolumeMeteringSettings",
    "WorkspaceChangeStreamSettings",
]
