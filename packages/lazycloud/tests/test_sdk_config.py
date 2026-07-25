from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from lazycloud.config import (
    DEFAULT_PROFILE,
    PACKAGED_DEFAULT_ENDPOINT,
    ClientProfile,
    activate_profile,
    active_profile_name,
    delete_profile,
    get_profile,
    list_profiles,
    reset_settings_cache,
    set_profile,
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
    # Isolate from any `.env` in the working tree so the unconfigured path is
    # deterministic; a customer in an empty directory has no endpoint set.
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
