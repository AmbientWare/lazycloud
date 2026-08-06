from __future__ import annotations

import re
from enum import StrEnum
from urllib.parse import urlparse

from compute.agent_control import TailnetConfig, host_is_unreachable_from_a_remote_machine
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import CONTROL_PLANE_TAILNET_HOSTNAME, ENV_PREFIX

from networking.dialer import BackendRouteDialerConfig
from networking.routing import BackendRouteAuthenticator
from networking.tailnet import (
    TailnetRuntimeMode,
    TailnetRuntimeOptions,
)
from networking.tailnet_control import (
    DEFAULT_TAILNET_AUTH_KEY_TTL_SECONDS,
    DEFAULT_TAILSCALE_API_URL,
    TailscaleTailnetControlConfig,
)

_TAILNET_TAG_PATTERN = re.compile(r"tag:[a-z0-9][a-z0-9-]{0,62}")


class ProviderNetworkClass(StrEnum):
    ClusterLocal = "cluster_local"
    Remote = "remote"


class TailnetRuntimeSettings(TailnetRuntimeOptions, BaseSettings):
    hostname: str = CONTROL_PLANE_TAILNET_HOSTNAME

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_TAILNET_",
        extra="forbid",
    )

    @field_validator(
        "control_url",
        "hostname",
        "state_dir",
        "socket_path",
        "tailscale_binary",
        "tailscaled_binary",
    )
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()

    def to_agent_config(self) -> TailnetConfig:
        return TailnetConfig(control_url=self.control_url)


class TailnetControlSettings(BaseSettings):
    oauth_client_id: str = ""
    oauth_client_secret: SecretStr = SecretStr("")
    agent_tag: str = "tag:lazycloud-agent"
    control_plane_tag: str = "tag:lazycloud-control-plane"
    api_url: str = DEFAULT_TAILSCALE_API_URL
    auth_key_ttl_seconds: int = Field(
        default=DEFAULT_TAILNET_AUTH_KEY_TTL_SECONDS,
        ge=30,
        le=3600,
    )

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_TAILNET_",
        extra="forbid",
    )

    @field_validator("oauth_client_id", "api_url")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("agent_tag", "control_plane_tag")
    @classmethod
    def normalize_tag(cls, value: str) -> str:
        return normalize_tailnet_tag(value)

    def validated_tags(self) -> tuple[str, str]:
        tags = (self.agent_tag, self.control_plane_tag)
        if len(set(tags)) != len(tags):
            # Each tag carries a different grant. Sharing one collapses both
            # postures into whichever is broader.
            raise ValueError("tailnet agent and control-plane tags must be distinct")
        return tags

    def to_control_config(self) -> TailscaleTailnetControlConfig:
        oauth_client_id = self.oauth_client_id
        oauth_client_secret = self.oauth_client_secret.get_secret_value().strip()
        self.validated_tags()
        if not oauth_client_id or not oauth_client_secret:
            raise ValueError("Tailscale OAuth client ID and secret are required by tailnet control")
        return TailscaleTailnetControlConfig(
            api_url=self.api_url,
            oauth_client_id=oauth_client_id,
            oauth_client_secret=self.oauth_client_secret,
            agent_tag=self.agent_tag,
            control_plane_tag=self.control_plane_tag,
            auth_key_ttl_seconds=self.auth_key_ttl_seconds,
        )


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


def normalize_tailnet_tag(value: str) -> str:
    normalized = value.strip().lower()
    if _TAILNET_TAG_PATTERN.fullmatch(normalized) is None:
        raise ValueError("Tailscale tag must use the tag:<name> form")
    return normalized


def validate_provider_network_configuration(
    network_class: ProviderNetworkClass,
    *,
    gateway_origin: str,
    internal_origin: str,
    presigned_origin: str = "",
    runtime: TailnetRuntimeSettings,
    control: TailnetControlSettings,
    backend_route: BackendRouteSettings,
) -> None:
    if network_class is ProviderNetworkClass.ClusterLocal:
        return

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
    # The origin nodes and workers dial is not checked here any more. It is no
    # longer configured: the control plane publishes the host of the tailnet
    # device it actually registered, and this gate already refuses a connected
    # deployment whose tailnet is disabled — so the Compose service name that
    # used to reach this check can no longer be what a remote node receives.
    # Validating the configured value would assert something nothing reads.
    #
    # The third origin a remote node dials. Unlike the other two it is not used
    # during enrolment, so a wrong value here starts a machine that joins,
    # reports ready, accepts work, and only then fails to read its image.
    presigned_host = urlparse(presigned_origin).hostname or ""
    if presigned_origin and not presigned_host:
        issues.append("object store presigned endpoint must include a host")
    elif presigned_host and host_is_unreachable_from_a_remote_machine(presigned_host):
        issues.append(
            f"object store presigned endpoint host {presigned_host!r} is unreachable from a "
            "remote machine; set LAZYCLOUD_OBJECT_STORE_PRESIGNED_ENDPOINT_URL to the control "
            "plane's tailnet origin"
        )
    if runtime.mode is TailnetRuntimeMode.Disabled:
        issues.append("tailnet runtime mode must be managed")
    if not runtime.hostname:
        issues.append("tailnet hostname is required")
    # No static auth key is required: the control plane mints its own from the
    # OAuth credentials this function already insists on, which is also what
    # carries its tag.

    try:
        control.validated_tags()
    except ValueError as exc:
        issues.append(str(exc))
    if not control.oauth_client_id:
        issues.append("Tailscale OAuth client ID is required")
    if not control.oauth_client_secret.get_secret_value().strip():
        issues.append("Tailscale OAuth client secret is required")
    if not _is_https_api_url(control.api_url):
        issues.append("Tailscale API URL must be an HTTPS URL without query or fragment")
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


def _is_https_api_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


__all__ = [
    "BackendRouteSettings",
    "ProviderNetworkClass",
    "TailnetControlSettings",
    "TailnetRuntimeSettings",
    "normalize_tailnet_tag",
    "validate_provider_network_configuration",
]
