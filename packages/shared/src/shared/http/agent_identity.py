from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from shared.http.base import HttpModel

TUNNEL_CERTIFICATE_RENEWAL_MARGIN_SECONDS = 1200


class AgentTunnelIdentity(HttpModel):
    workspace_id: str
    enrollment_id: str
    credential_generation: int = Field(strict=True, ge=1)

    @field_validator("workspace_id", "enrollment_id")
    @classmethod
    def validate_uuid(cls, value: str) -> str:
        return str(UUID(value))

    @property
    def identity_uri(self) -> str:
        return (
            f"lazycloud://agent/{self.workspace_id}/"
            f"{self.enrollment_id}/{self.credential_generation}"
        )

    @classmethod
    def from_uri(cls, uri: str) -> AgentTunnelIdentity:
        parts = uri.removeprefix("lazycloud://agent/").split("/")
        if len(parts) != 3:
            raise ValueError("Invalid agent certificate identity")
        identity = cls(
            workspace_id=parts[0], enrollment_id=parts[1], credential_generation=int(parts[2])
        )
        if identity.identity_uri != uri:
            raise ValueError("Agent certificate identity must use its canonical URI")
        return identity


class TunnelServiceRole(StrEnum):
    Gateway = "gateway"
    ControlPlane = "control-plane"


class ServiceTunnelIdentity(HttpModel):
    role: TunnelServiceRole
    instance_id: str

    @field_validator("instance_id")
    @classmethod
    def validate_uuid(cls, value: str) -> str:
        return str(UUID(value))

    @property
    def identity_uri(self) -> str:
        return f"lazycloud://service/{self.role}/{self.instance_id}"

    @classmethod
    def from_uri(cls, uri: str) -> ServiceTunnelIdentity:
        parts = uri.removeprefix("lazycloud://service/").split("/")
        if len(parts) != 2:
            raise ValueError("Invalid service certificate identity")
        identity = cls(role=TunnelServiceRole(parts[0]), instance_id=parts[1])
        if identity.identity_uri != uri:
            raise ValueError("Service certificate identity must use its canonical URI")
        return identity


class AgentCertificateRequest(HttpModel):
    agent_token: str = Field(min_length=1, repr=False)
    csr_pem: str = Field(min_length=1, max_length=16_384, repr=False)


class AgentCertificate(HttpModel):
    identity: AgentTunnelIdentity
    certificate_pem: str = Field(min_length=1, repr=False)
    trust_bundle_pem: str = Field(min_length=1, repr=False)
    not_before: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def validate_validity(self) -> AgentCertificate:
        if self.expires_at <= self.not_before:
            raise ValueError("Certificate expiry must follow its validity start")
        return self


class AgentCertificateResponse(AgentCertificate):
    tunnel_address: str = Field(
        min_length=5,
        max_length=257,
        pattern=(
            r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?:443$"
        ),
    )


class GatewayCertificateRequest(HttpModel):
    csr_pem: str = Field(min_length=1, max_length=16_384, repr=False)
    instance_id: str

    @field_validator("instance_id")
    @classmethod
    def validate_uuid(cls, value: str) -> str:
        return str(UUID(value))


class ServiceCertificateResponse(HttpModel):
    identity: ServiceTunnelIdentity
    certificate_pem: str = Field(min_length=1, repr=False)
    trust_bundle_pem: str = Field(min_length=1, repr=False)
    not_before: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def validate_validity(self) -> ServiceCertificateResponse:
        if self.expires_at <= self.not_before:
            raise ValueError("Certificate expiry must follow its validity start")
        return self


__all__ = [
    "TUNNEL_CERTIFICATE_RENEWAL_MARGIN_SECONDS",
    "AgentCertificate",
    "AgentCertificateRequest",
    "AgentCertificateResponse",
    "AgentTunnelIdentity",
    "GatewayCertificateRequest",
    "ServiceCertificateResponse",
    "ServiceTunnelIdentity",
    "TunnelServiceRole",
]
