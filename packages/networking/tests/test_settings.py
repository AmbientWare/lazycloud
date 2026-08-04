from __future__ import annotations

from pathlib import Path

import pytest
from networking.settings import (
    NETWORKING_CONFIG_FILE,
    BackendRouteSettings,
    ProviderNetworkClass,
    TailnetControlSettings,
    TailnetRuntimeSettings,
    validate_provider_network_configuration,
)
from networking.tailnet import TailnetRuntimeMode
from networking.tailnet_control import DEFAULT_TAILSCALE_API_URL
from pydantic import SecretStr, ValidationError
from shared.settings import CONFIG_DIR_ENV


def test_tailnet_settings_layer_yaml_under_env_and_never_carry_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / NETWORKING_CONFIG_FILE
    config.write_text(
        "tailnet:\n  control:\n    api_url: https://yaml.tailscale.example.test\n",
        encoding="utf-8",
    )

    # A file nothing addresses is a file nothing reads. The failure this guards
    # against is a config file left on one machine quietly changing results
    # everywhere it exists and nowhere it does not.
    assert TailnetControlSettings().api_url == DEFAULT_TAILSCALE_API_URL

    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path))
    assert TailnetControlSettings().api_url == "https://yaml.tailscale.example.test"

    monkeypatch.setenv("LAZYCLOUD_TAILNET_API_URL", "https://env.tailscale.example.test")
    assert TailnetControlSettings().api_url == "https://env.tailscale.example.test"

    from_init = TailnetControlSettings(api_url="https://init.tailscale.example.test")
    assert from_init.api_url == "https://init.tailscale.example.test"

    config.write_text(
        "tailnet:\n  control:\n    oauth_client_id: tenant-client\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="oauth_client_id"):
        TailnetControlSettings()


def test_tailnet_control_rejects_partial_cleanup_credentials() -> None:
    settings = TailnetControlSettings(
        oauth_client_id="oauth-client",
        oauth_client_secret=SecretStr(""),
    )

    with pytest.raises(ValueError, match="OAuth client ID and secret"):
        settings.to_control_config()


def test_remote_provider_gate_reports_all_missing_security_requirements() -> None:
    # Sidecar with nothing configured: a managed runtime without an auth key is
    # rejected at construction and cannot reach this gate.
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
            internal_origin="http://control-plane:9000",
            presigned_origin="http://object-store:9002",
            runtime=runtime,
            control=control,
            backend_route=route,
        )

    message = str(error.value)
    assert "gateway HTTP URL must be an HTTPS origin" in message
    # A Compose service name resolves on the control-plane host and nowhere
    # else. Accepting it here produces remote machines that enrol, report
    # healthy, and then fail every call they make.
    assert "'control-plane' is unreachable from a remote machine" in message
    # The presigned endpoint is the third origin a remote node dials, and the
    # only one it does not use while enrolling: a local value here produces a
    # machine that joins and reports ready before failing to read its image.
    assert "'object-store' is unreachable from a remote machine" in message
    assert "tailnet hostname is required" in message
    assert "tailnet sidecar socket path is required" in message
    assert "tailnet agent and control-plane tags must be distinct" in message
    assert "Tailscale OAuth client ID is required" in message
    assert "Tailscale OAuth client secret is required" in message
    assert "Tailscale API URL must be an HTTPS URL without query or fragment" in message
    assert "backend route authentication key must be at least 32 bytes" in message


def test_tailnet_tags_reject_invalid_values() -> None:
    with pytest.raises(ValidationError, match="tag:<name>"):
        TailnetControlSettings(agent_tag="provider_agent")


def test_managed_runtime_requires_an_auth_key_when_enabled() -> None:
    with pytest.raises(ValueError, match="gateway Tailscale auth key"):
        TailnetRuntimeSettings(
            mode=TailnetRuntimeMode.Managed,
            auth_key=SecretStr(""),
        )
