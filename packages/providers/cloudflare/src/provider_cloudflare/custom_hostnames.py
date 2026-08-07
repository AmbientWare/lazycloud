from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx
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
        payload: dict[str, Any] = {
            "hostname": hostname.removeprefix(WILDCARD_PREFIX),
            "ssl": {
                # The CNAME the customer publishes to route traffic here also proves
                # they control the name, so one record does both. Asking for a TXT as
                # well would add a second record that answers a question the first
                # already answered.
                "method": "http",
                "type": "dv",
                # One label, matching what the domain model promises a wildcard covers.
                "wildcard": hostname.startswith(WILDCARD_PREFIX),
            },
        }
        result = self._request("POST", f"/zones/{self.zone_id}/custom_hostnames", json=payload)
        return _hostname_state(result)

    def get_hostname(self, provider_hostname_id: str) -> ProviderCustomHostname | None:
        try:
            result = self._request(
                "GET",
                f"/zones/{self.zone_id}/custom_hostnames/{provider_hostname_id}",
            )
        except _NotFound:
            return None
        return _hostname_state(result)

    def delete_hostname(self, provider_hostname_id: str) -> None:
        try:
            self._request(
                "DELETE",
                f"/zones/{self.zone_id}/custom_hostnames/{provider_hostname_id}",
            )
        except _NotFound:
            # Already gone is the state the caller asked for.
            return

    def _request(self, method: str, path: str, *, json: dict[str, Any] | None = None) -> Any:
        try:
            response = self.client.request(method, path, json=json)
        except httpx.RequestError as exc:
            raise UpstreamUnavailableError(f"Cloudflare request failed: {exc!s}") from exc
        if response.status_code == httpx.codes.NOT_FOUND:
            raise _NotFound
        try:
            body = response.json()
        except ValueError as exc:
            raise UpstreamUnavailableError(
                f"Cloudflare returned a non-JSON response ({response.status_code})"
            ) from exc
        if not body.get("success", False):
            self._raise_api_error(body, status_code=response.status_code)
        return body.get("result", {})

    @staticmethod
    def _raise_api_error(body: dict[str, Any], *, status_code: int) -> None:
        errors = body.get("errors") or []
        detail = "; ".join(
            f"{item.get('code', 'unknown')}: {item.get('message', 'unknown error')}"
            for item in errors
        )
        message = detail or f"Cloudflare rejected the request ({status_code})"
        # A rejected hostname is the caller's input; anything else is the edge's
        # problem, and only one of those is worth retrying.
        if httpx.codes.BAD_REQUEST <= status_code < httpx.codes.INTERNAL_SERVER_ERROR:
            raise InvalidInputError(message)
        raise UpstreamUnavailableError(message)


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


def _hostname_state(result: dict[str, Any]) -> ProviderCustomHostname:
    ssl = result.get("ssl") or {}
    ssl_status = str(ssl.get("status") or "")
    errors = [str(item) for item in (ssl.get("validation_errors") or []) if item]
    phase, error_code = _phase(ssl_status, hostname_status=str(result.get("status") or ""))
    return ProviderCustomHostname(
        provider_hostname_id=str(result.get("id") or ""),
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


def _required_records(result: dict[str, Any]) -> tuple[DnsRecord, ...]:
    """Records still outstanding, each complete enough to be typed into a DNS form.

    Only what is genuinely pending: an issued certificate leaves nothing to add, and
    listing a satisfied record as outstanding sends a customer looking for a problem
    that is not there.
    """

    records: list[DnsRecord] = []
    ssl = result.get("ssl") or {}
    for record in ssl.get("validation_records") or []:
        name, value = record.get("txt_name"), record.get("txt_value")
        if name and value and record.get("status") != "active":
            records.append(DnsRecord(type="TXT", name=str(name), value=str(value)))
    ownership = result.get("ownership_verification") or {}
    if result.get("status") == "pending" and ownership.get("name") and ownership.get("value"):
        records.append(
            DnsRecord(
                type=str(ownership.get("type") or "TXT").upper(),
                name=str(ownership["name"]),
                value=str(ownership["value"]),
            )
        )
    return tuple(records)


__all__ = ["API_BASE_URL", "CloudflareCustomHostnames", "build_client"]
