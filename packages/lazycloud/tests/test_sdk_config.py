from __future__ import annotations

import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from lazycloud.cli.main import start as client_start
from lazycloud.config import (
    CONFIG_DIRECTORY_MODE,
    CONFIG_FILE_MODE,
    DEFAULT_PROFILE,
    PACKAGED_DEFAULT_ENDPOINT,
    ClientProfile,
    ConfigError,
    activate_profile,
    active_profile_name,
    delete_profile,
    get_profile,
    list_profiles,
    reset_settings_cache,
    set_profile,
    settings,
)


@pytest.fixture(autouse=True)
def reset_config_settings_cache() -> Iterator[None]:
    reset_settings_cache()
    yield
    reset_settings_cache()


def test_sdk_profile_lifecycle_uses_sdk_config_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("LAZYCLOUD_ENDPOINT", raising=False)
    # A customer in an empty directory has no endpoint set.
    monkeypatch.chdir(tmp_path)
    reset_settings_cache()

    assert get_profile().name == DEFAULT_PROFILE
    assert get_profile().endpoint == ""
    # With no endpoint configured anywhere, resolution falls back to the
    # packaged hosted default rather than raising.
    assert get_profile().resolved_endpoint() == PACKAGED_DEFAULT_ENDPOINT

    saved = set_profile(
        ClientProfile(
            name="company",
            endpoint="https://api.example",
            workspace="platform",
            token="token",
            tls=True,
        )
    )

    assert saved.name == "company"
    assert active_profile_name() == "company"
    assert get_profile().endpoint == "https://api.example"
    # A stored endpoint overrides the packaged default.
    assert get_profile().resolved_endpoint() == "https://api.example"
    assert get_profile().workspace == "platform"
    assert {item.name for item in list_profiles()} == {DEFAULT_PROFILE, "company"}

    activate_profile(DEFAULT_PROFILE)
    assert active_profile_name() == DEFAULT_PROFILE

    delete_profile("company")
    with pytest.raises(KeyError, match="profile not found"):
        get_profile("company")


def test_sdk_profile_environment_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LAZYCLOUD_HOME", str(tmp_path))
    monkeypatch.setenv("LAZYCLOUD_ENDPOINT", "https://env.example")
    monkeypatch.setenv("LAZYCLOUD_WORKSPACE", "env-workspace")
    monkeypatch.setenv("LAZYCLOUD_TOKEN", "env-token")
    monkeypatch.setenv("LAZYCLOUD_TLS", "true")
    reset_settings_cache()

    try:
        profile = get_profile()
    finally:
        reset_settings_cache()

    assert profile.endpoint == "https://env.example"
    assert profile.workspace == "env-workspace"
    assert profile.token == "env-token"
    assert profile.tls is True


def test_sdk_profile_file_stays_owner_only_across_creation_and_update(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LAZYCLOUD_HOME", str(tmp_path / "state"))
    reset_settings_cache()

    set_profile(ClientProfile(name="company", endpoint="https://api.example", token="stored"))
    path = settings().config_path

    assert stat.S_IMODE(path.parent.stat().st_mode) == CONFIG_DIRECTORY_MODE
    assert stat.S_IMODE(path.stat().st_mode) == CONFIG_FILE_MODE

    # A file left readable by other users is repaired by the next write rather
    # than carried forward.
    path.chmod(0o644)
    set_profile(ClientProfile(name="other", endpoint="https://other.example", token="stored"))

    assert stat.S_IMODE(path.stat().st_mode) == CONFIG_FILE_MODE


def test_sdk_profile_write_refuses_a_symlink_or_non_regular_config_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LAZYCLOUD_HOME", str(tmp_path / "state"))
    reset_settings_cache()

    set_profile(ClientProfile(name="company", endpoint="https://api.example", token="stored"))
    path = settings().config_path
    planted = tmp_path / "planted.yaml"
    planted.write_text("profiles: {}\n", encoding="utf-8")
    path.unlink()
    path.symlink_to(planted)

    with pytest.raises(ConfigError, match="symlink"):
        set_profile(ClientProfile(name="company", endpoint="https://api.example", token="rotated"))

    assert planted.read_text(encoding="utf-8") == "profiles: {}\n"

    path.unlink()
    path.mkdir()

    with pytest.raises(ConfigError, match="not a regular file"):
        set_profile(ClientProfile(name="company", endpoint="https://api.example", token="rotated"))


def test_sdk_broken_profile_file_never_reaches_cli_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=CONFIG_DIRECTORY_MODE)
    stored = "unbalanced-quote-profile-value"
    # An unterminated quote: PyYAML reports the offending source line verbatim,
    # which is the line holding the credential.
    (state / "config.yaml").write_text(
        f'active_profile: default\nprofiles:\n  default:\n    token: "{stored}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("LAZYCLOUD_HOME", str(state))
    reset_settings_cache()

    with pytest.raises(SystemExit) as raised:
        client_start(args=["profile", "list"], prog_name="lazycloud")

    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert stored not in captured.out
    assert stored not in captured.err
