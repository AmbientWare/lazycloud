from __future__ import annotations

import pytest
from networking.settings import (
    BackendRouteSettings,
    ProviderNetworkClass,
    TailnetControlSettings,
    TailnetRuntimeSettings,
    validate_provider_network_configuration,
)
from networking.tailnet import TailnetRuntimeMode
from pydantic import SecretStr, ValidationError


def test_tailnet_control_rejects_partial_cleanup_credentials() -> None:
    settings = TailnetControlSettings(
        oauth_client_id="oauth-client",
        oauth_client_secret=SecretStr(""),
    )

    with pytest.raises(ValueError, match="OAuth client ID and secret"):
        settings.to_control_config()


def test_remote_provider_gate_reports_all_missing_security_requirements() -> None:
    # Sidecar with nothing configured: a managed runtime without an auth key is
    # now rejected at construction, so that case can no longer reach this gate.
    runtime = TailnetRuntimeSettings(
        mode=TailnetRuntimeMode.Sidecar,
        hostname="",
        socket_path="",
    )
    control = TailnetControlSettings(
        oauth_client_id="",
        oauth_client_secret=SecretStr(""),
        agent_tag="tag:shared",
        control_plane_tag="tag:shared",
        api_url="http://user:secret@tailscale.example.test?insecure=true",
    )
    route = BackendRouteSettings(auth_key=SecretStr("short"))

    with pytest.raises(ValueError) as error:
        validate_provider_network_configuration(
            ProviderNetworkClass.Remote,
            gateway_origin="https://user@control.example.test/path",
            runtime=runtime,
            control=control,
            backend_route=route,
        )

    message = str(error.value)
    assert "gateway HTTP URL must be an HTTPS origin" in message
    assert "tailnet hostname is required" in message
    assert "tailnet sidecar socket path is required" in message
    assert "tailnet agent and control-plane tags must be distinct" in message
    assert "Tailscale OAuth client ID is required" in message
    assert "Tailscale OAuth client secret is required" in message
    assert "Tailscale API URL must be an HTTPS URL without query or fragment" in message
    assert "backend route authentication key must be at least 32 bytes" in message


def test_distinct_tailnet_tags_are_required() -> None:
    control = TailnetControlSettings(
        agent_tag="tag:shared",
        control_plane_tag="tag:shared",
    )

    with pytest.raises(ValueError, match="must be distinct"):
        control.validated_tags()


def test_tailnet_tags_reject_invalid_values() -> None:
    with pytest.raises(ValidationError, match="tag:<name>"):
        TailnetControlSettings(agent_tag="provider_agent")


def test_managed_runtime_requires_an_auth_key_when_enabled() -> None:
    with pytest.raises(ValueError, match="gateway Tailscale auth key"):
        TailnetRuntimeSettings(
            mode=TailnetRuntimeMode.Managed,
            auth_key=SecretStr(""),
        )
