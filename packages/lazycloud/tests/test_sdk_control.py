from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from lazycloud.config import (
    PACKAGED_DEFAULT_ENDPOINT,
    ClientProfile,
    reset_settings_cache,
    set_profile,
)
from lazycloud.control import resolve_control_client_config
from shared.env import (
    GATEWAY_HTTP_URL_ENV,
    GATEWAY_TOKEN_ENV,
    WORKSPACE_ID_ENV,
    WORKSPACE_NAME_ENV,
)


@pytest.fixture(autouse=True)
def reset_config_settings_cache() -> Iterator[None]:
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("worker", ("http://control-plane:9000", "rt_worker", "workspace-id")),
        ("profile", ("https://api.example", "profile-token", "profile-workspace")),
        ("explicit", ("https://api.example", "explicit-token", "explicit-workspace")),
        ("packaged", (PACKAGED_DEFAULT_ENDPOINT, None, "default")),
        ("endpoint-env", ("https://env.example", None, "default")),
    ],
)
def test_resolve_control_client_config_precedence(
    source: str,
    expected: tuple[str, str | None, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LAZYCLOUD_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    for variable in (
        "LAZYCLOUD_ENDPOINT",
        GATEWAY_HTTP_URL_ENV,
        GATEWAY_TOKEN_ENV,
        WORKSPACE_ID_ENV,
        WORKSPACE_NAME_ENV,
    ):
        monkeypatch.delenv(variable, raising=False)
    if source in {"worker", "explicit"}:
        monkeypatch.setenv(GATEWAY_HTTP_URL_ENV, "http://control-plane:9000")
        monkeypatch.setenv(GATEWAY_TOKEN_ENV, "rt_worker")
        monkeypatch.setenv(WORKSPACE_ID_ENV, "workspace-id")
        monkeypatch.setenv(WORKSPACE_NAME_ENV, "workspace-name")
    elif source == "profile":
        set_profile(
            ClientProfile(
                name="company",
                endpoint="https://api.example",
                token="profile-token",
                workspace="profile-workspace",
                tls=True,
            )
        )
    elif source == "endpoint-env":
        monkeypatch.setenv("LAZYCLOUD_ENDPOINT", "https://env.example")
    reset_settings_cache()

    config = (
        resolve_control_client_config(
            endpoint="https://api.example",
            token="explicit-token",
            workspace="explicit-workspace",
        )
        if source == "explicit"
        else resolve_control_client_config()
    )

    assert (config.endpoint, config.token, config.workspace) == expected
