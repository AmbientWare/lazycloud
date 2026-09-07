from __future__ import annotations

from urllib.parse import urlparse

from compute.agent_control import host_is_unreachable_from_a_remote_machine
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX

from networking.dialer import BackendRouteDialerConfig
from networking.routing import BackendRouteAuthenticator


class BackendRouteSettings(BaseSettings):
    auth_key: SecretStr = SecretStr("")

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_BACKEND_ROUTE_",
        extra="forbid",
    )

    def to_authenticator(self) -> BackendRouteAuthenticator:
        return BackendRouteAuthenticator(self.auth_key)

    def to_dialer_config(self) -> BackendRouteDialerConfig:
        return BackendRouteDialerConfig(auth_key=self.to_authenticator().secret)


def validate_remote_provider_network_configuration(
    *,
    gateway_origin: str,
    presigned_origin: str = "",
    backend_route: BackendRouteSettings,
) -> None:
    issues: list[str] = []
    if not _is_https_origin(gateway_origin):
        issues.append("gateway HTTP URL must be an HTTPS origin")
    # A managed node enrols against the public origin, so a loopback or LAN
    # address here is a pool that launches machines which can never report.
    gateway_host = urlparse(gateway_origin).hostname or ""
    if not gateway_host:
        issues.append("gateway HTTP URL must include a host")
    elif host_is_unreachable_from_a_remote_machine(gateway_host):
        issues.append(
            f"gateway public origin host {gateway_host!r} is unreachable from a remote "
            "machine; set LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL to the deployment's public "
            "ingress origin"
        )
    presigned_host = urlparse(presigned_origin).hostname or ""
    if presigned_origin and not presigned_host:
        issues.append("object store presigned endpoint must include a host")
    elif presigned_host and host_is_unreachable_from_a_remote_machine(presigned_host):
        issues.append(
            f"object store presigned endpoint host {presigned_host!r} is unreachable from a "
            "remote machine; configure the deployment's R2 account endpoint"
        )
    try:
        BackendRouteAuthenticator(backend_route.auth_key)
    except ValueError as exc:
        issues.append(str(exc))

    if issues:
        raise ValueError(
            "remote provider network configuration is incomplete: " + "; ".join(issues)
        )


def _is_https_origin(value: str) -> bool:
    parsed = urlparse(value.strip())
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and not parsed.path
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
    )


__all__ = [
    "BackendRouteSettings",
    "validate_remote_provider_network_configuration",
]
