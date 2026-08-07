from __future__ import annotations

import re
from datetime import datetime

from pydantic import Field

from shared.contracts import ContractModel
from shared.enums import StringEnum
from shared.timestamps import utc_now

_UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"

MAX_HOSTNAME_LENGTH = 253
WILDCARD_PREFIX = "*."

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN = rf"{_LABEL}(?:\.{_LABEL})+"
_REGISTRABLE_PATTERN = re.compile(rf"^(?:\*\.)?{_DOMAIN}$")
_ASSIGNABLE_PATTERN = re.compile(rf"^{_DOMAIN}$")


class CustomDomainPhase(StringEnum):
    AwaitingVerification = "awaiting_verification"
    """Provider hostname created; the customer still has to publish the CNAME."""

    Validating = "validating"
    Ready = "ready"
    ActionRequired = "action_required"


class CustomDomainErrorCode(StringEnum):
    VerificationTimedOut = "verification_timed_out"
    CertificateFailed = "certificate_failed"
    HostnameRejected = "hostname_rejected"
    UpstreamUnavailable = "upstream_unavailable"


class CustomDomain(ContractModel):
    """A domain a workspace has registered and may serve resources under.

    Registered once and verified once, rather than per resource: the provider
    hostname and its certificate belong here, so assigning or unassigning a name to a
    deployment never revokes anything.
    """

    id: str = Field(pattern=_UUID_PATTERN)
    workspace_id: str = Field(pattern=_UUID_PATTERN)
    hostname: str = Field(min_length=3, max_length=MAX_HOSTNAME_LENGTH)
    phase: CustomDomainPhase = CustomDomainPhase.AwaitingVerification
    provider_hostname_id: str | None = Field(default=None, min_length=1, max_length=128)
    verification_target: str = ""
    """The CNAME value the customer publishes, as the provider reported it."""

    error_code: CustomDomainErrorCode | None = None
    error_message: str | None = Field(default=None, max_length=512)
    verified_at: datetime | None = None
    last_checked_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    deleted_at: datetime | None = None

    @property
    def is_wildcard(self) -> bool:
        return self.hostname.startswith(WILDCARD_PREFIX)

    def covers(self, hostname: str) -> bool:
        """Whether a deployment may claim `hostname` under this registration.

        A wildcard covers exactly one label, matching what a certificate for it
        actually secures; `*.acme.com` serves `api.acme.com` and not
        `api.staging.acme.com`.
        """

        candidate = hostname.strip().rstrip(".").lower()
        if not self.is_wildcard:
            return candidate == self.hostname
        suffix = self.hostname.removeprefix("*")
        if not candidate.endswith(suffix):
            return False
        label = candidate[: -len(suffix)]
        return bool(label) and "." not in label


def normalize_registrable_domain(value: str) -> str:
    """Accept a domain a workspace can register: an apex or a one-level wildcard."""

    hostname = value.strip().rstrip(".").lower()
    if len(hostname) > MAX_HOSTNAME_LENGTH:
        raise ValueError(f"domain must be at most {MAX_HOSTNAME_LENGTH} characters")
    if not _REGISTRABLE_PATTERN.fullmatch(hostname):
        raise ValueError(
            "domain must be a hostname such as acme.com, or a single-level wildcard "
            "such as *.acme.com"
        )
    return hostname


def normalize_assignable_hostname(value: str) -> str:
    """Accept a concrete hostname a deployment can serve. Wildcards are not one."""

    hostname = value.strip().rstrip(".").lower()
    if len(hostname) > MAX_HOSTNAME_LENGTH:
        raise ValueError(f"hostname must be at most {MAX_HOSTNAME_LENGTH} characters")
    if not _ASSIGNABLE_PATTERN.fullmatch(hostname):
        raise ValueError(f"hostname must be a concrete domain name: {value!r}")
    return hostname


__all__ = [
    "MAX_HOSTNAME_LENGTH",
    "WILDCARD_PREFIX",
    "CustomDomain",
    "CustomDomainErrorCode",
    "CustomDomainPhase",
    "normalize_assignable_hostname",
    "normalize_registrable_domain",
]
