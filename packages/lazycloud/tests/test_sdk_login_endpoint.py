from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from lazycloud.cli.identity import DeviceLoginResult, resolve_login_endpoint, resolve_login_token
from lazycloud.cli.main import build_public_cli
from lazycloud.cli.main import start as client_start
from lazycloud.config import (
    PACKAGED_DEFAULT_ENDPOINT,
    ClientProfile,
    get_profile,
    reset_settings_cache,
    set_profile,
)
from shared.http.errors import HttpApiError
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
        monkeypatch.setenv("LAZYCLOUD_TOKEN", "environment-secret")
        reset_settings_cache()
        assert resolve_login_token("provided-secret") == (
            "provided-secret",
            "provided",
        )
        assert resolve_login_token(None) == (
            "environment-secret",
            "environment",
        )
        monkeypatch.delenv("LAZYCLOUD_TOKEN")
        reset_settings_cache()
        assert resolve_login_token(None) == ("", "device")
        assert resolve_login_token("") == ("", "provided")


def test_login_accepts_environment_token_without_exposing_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    secret = "environment-bootstrap-secret"
    monkeypatch.setenv("LAZYCLOUD_HOME", str(state))
    monkeypatch.setenv("LAZYCLOUD_TOKEN", secret)
    reset_settings_cache()
    validated: list[tuple[str, str | None, str]] = []

    class ValidationClient:
        def current(self) -> object:
            return object()

    def validation_client(
        endpoint: str,
        *,
        token: str | None,
        timeout_seconds: float = 10.0,
        workspace: str = "default",
    ) -> ValidationClient:
        assert timeout_seconds == 10.0
        validated.append((endpoint, token, workspace))
        return ValidationClient()

    monkeypatch.setattr(
        "lazycloud.cli.identity.WorkspaceControlClient.from_endpoint",
        validation_client,
    )

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
    assert validated == [("http://127.0.0.1:8000", secret, "")]
    assert get_profile(apply_env=False).token == secret


def test_plain_login_replaces_a_stale_token_only_after_device_authentication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    stale_token = "stale-profile-secret"
    fresh_token = "fresh-device-secret"
    monkeypatch.setenv("LAZYCLOUD_HOME", str(state))
    reset_settings_cache()
    set_profile(
        ClientProfile(
            endpoint="https://control.example",
            workspace="team",
            token=stale_token,
        )
    )
    validated: list[tuple[str, str | None, str]] = []

    class ValidationClient:
        def current(self) -> object:
            return object()

    def validation_client(
        endpoint: str,
        *,
        token: str | None,
        timeout_seconds: float = 10.0,
        workspace: str = "default",
    ) -> ValidationClient:
        assert timeout_seconds == 10.0
        validated.append((endpoint, token, workspace))
        return ValidationClient()

    def authenticate_device(*_: object, **__: object) -> DeviceLoginResult:
        return DeviceLoginResult(token=fresh_token)

    monkeypatch.setattr("lazycloud.cli.identity.device_login", authenticate_device)
    monkeypatch.setattr(
        "lazycloud.cli.identity.WorkspaceControlClient.from_endpoint",
        validation_client,
    )

    result = CliRunner().invoke(build_public_cli(), ["--json", "login"])

    assert result.exit_code == 0, result.output
    assert stale_token not in result.output
    assert fresh_token not in result.output
    assert json.loads(result.output)["token_source"] == "device"
    assert validated == [("https://control.example", fresh_token, "team")]
    assert get_profile(apply_env=False).token == fresh_token


def test_login_rejects_an_invalid_token_without_replacing_the_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state"
    stored_token = "working-profile-secret"
    invalid_token = "invalid-candidate-secret"
    monkeypatch.setenv("LAZYCLOUD_HOME", str(state))
    reset_settings_cache()
    set_profile(
        ClientProfile(
            endpoint="https://control.example",
            workspace="team",
            token=stored_token,
        )
    )

    class ValidationClient:
        def current(self) -> object:
            raise HttpApiError("invalid token", status_code=401)

    def validation_client(
        endpoint: str,
        *,
        token: str | None,
        timeout_seconds: float = 10.0,
        workspace: str = "default",
    ) -> ValidationClient:
        assert (endpoint, token, timeout_seconds, workspace) == (
            "https://control.example",
            invalid_token,
            10.0,
            "team",
        )
        return ValidationClient()

    monkeypatch.setattr(
        "lazycloud.cli.identity.WorkspaceControlClient.from_endpoint",
        validation_client,
    )

    with pytest.raises(SystemExit) as raised:
        client_start(
            args=["--json", "login", "--token", invalid_token],
            prog_name="lazycloud",
        )

    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert stored_token not in captured.err
    assert invalid_token not in captured.err
    assert json.loads(captured.err) == {
        "error": {
            "hint": "Run `lazycloud login` to sign in again.",
            "message": "invalid token",
            "type": "authentication_failed",
        }
    }
    assert get_profile(apply_env=False).token == stored_token
