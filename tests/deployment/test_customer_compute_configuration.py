from __future__ import annotations

import subprocess
from pathlib import Path

import yaml
from pydantic import JsonValue, TypeAdapter

ROOT = Path(__file__).resolve().parents[2]
_YAML_MAPPING = TypeAdapter(dict[str, JsonValue])


def _mapping(value: JsonValue, context: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise AssertionError(f"{context} must be a mapping")
    return value


def test_compose_keeps_gateway_identity_credential_in_sidecar_only() -> None:
    rendered = subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "tailnet",
            "--profile",
            "tools",
            "config",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    config = _YAML_MAPPING.validate_python(yaml.safe_load(rendered.stdout))
    services = _mapping(config["services"], "services")
    control_plane = _mapping(services["control-plane"], "control-plane")
    cli = _mapping(services["cli"], "cli")
    gateway = _mapping(services["tailnet-gateway"], "tailnet gateway")
    control_plane_env = _mapping(control_plane["environment"], "control-plane environment")
    cli_env = _mapping(cli["environment"], "CLI environment")
    gateway_env = _mapping(gateway["environment"], "tailnet gateway environment")

    assert "LAZYCLOUD_TAILNET_AUTH_KEY" not in control_plane_env
    assert "LAZYCLOUD_TAILNET_AUTH_KEY" not in cli_env
    assert "LAZYCLOUD_BACKEND_ROUTE_AUTH_KEY" in cli_env
    assert "TS_AUTHKEY" in gateway_env
