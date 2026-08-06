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


def test_compose_holds_no_long_lived_tailnet_identity_credential() -> None:
    """No service is handed a tailnet auth key, because none needs one.

    The control plane mints its own from its OAuth client at the moment it joins,
    which is also what carries its tag. A static key in a deployment file is both
    a long-lived credential and a silent way to register an untagged device.
    """
    rendered = subprocess.run(
        ["docker", "compose", "--profile", "tools", "config"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    config = _YAML_MAPPING.validate_python(yaml.safe_load(rendered.stdout))
    services = _mapping(config["services"], "services")

    for name, service in services.items():
        environment = _mapping(service, name).get("environment") or {}
        keys = set(_mapping(environment, f"{name} environment"))
        assert "LAZYCLOUD_TAILNET_AUTH_KEY" not in keys, name
        assert "TS_AUTHKEY" not in keys, name

    cli_env = _mapping(_mapping(services["cli"], "cli")["environment"], "CLI environment")
    assert "LAZYCLOUD_BACKEND_ROUTE_AUTH_KEY" in cli_env
