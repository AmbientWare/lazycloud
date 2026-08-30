from __future__ import annotations

from dataclasses import dataclass

from networking.private_network_control import (
    PrivateNetworkActiveSite,
    PrivateNetworkControlError,
    PrivateNetworkControlErrorCode,
    PrivateNetworkCredential,
    PrivateNetworkSite,
)
from shared.errors import InvalidInputError, UpstreamUnavailableError

from provider_pangolin.client import PangolinApiError, PangolinClient

AGENT_ROUTE_PROXY_PORT = 29443


@dataclass(frozen=True, slots=True)
class PangolinPrivateNetworkControl:
    client: PangolinClient

    def create_site(self, *, name: str) -> PrivateNetworkCredential:
        try:
            credential = self.client.create_site(name=name)
        except (PangolinApiError, InvalidInputError, UpstreamUnavailableError) as exc:
            raise _control_error(exc, "Pangolin site creation failed") from exc
        return PrivateNetworkCredential(
            name=name,
            endpoint=self.client.endpoint,
            site_id=str(credential.site_id),
            connector_id=credential.newt_id,
            secret=credential.secret,
        )

    def find_site(
        self,
        *,
        site_id: str,
        name: str,
        connector_id: str,
    ) -> PrivateNetworkSite | None:
        try:
            normalized = int(site_id)
        except ValueError as exc:
            raise PrivateNetworkControlError(
                PrivateNetworkControlErrorCode.InvalidConfiguration,
                "Pangolin site ID must be an integer",
                retryable=False,
            ) from exc
        try:
            site = self.client.get_site(normalized)
        except (PangolinApiError, InvalidInputError, UpstreamUnavailableError) as exc:
            raise _control_error(exc, "Pangolin site lookup failed") from exc
        if site is None or site.name != name or site.newt_id != connector_id:
            return None
        return PrivateNetworkSite(
            site_id=str(site.site_id),
            name=site.name,
            online=site.online,
        )

    def activate_site(self, site_id: str) -> PrivateNetworkActiveSite:
        try:
            normalized = int(site_id)
        except ValueError as exc:
            raise PrivateNetworkControlError(
                PrivateNetworkControlErrorCode.InvalidConfiguration,
                "Pangolin site ID must be an integer",
                retryable=False,
            ) from exc
        try:
            site = self.client.get_site(normalized)
            if site is None:
                raise PrivateNetworkControlError(
                    PrivateNetworkControlErrorCode.NotFound,
                    "Pangolin site not found",
                    retryable=False,
                )
            resource = self.client.ensure_agent_private_resource(
                site_id=normalized,
                destination_port=AGENT_ROUTE_PROXY_PORT,
            )
        except PrivateNetworkControlError:
            raise
        except (PangolinApiError, InvalidInputError, UpstreamUnavailableError) as exc:
            raise _control_error(exc, "Pangolin private resource activation failed") from exc
        return PrivateNetworkActiveSite(
            site_id=str(site.site_id),
            name=site.name,
            resource_id=str(resource.site_resource_id),
            address=resource.alias_address,
            online=site.online,
        )

    def delete_resource(self, resource_id: str) -> None:
        try:
            normalized = int(resource_id)
        except ValueError as exc:
            raise PrivateNetworkControlError(
                PrivateNetworkControlErrorCode.InvalidConfiguration,
                "Pangolin private resource ID must be an integer",
                retryable=False,
            ) from exc
        try:
            self.client.delete_private_resource(normalized)
        except (PangolinApiError, InvalidInputError, UpstreamUnavailableError) as exc:
            raise _control_error(exc, "Pangolin private resource deletion failed") from exc

    def delete_site(self, site_id: str) -> None:
        try:
            normalized = int(site_id)
        except ValueError as exc:
            raise PrivateNetworkControlError(
                PrivateNetworkControlErrorCode.InvalidConfiguration,
                "Pangolin site ID must be an integer",
                retryable=False,
            ) from exc
        try:
            self.client.delete_site(normalized)
        except (PangolinApiError, InvalidInputError, UpstreamUnavailableError) as exc:
            raise _control_error(exc, "Pangolin site deletion failed") from exc


def _control_error(exc: Exception, fallback: str) -> PrivateNetworkControlError:
    if isinstance(exc, InvalidInputError):
        return PrivateNetworkControlError(
            PrivateNetworkControlErrorCode.InvalidConfiguration,
            str(exc),
            retryable=False,
        )
    if isinstance(exc, PangolinApiError):
        if exc.status_code == 401:
            code = PrivateNetworkControlErrorCode.AuthenticationFailed
        elif exc.status_code == 403:
            code = PrivateNetworkControlErrorCode.PermissionDenied
        elif exc.status_code == 404:
            code = PrivateNetworkControlErrorCode.NotFound
        elif exc.status_code == 409:
            code = PrivateNetworkControlErrorCode.Conflict
        elif exc.status_code == 429:
            code = PrivateNetworkControlErrorCode.RateLimited
        else:
            code = PrivateNetworkControlErrorCode.UpstreamUnavailable
        return PrivateNetworkControlError(code, str(exc), retryable=exc.retryable)
    return PrivateNetworkControlError(
        PrivateNetworkControlErrorCode.UpstreamUnavailable,
        fallback,
        retryable=True,
    )


__all__ = ["PangolinPrivateNetworkControl"]
