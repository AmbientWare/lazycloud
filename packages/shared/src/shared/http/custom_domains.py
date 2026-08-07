from __future__ import annotations

from datetime import datetime

from pydantic import Field

from shared.custom_domains import (
    MAX_HOSTNAME_LENGTH,
    CustomDomain,
    CustomDomainErrorCode,
    CustomDomainPhase,
)
from shared.http.base import HttpModel


class CustomDomainRegisterRequest(HttpModel):
    domain: str = Field(min_length=3, max_length=MAX_HOSTNAME_LENGTH)
    """An apex such as `acme.com`, or a single-level wildcard such as `*.acme.com`."""


class CustomDomainResponse(HttpModel):
    id: str
    hostname: str
    phase: CustomDomainPhase
    cname_target: str = ""
    """Hostname the customer points their DNS at. The platform's own public host."""

    verification_target: str = ""
    """An extra record the edge asked for, on the rare occasion it wants one."""

    error_code: CustomDomainErrorCode | None = None
    error_message: str | None = Field(default=None, max_length=512)
    verified_at: datetime | None = None
    last_checked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class CustomDomainListResponse(HttpModel):
    data: list[CustomDomainResponse] = Field(default_factory=list)
    next: str = ""


def custom_domain_response(domain: CustomDomain, *, cname_target: str) -> CustomDomainResponse:
    return CustomDomainResponse(
        id=domain.id,
        hostname=domain.hostname,
        phase=domain.phase,
        cname_target=cname_target,
        verification_target=domain.verification_target,
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
