from __future__ import annotations

import re
from enum import StrEnum
from typing import Self
from urllib.parse import urlparse

from compute.agent_control import TailnetConfig, host_is_unreachable_from_a_remote_machine
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import CONTROL_PLANE_TAILNET_HOSTNAME, ENV_PREFIX

from networking.dialer import BackendRouteDialerConfig
from networking.routing import BackendRouteAuthenticator
from networking.tailnet import (
    TailnetRuntimeMode,
    TailnetRuntimeOptions,
)
from networking.tailnet_control import (
    DEFAULT_POOL_BOOTSTRAP_KEY_REFRESH_SECONDS,
    DEFAULT_POOL_BOOTSTRAP_KEY_TTL_SECONDS,
    DEFAULT_POOL_BOOTSTRAP_TAG,
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
    socket_path: str = "/var/run/tailscale/tailscaled.sock"

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_TAILNET_",
        extra="ignore",
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

    @model_validator(mode="after")
    def managed_runtime_requires_auth_key(self) -> Self:
        if self.mode is TailnetRuntimeMode.Managed and not self.auth_key.get_secret_value().strip():
            raise ValueError("gateway Tailscale auth key is required by the tailnet runtime")
        return self

    def to_agent_config(self) -> TailnetConfig:
        return TailnetConfig(control_url=self.control_url)


class TailnetControlSettings(BaseSettings):
    oauth_client_id: str = ""
    oauth_client_secret: SecretStr = SecretStr("")
    agent_tag: str = "tag:lazycloud-agent"
    control_plane_tag: str = "tag:lazycloud-control-plane"
    pool_bootstrap_tag: str = DEFAULT_POOL_BOOTSTRAP_TAG
    api_url: str = DEFAULT_TAILSCALE_API_URL
    auth_key_ttl_seconds: int = Field(
        default=DEFAULT_TAILNET_AUTH_KEY_TTL_SECONDS,
        ge=30,
        le=3600,
    )
    pool_bootstrap_key_ttl_seconds: int = Field(
        default=DEFAULT_POOL_BOOTSTRAP_KEY_TTL_SECONDS,
        ge=86_400,
        le=DEFAULT_POOL_BOOTSTRAP_KEY_TTL_SECONDS,
    )
    pool_bootstrap_key_refresh_seconds: int = Field(
        default=DEFAULT_POOL_BOOTSTRAP_KEY_REFRESH_SECONDS,
        ge=3600,
        le=DEFAULT_POOL_BOOTSTRAP_KEY_TTL_SECONDS,
    )

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_TAILNET_",
        extra="ignore",
    )

    @field_validator("oauth_client_id", "api_url")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("agent_tag", "control_plane_tag", "pool_bootstrap_tag")
    @classmethod
    def normalize_tag(cls, value: str) -> str:
        return normalize_tailnet_tag(value)

    def validated_tags(self) -> tuple[str, str, str]:
        tags = (self.agent_tag, self.control_plane_tag, self.pool_bootstrap_tag)
        if len(set(tags)) != len(tags):
            # Each tag carries a different grant. Sharing one collapses three
            # postures into whichever is broadest.
            raise ValueError("tailnet agent, control-plane, and bootstrap tags must be distinct")
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
            pool_bootstrap_tag=self.pool_bootstrap_tag,
            auth_key_ttl_seconds=self.auth_key_ttl_seconds,
            pool_bootstrap_key_ttl_seconds=self.pool_bootstrap_key_ttl_seconds,
        )


class BackendRouteSettings(BaseSettings):
    auth_key: SecretStr = SecretStr("")

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_BACKEND_ROUTE_",
        extra="ignore",
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
    runtime: TailnetRuntimeSettings,
    control: TailnetControlSettings,
    backend_route: BackendRouteSettings,
) -> None:
    if network_class is ProviderNetworkClass.ClusterLocal:
        return

    issues: list[str] = []
    if not _is_https_origin(gateway_origin):
        issues.append("gateway HTTP URL must be an HTTPS origin")
    # Nodes and workers dial the internal origin, not the public one. A Compose
    # service name or a LAN address resolves on the control-plane host and
    # nowhere else, and the machine that discovers that is an EC2 instance
    # twenty minutes into a boot it will never finish.
    internal_host = urlparse(internal_origin).hostname or ""
    if not internal_host:
        issues.append("internal control-plane origin must include a host")
    elif host_is_unreachable_from_a_remote_machine(internal_host):
        issues.append(
            f"internal control-plane origin host {internal_host!r} is unreachable from a "
            "remote machine; set LAZYCLOUD_GATEWAY_RUNTIME_HTTP_URL to the control plane's "
            "tailnet origin"
        )
    if runtime.mode is TailnetRuntimeMode.Disabled:
        issues.append("tailnet runtime mode must be sidecar or managed")
    if not runtime.hostname:
        issues.append("tailnet hostname is required")
    if runtime.mode is TailnetRuntimeMode.Sidecar and not runtime.socket_path:
        issues.append("tailnet sidecar socket path is required")
    if (
        runtime.mode is TailnetRuntimeMode.Managed
        and not runtime.auth_key.get_secret_value().strip()
    ):
        issues.append("managed tailnet runtime auth key is required")

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
