from __future__ import annotations

from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX, METRICS_SOURCE
from shared.billing import UsagePriceConfig
from shared.usage import UsageCollectorKind, UsageMetricsSinkSettings

from observability.billing import UsagePriceCatalog, configured_usage_price_catalog
from observability.managed_billing import ManagedBillingMode, ManagedBillingSettings
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
        env_file=".env",
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


class UsageMetricsSettings(BaseSettings):
    collector: UsageCollectorKind = UsageCollectorKind.Disabled
    source: str = METRICS_SOURCE
    prometheus_port: int = Field(default=9090, ge=1, le=65535)
    openmeter_url: str | None = None
    openmeter_api_key: SecretStr | None = None

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_USAGE_METRICS_",
        env_file=".env",
        extra="ignore",
    )

    @field_validator("source")
    @classmethod
    def normalize_source(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("usage metrics source cannot be empty")
        return normalized

    @model_validator(mode="after")
    def validate_collector(self) -> UsageMetricsSettings:
        if self.openmeter_url is not None:
            self.openmeter_url = self.openmeter_url.strip() or None
        if self.collector is UsageCollectorKind.OpenMeter and self.openmeter_url is None:
            raise ValueError("OpenMeter URL is required for the OpenMeter usage collector")
        if self.openmeter_url is not None:
            parsed = urlparse(self.openmeter_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("OpenMeter URL must be an absolute HTTP(S) URL")
        return self

    def to_sink_settings(self) -> UsageMetricsSinkSettings:
        api_key = (
            self.openmeter_api_key.get_secret_value().strip()
            if self.openmeter_api_key is not None
            else ""
        )
        return UsageMetricsSinkSettings(
            collector=self.collector,
            source=self.source,
            prometheus_port=self.prometheus_port,
            openmeter_url=self.openmeter_url,
            openmeter_api_key=SecretStr(api_key) if api_key else None,
        )


class UsagePricingSettings(BaseSettings):
    billing_currency: str = "USD"
    price_catalog: list[UsagePriceConfig] | None = None

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_USAGE_",
        env_file=".env",
        extra="ignore",
    )

    @field_validator("billing_currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        currency = value.strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("usage billing currency must be a three-letter code")
        return currency

    @model_validator(mode="after")
    def validate_price_catalog(self) -> UsagePricingSettings:
        self.to_price_catalog()
        return self

    def to_price_catalog(self) -> UsagePriceCatalog:
        return configured_usage_price_catalog(
            currency=self.billing_currency,
            prices=self.price_catalog,
        )


class ManagedBillingClientSettings(BaseSettings):
    mode: ManagedBillingMode = ManagedBillingMode.Noop
    endpoint: str = ""
    auth_token: SecretStr = SecretStr("")
    timeout_seconds: float = Field(default=10.0, gt=0)
    minimum_credit_cents: int = Field(default=0, ge=0)
    required: bool = False
    headers: dict[str, SecretStr] = Field(default_factory=dict)

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_MANAGED_BILLING_",
        env_file=".env",
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_runtime_settings(self) -> ManagedBillingClientSettings:
        self.to_runtime_settings()
        return self

    def to_runtime_settings(self) -> ManagedBillingSettings:
        return ManagedBillingSettings(
            mode=self.mode,
            endpoint=self.endpoint,
            auth_token=self.auth_token.get_secret_value(),
            timeout_seconds=self.timeout_seconds,
            minimum_credit_cents=self.minimum_credit_cents,
            required=self.required,
            headers={name: value.get_secret_value() for name, value in self.headers.items()},
        )


class VolumeMeteringSettings(BaseSettings):
    interval_seconds: float = Field(default=60.0, gt=0)

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_VOLUME_METERING_",
        env_file=".env",
        extra="ignore",
    )


class WorkspaceChangeStreamSettings(BaseSettings):
    max_length: int = Field(default=10_000, gt=0)

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_WORKSPACE_CHANGE_STREAM_",
        env_file=".env",
        extra="ignore",
    )


__all__ = [
    "ManagedBillingClientSettings",
    "TelemetrySettings",
    "UsageMetricsSettings",
    "UsagePricingSettings",
    "VolumeMeteringSettings",
    "WorkspaceChangeStreamSettings",
]
