from __future__ import annotations

from datetime import datetime

from pydantic import Field

from shared.custom_domains import (
    MAX_HOSTNAME_LENGTH,
    CustomDomain,
    CustomDomainDnsMode,
    CustomDomainErrorCode,
    CustomDomainPhase,
    DnsRecord,
)
from shared.http.base import HttpModel


class CustomDomainRegisterRequest(HttpModel):
    domain: str = Field(min_length=3, max_length=MAX_HOSTNAME_LENGTH)
    dns_mode: CustomDomainDnsMode


class CustomDomainResponse(HttpModel):
    id: str
    hostname: str
    dns_mode: CustomDomainDnsMode
    phase: CustomDomainPhase

    required_records: tuple[DnsRecord, ...] = ()
    """The exact DNS records Pangolin is waiting for."""

    error_code: CustomDomainErrorCode | None = None
    error_message: str | None = Field(default=None, max_length=512)
    verified_at: datetime | None = None
    last_checked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class CustomDomainListResponse(HttpModel):
    data: list[CustomDomainResponse] = Field(default_factory=list)
    next: str = ""


def custom_domain_response(domain: CustomDomain) -> CustomDomainResponse:
    return CustomDomainResponse(
        id=domain.id,
        hostname=domain.hostname,
        dns_mode=domain.dns_mode,
        phase=domain.phase,
        required_records=domain.required_records,
        error_code=domain.error_code,
        error_message=domain.error_message,
        verified_at=domain.verified_at,
        last_checked_at=domain.last_checked_at,
        created_at=domain.created_at,
        updated_at=domain.updated_at,
    )


__all__ = [
    "CustomDomainListResponse",
    "CustomDomainRegisterRequest",
    "CustomDomainResponse",
    "custom_domain_response",
]
