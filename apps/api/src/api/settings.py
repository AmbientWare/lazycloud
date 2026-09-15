from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX


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


class AgentDisconnectReconciliationSettings(BaseSettings):
    """How often to look for machines that stopped reporting.

    Shorter than the route reconciliation because this one decides how far the
    durable record trails the live view. The heartbeat timeout is 60s, so at
    this interval a machine is written off within about a minute and a quarter
    of its last report.
    """

    interval_seconds: float = Field(default=15.0, gt=0)

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_AGENT_DISCONNECT_RECONCILIATION_",
        extra="ignore",
    )


__all__ = [
    "AgentDisconnectReconciliationSettings",
    "AgentRouteReconciliationSettings",
]
