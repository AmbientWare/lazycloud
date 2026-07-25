from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml
from pydantic import JsonValue, TypeAdapter

ROOT = Path(__file__).resolve().parents[2]
_YAML_MAPPING = TypeAdapter(dict[str, JsonValue])


def _yaml_mapping(value: str) -> dict[str, JsonValue]:
    return _YAML_MAPPING.validate_python(yaml.safe_load(value))


def _mapping(value: JsonValue, context: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise AssertionError(f"{context} must be a mapping")
    return value


def _sequence(value: JsonValue, context: str) -> list[JsonValue]:
    if not isinstance(value, list):
        raise AssertionError(f"{context} must be a sequence")
    return value


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        raise AssertionError(f"{context} must be text")
    return value


def _render_compose(env_file: Path) -> str:
    rendered = subprocess.run(
        ["docker", "compose", "--env-file", str(env_file), "config"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env={"HOME": os.environ["HOME"], "PATH": os.environ["PATH"]},
    )
    return rendered.stdout


def test_connected_object_store_does_not_retarget_local_garage_bootstrap(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "LAZYCLOUD_OBJECT_STORE_BUCKET=connected-control-bucket",
                "LAZYCLOUD_OBJECT_STORE_ENDPOINT_URL=https://s3.us-east-1.amazonaws.com",
                "LAZYCLOUD_OBJECT_STORE_REGION_NAME=us-east-1",
                "LAZYCLOUD_OBJECT_STORE_ACCESS_KEY_ID=temporary-connected-key",
                "LAZYCLOUD_OBJECT_STORE_SECRET_ACCESS_KEY=temporary-connected-secret",
                "",
            )
        ),
        encoding="utf-8",
    )

    config = _yaml_mapping(_render_compose(env_file))
    services = _mapping(config["services"], "services")
    garage = _mapping(services["object-store"], "object-store")
    garage_environment = _mapping(garage["environment"], "object-store environment")
    bootstrap = _mapping(services["object-store-bucket"], "object-store-bucket")
    bootstrap_environment = _mapping(
        bootstrap["environment"],
        "object-store-bucket environment",
    )
    control_plane = _mapping(services["control-plane"], "control-plane")
    control_environment = _mapping(
        control_plane["environment"],
        "control-plane environment",
    )

    assert garage_environment["GARAGE_DEFAULT_BUCKET"] == "lazycloud-objects"
    assert garage_environment["GARAGE_DEFAULT_ACCESS_KEY"] == "lazycloud-local"
    assert garage_environment["GARAGE_DEFAULT_SECRET_KEY"] == "lazycloud-local-secret"
    assert bootstrap_environment["GARAGE_ENDPOINT_URL"] == "http://object-store:9000"
    assert bootstrap_environment["AWS_ACCESS_KEY_ID"] == "lazycloud-local"
    assert bootstrap_environment["AWS_SECRET_ACCESS_KEY"] == "lazycloud-local-secret"
    assert control_environment["LAZYCLOUD_OBJECT_STORE_BUCKET"] == "connected-control-bucket"
    assert (
        control_environment["LAZYCLOUD_OBJECT_STORE_ENDPOINT_URL"]
        == "https://s3.us-east-1.amazonaws.com"
    )
