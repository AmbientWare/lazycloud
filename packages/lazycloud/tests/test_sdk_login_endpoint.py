from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from lazycloud.cli.identity import resolve_login_endpoint, resolve_login_token
from lazycloud.cli.main import build_public_cli
from lazycloud.config import (
    PACKAGED_DEFAULT_ENDPOINT,
    ClientProfile,
    get_profile,
    reset_settings_cache,
)
from typer.testing import CliRunner


@pytest.fixture(autouse=True)
def reset_config_settings_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[None]:
    monkeypatch.delenv("LAZYCLOUD_ENDPOINT", raising=False)
    monkeypatch.delenv("LAZYCLOUD_TOKEN", raising=False)
    monkeypatch.delenv("LAZYCLOUD_HOME", raising=False)
    monkeypatch.delenv("LAZYCLOUD_CONFIG", raising=False)
    # Isolate from any `.env` in the working tree so env resolution is
    # deterministic when no endpoint is configured.
    monkeypatch.chdir(tmp_path)
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.mark.parametrize("source", ["flag", "environment", "profile", "packaged", "token"])
def test_login_endpoint_and_token_precedence(
    source: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if source == "flag":
        monkeypatch.setenv("LAZYCLOUD_ENDPOINT", "https://env.example")
        reset_settings_cache()
        profile = ClientProfile(name="default", endpoint="https://profile.example")
        assert resolve_login_endpoint("https://flag.example", profile) == "https://flag.example"
    elif source == "environment":
        monkeypatch.setenv("LAZYCLOUD_ENDPOINT", "https://env.example")
        reset_settings_cache()
        profile = ClientProfile(name="default", endpoint="https://profile.example")
        assert resolve_login_endpoint(None, profile) == "https://env.example"
    elif source == "profile":
        profile = ClientProfile(name="default", endpoint="https://profile.example")
        assert resolve_login_endpoint(None, profile) == "https://profile.example"
    elif source == "packaged":
        profile = ClientProfile(name="default")
        assert resolve_login_endpoint(None, profile) == PACKAGED_DEFAULT_ENDPOINT
        assert resolve_login_endpoint("", profile) == PACKAGED_DEFAULT_ENDPOINT
    else:
        profile = ClientProfile(name="default", token="stored-secret")
        monkeypatch.setenv("LAZYCLOUD_TOKEN", "environment-secret")
        reset_settings_cache()
        assert resolve_login_token("provided-secret", profile) == (
            "provided-secret",
            "provided",
        )
        assert resolve_login_token(None, profile) == (
            "environment-secret",
            "environment",
        )
        monkeypatch.delenv("LAZYCLOUD_TOKEN")
        reset_settings_cache()
        assert resolve_login_token(None, profile) == ("stored-secret", "stored")
        assert resolve_login_token("", profile) == ("", "provided")


def test_login_accepts_environment_token_without_exposing_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    secret = "environment-bootstrap-secret"
    monkeypatch.setenv("LAZYCLOUD_HOME", str(state))
    monkeypatch.setenv("LAZYCLOUD_TOKEN", secret)
    reset_settings_cache()

    result = CliRunner().invoke(
        build_public_cli(),
        ["--json", "login", "--endpoint", "http://127.0.0.1:8000"],
    )

    assert result.exit_code == 0, result.output
    assert secret not in result.output
    assert json.loads(result.output) == {
        "activated": True,
        "endpoint": "http://127.0.0.1:8000",
        "name": "default",
        "tls": False,
        "token": "set",
        "token_source": "environment",
        "workspace": "",
    }
    assert get_profile(apply_env=False).token == secret
