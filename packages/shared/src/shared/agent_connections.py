from __future__ import annotations

import re
from datetime import UTC, datetime
from ipaddress import IPv6Address
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, field_validator

from shared.contracts import ContractModel
from shared.http.agent_identity import AgentTunnelIdentity

_HOSTNAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
AGENT_TUNNEL_CONTROL_PORT = 9000
AGENT_TUNNEL_CONTROL_URL = f"http://127.0.0.1:{AGENT_TUNNEL_CONTROL_PORT}"


class AgentConnectionRecord(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    identity: AgentTunnelIdentity
    gateway_id: str
    connection_id: str
    gateway_address: str
    expires_at: AwareDatetime

    @field_validator("gateway_id", "connection_id")
    @classmethod
    def validate_uuid(cls, value: str) -> str:
        return str(UUID(value))

    @field_validator("gateway_address")
    @classmethod
    def validate_gateway_address(cls, value: str) -> str:
        invalid = "gateway address must be a host:port without credentials or a URL path"
        if value != value.strip() or any(character.isspace() for character in value):
            raise ValueError(invalid)
        parsed = urlsplit(f"//{value}")
        if (
            parsed.netloc != value
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or not parsed.hostname
            or parsed.port is None
            or not 1 <= parsed.port <= 65535
        ):
            raise ValueError(invalid)
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{IPv6Address(host).compressed}]"
        else:
            host = host.rstrip(".")
            if len(host) > 253 or not all(_HOSTNAME.fullmatch(label) for label in host.split(".")):
                raise ValueError(invalid)
        return f"{host}:{parsed.port}"

    @field_validator("expires_at")
    @classmethod
    def normalize_expiry(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


__all__ = ["AGENT_TUNNEL_CONTROL_PORT", "AGENT_TUNNEL_CONTROL_URL", "AgentConnectionRecord"]
