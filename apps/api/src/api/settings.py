from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX
from shared.capacity import CAPACITY_OWNER_ID_PATTERN
from shared.http.compute import PoolPolicy
from shared.routing import BackendRouteTransport, PrivatePoolFallback


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


class PublicIngressSettings(BaseSettings):
    """How the API reads the true client behind the deployment's ingress.

    Empty means no proxy: the socket peer is the client, and any forwarding
    header is attacker-settable noise. The deployment sets the header name only
    because its ingress strips and rewrites that header on every request.
    """

    client_ip_header: str = ""
    provider_node_proof_max_inflight: int = Field(default=8, ge=1)

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_PUBLIC_INGRESS_",
        extra="ignore",
    )

    @field_validator("client_ip_header")
    @classmethod
    def normalize_header(cls, value: str) -> str:
        return value.strip().lower()


class AgentRouteReconciliationSettings(BaseSettings):
    interval_seconds: float = Field(default=60.0, gt=0)

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_AGENT_ROUTE_RECONCILIATION_",
        extra="ignore",
    )


class CapacityBootstrapPool(PoolPolicy):
    """One provisioning unit reconciled before the production API starts serving.

    `machine_pool` is the pool the unit stamps on its machines; it
    defaults to the unit's own name so an unset pool still routes.
    """

    name: str = Field(min_length=1, max_length=160)
    machine_pool: str = Field(default="", max_length=240)
    workspace: str = Field(default="default", min_length=1, max_length=160)
    provider: str = Field(default="local", min_length=1, max_length=160)
    capacity_owner_id: str = Field(pattern=CAPACITY_OWNER_ID_PATTERN)
    transport: BackendRouteTransport = BackendRouteTransport.TsnetRestricted
    fallback: PrivatePoolFallback = PrivatePoolFallback.Internal

    @field_validator("name", "workspace", "provider", "machine_pool")
    @classmethod
    def normalize_identity(cls, value: str) -> str:
        return value.strip()


class CapacityBootstrapSettings(BaseSettings):
    pools: tuple[CapacityBootstrapPool, ...] = ()

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_CAPACITY_BOOTSTRAP_",
        extra="ignore",
    )

    @field_validator("pools")
    @classmethod
    def require_unique_pool_owners(
        cls,
        pools: tuple[CapacityBootstrapPool, ...],
    ) -> tuple[CapacityBootstrapPool, ...]:
        identities = [(pool.workspace, pool.name) for pool in pools]
        if len(identities) != len(set(identities)):
            raise ValueError("capacity bootstrap pools must have unique workspace/name pairs")
        owner_ids = [pool.capacity_owner_id for pool in pools]
        if len(owner_ids) != len(set(owner_ids)):
            raise ValueError("capacity bootstrap pools must have unique capacity owner ids")
        return pools


__all__ = [
    "AgentRouteReconciliationSettings",
    "CapacityBootstrapPool",
    "CapacityBootstrapSettings",
    "TcpIngressSettings",
]
