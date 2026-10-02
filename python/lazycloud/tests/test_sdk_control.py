from __future__ import annotations

from pathlib import Path

import pytest
from lazycloud.config import (
    PACKAGED_DEFAULT_ENDPOINT,
    ClientProfile,
    reset_settings_cache,
    set_profile,
)
from lazycloud.control import control_workspace_scope, resolve_control_client_config


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("profile", ("https://api.example", "profile-token", "profile-workspace")),
        ("explicit", ("https://api.example", "explicit-token", "explicit-workspace")),
        ("scope", ("https://api.example", "profile-token", "scoped-workspace")),
        ("packaged", (PACKAGED_DEFAULT_ENDPOINT, None, "")),
        ("endpoint-env", ("https://env.example", None, "")),
        ("bare-host", ("http://127.0.0.1:8080", None, "")),
    ],
)
def test_resolve_control_client_config_precedence(
    source: str,
    expected: tuple[str, str | None, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LAZYCLOUD_HOME", str(tmp_path))
    monkeypatch.delenv("LAZYCLOUD_ENDPOINT", raising=False)
    if source in {"profile", "scope"}:
        set_profile(
            ClientProfile(
                name="company",
                endpoint="https://api.example",
                token="profile-token",
                workspace="profile-workspace",
            )
        )
    elif source == "endpoint-env":
        monkeypatch.setenv("LAZYCLOUD_ENDPOINT", "https://env.example")
    elif source == "bare-host":
        monkeypatch.setenv("LAZYCLOUD_ENDPOINT", "127.0.0.1:8080")
    reset_settings_cache()

    if source == "explicit":
        config = resolve_control_client_config(
            endpoint="https://api.example",
            token="explicit-token",
            workspace="explicit-workspace",
        )
    elif source == "scope":
        with control_workspace_scope("scoped-workspace"):
            config = resolve_control_client_config()
    else:
        config = resolve_control_client_config()

    assert (config.endpoint, config.token, config.workspace) == expected
