from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import NoReturn

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from shared.custom_domains import (
    WILDCARD_PREFIX,
    CustomDomainErrorCode,
    CustomDomainPhase,
    DnsRecord,
    ProviderCustomHostname,
)
from shared.errors import InvalidInputError, UpstreamUnavailableError

LOGGER = logging.getLogger(__name__)

API_BASE_URL = "https://api.cloudflare.com/client/v4"

# Certificate states Cloudflare reports while it is still working. Anything it does
# not report as one of these, or as issued, is something only the customer can move.
_VALIDATING_SSL_STATES = frozenset(
    {"initializing", "pending_issuance", "pending_deployment", "pending_cleanup"}
)
_AWAITING_SSL_STATES = frozenset({"pending_validation"})


class _CloudflareModel(BaseModel):
    """Cloudflare sends far more than this adapter reads, so unknown fields are expected."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class _SslRequest(_CloudflareModel):
    method: str
    type: str
    wildcard: bool


class _CreateHostnameRequest(_CloudflareModel):
    hostname: str
    ssl: _SslRequest


class _ApiError(_CloudflareModel):
    code: int | None = None
    message: str = ""


class _SslValidationError(_CloudflareModel):
    message: str = ""


class _SslValidationRecord(_CloudflareModel):
    txt_name: str = ""
    txt_value: str = ""
    status: str = ""


class _HostnameSsl(_CloudflareModel):
    status: str
    # Absent until Cloudflare has something to report, and null rather than empty in
    # some of those replies, so the two are one state here.
    validation_errors: tuple[_SslValidationError, ...] | None = None
    validation_records: tuple[_SslValidationRecord, ...] | None = None


class _OwnershipVerification(_CloudflareModel):
    type: str = Field(default="TXT", min_length=1)
    name: str = ""
    value: str = ""


class _HostnameResult(_CloudflareModel):
    # The identifier every later call acts on, and the two statuses the phase is read
    # from. A reply missing any of them says nothing about where the hostname is, and
    # guessing would report a working certificate as one the customer has to fix.
    id: str = Field(min_length=1)
    status: str
    ssl: _HostnameSsl
    ownership_verification: _OwnershipVerification | None = None


class _Envelope(_CloudflareModel):
    success: bool
    errors: tuple[_ApiError, ...] = ()


class _HostnameEnvelope(_Envelope):
    result: _HostnameResult | None = None


@dataclass(frozen=True, slots=True)
class CloudflareCustomHostnames:
    """Cloudflare for SaaS custom hostnames for one zone.

    Holds the hostname identifier Cloudflare assigns rather than looking a hostname up
    by name on every call: the name is the customer's and can be re-registered, while
    the identifier names the object this platform actually created.
    """

    client: httpx.Client
    zone_id: str

    def create_hostname(self, hostname: str) -> ProviderCustomHostname:
        body = _CreateHostnameRequest(
            hostname=hostname.removeprefix(WILDCARD_PREFIX),
            ssl=_SslRequest(
                # The CNAME the customer publishes to route traffic here also proves
                # they control the name, so one record does both. Asking for a TXT as
                # well would add a second record that answers a question the first
                # already answered.
                method="http",
                type="dv",
                # One label, matching what the domain model promises a wildcard covers.
                wildcard=hostname.startswith(WILDCARD_PREFIX),
            ),
        )
        envelope = self._request(
            "POST",
            f"/zones/{self.zone_id}/custom_hostnames",
            _HostnameEnvelope,
            body=body,
        )
        return _hostname_state(envelope)

    def get_hostname(self, provider_hostname_id: str) -> ProviderCustomHostname | None:
        try:
            envelope = self._request(
                "GET",
                f"/zones/{self.zone_id}/custom_hostnames/{provider_hostname_id}",
                _HostnameEnvelope,
            )
        except _NotFound:
            return None
        return _hostname_state(envelope)

    def delete_hostname(self, provider_hostname_id: str) -> None:
        try:
            # The deleted object comes back carrying only its own identifier, which
            # the caller already holds, so nothing here reads the result.
            self._request(
                "DELETE",
                f"/zones/{self.zone_id}/custom_hostnames/{provider_hostname_id}",
                _Envelope,
            )
        except _NotFound:
            # Already gone is the state the caller asked for.
            return

    def _request[TEnvelope: _Envelope](
        self,
        method: str,
        path: str,
        envelope_type: type[TEnvelope],
        *,
        body: _CreateHostnameRequest | None = None,
    ) -> TEnvelope:
        try:
            response = self.client.request(
                method, path, json=None if body is None else body.model_dump()
            )
        except httpx.RequestError as exc:
            raise UpstreamUnavailableError(f"Cloudflare request failed: {exc!s}") from exc
        if response.status_code == httpx.codes.NOT_FOUND:
            raise _NotFound
        try:
            envelope = envelope_type.model_validate_json(response.content)
        except ValidationError as exc:
            raise UpstreamUnavailableError(
                f"Cloudflare returned an unreadable response ({response.status_code})"
            ) from exc
        if not envelope.success:
            _raise_api_error(envelope, status_code=response.status_code)
        return envelope


class _NotFound(Exception):
    pass


def build_client(*, api_token: str, timeout_seconds: float = 30.0) -> httpx.Client:
    return httpx.Client(
        base_url=API_BASE_URL,
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        },
        timeout=timeout_seconds,
    )


def _raise_api_error(envelope: _Envelope, *, status_code: int) -> NoReturn:
    detail = "; ".join(
        f"{'unknown' if error.code is None else error.code}: {error.message or 'unknown error'}"
        for error in envelope.errors
    )
    message = detail or f"Cloudflare rejected the request ({status_code})"
    # A rejected hostname is the caller's input; anything else is the edge's
    # problem, and only one of those is worth retrying.
    if httpx.codes.BAD_REQUEST <= status_code < httpx.codes.INTERNAL_SERVER_ERROR:
        raise InvalidInputError(message)
    raise UpstreamUnavailableError(message)


def _hostname_state(envelope: _HostnameEnvelope) -> ProviderCustomHostname:
    result = envelope.result
    if result is None:
        raise UpstreamUnavailableError("Cloudflare reported success without a custom hostname")
    phase, error_code = _phase(result.ssl.status, hostname_status=result.status)
    errors = [error.message for error in result.ssl.validation_errors or () if error.message]
    return ProviderCustomHostname(
        provider_hostname_id=result.id,
        phase=phase,
        required_records=_required_records(result),
        error_code=error_code,
        error_message="; ".join(errors)[:512] or None,
    )


def _phase(
    ssl_status: str,
    *,
    hostname_status: str,
) -> tuple[CustomDomainPhase, CustomDomainErrorCode | None]:
    if ssl_status == "active" and hostname_status == "active":
        return CustomDomainPhase.Ready, None
    if ssl_status in _AWAITING_SSL_STATES:
        return CustomDomainPhase.AwaitingVerification, None
    if ssl_status in _VALIDATING_SSL_STATES or hostname_status == "pending":
        return CustomDomainPhase.Validating, None
    return CustomDomainPhase.ActionRequired, CustomDomainErrorCode.CertificateFailed


def _required_records(result: _HostnameResult) -> tuple[DnsRecord, ...]:
    """Records still outstanding, each complete enough to be typed into a DNS form.

    Only what is genuinely pending: an issued certificate leaves nothing to add, and
    listing a satisfied record as outstanding sends a customer looking for a problem
    that is not there.
    """

    records = [
        DnsRecord(type="TXT", name=record.txt_name, value=record.txt_value)
        for record in result.ssl.validation_records or ()
        if record.txt_name and record.txt_value and record.status != "active"
    ]
    ownership = result.ownership_verification
    if result.status == "pending" and ownership is not None and ownership.name and ownership.value:
        records.append(
            DnsRecord(
                type=ownership.type.upper(),
                name=ownership.name,
                value=ownership.value,
            )
        )
    return tuple(records)


__all__ = ["API_BASE_URL", "CloudflareCustomHostnames", "build_client"]
