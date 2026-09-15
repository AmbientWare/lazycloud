from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX
from shared.errors import InvalidInputError, UpstreamUnavailableError
from shared.urls import tcp_ingress_hostname


class TcpIngressSettings(BaseSettings):
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = Field(default=1995, ge=1, le=65535)
    external_host: str = "tcp.localhost"
    certificate_file: Path | None = None
    key_file: Path | None = None
    route_cache_ttl_seconds: int = Field(default=300, ge=1)
    max_connections: int = Field(default=1024, ge=1)
    tls_handshake_timeout_seconds: float = Field(default=10.0, gt=0)

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_TCP_INGRESS_",
        extra="ignore",
    )

    @field_validator("host")
    @classmethod
    def normalize_host(cls, value: str) -> str:
        return value.strip()

    @field_validator("external_host")
    @classmethod
    def normalize_external_host(cls, value: str) -> str:
        return value.strip().strip(".").lower()

    @model_validator(mode="after")
    def validate_enabled_configuration(self) -> TcpIngressSettings:
        if not self.enabled:
            return self
        if not self.external_host:
            raise ValueError("TCP ingress external host is required when ingress is enabled")
        if self.certificate_file is None or self.key_file is None:
            raise ValueError("TCP ingress certificate and key files are required when enabled")
        return self


def require_tcp_ingress(*, public: bool) -> TcpIngressSettings:
    if not public:
        raise InvalidInputError("raw TCP ingress requires a public Pod with authorized=False")
    settings = TcpIngressSettings()
    if not settings.enabled:
        raise UpstreamUnavailableError("TCP ingress is not configured on this installation")
    return settings


def tcp_pod_url(stub_id: str, port: int, *, public: bool) -> str:
    settings = require_tcp_ingress(public=public)
    hostname = tcp_ingress_hostname(stub_id, port, settings.external_host)
    return f"tls://{hostname}:{settings.port}"
