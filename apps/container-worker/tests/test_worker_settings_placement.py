"""The agent passes the worker its placement as a plain key in the environment."""

import pytest
from container_worker_app.settings import WorkerSettings
from shared.placement import Placement


@pytest.mark.parametrize(
    "key",
    ["platform", "machine:3372a7f4-42f7-4141-9158-2153672d8948"],
)
def test_worker_settings_read_the_placement_key_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    monkeypatch.setenv("WORKER_PLACEMENT", key)
    assert WorkerSettings().placement == Placement.parse(key)
