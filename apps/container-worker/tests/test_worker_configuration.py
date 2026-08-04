from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from container_worker_app.settings import WorkerSettings
from pydantic import ValidationError
from worker.configuration import (
    WORKER_CONFIG_PATH_ENV,
    WorkerCapacityConfiguration,
    WorkerConfiguration,
    WorkerExecutionConfiguration,
    WorkerPathConfiguration,
    serialize_worker_configuration,
)
from worker.runtime_config import OciRuntimeName


def test_worker_settings_precedence_is_init_then_env_then_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A launcher's environment beats the configuration file, and an argument beats both.

    The launcher sets per-worker identity in the environment while the file it
    writes carries the configuration, so an environment value that lost to the
    file would give the worker another worker's network.
    """
    config_path = tmp_path / "worker.yaml"
    document = yaml.safe_load(
        serialize_worker_configuration(
            WorkerConfiguration(
                execution=WorkerExecutionConfiguration(
                    capacity=WorkerCapacityConfiguration(cpu_millicores=2000),
                )
            )
        )
    )
    document["network_prefix"] = "yaml-prefix"
    config_path.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setenv(WORKER_CONFIG_PATH_ENV, str(config_path))

    from_yaml = WorkerSettings()
    assert from_yaml.network_prefix == "yaml-prefix"
    assert from_yaml.configuration.execution.capacity.cpu_millicores == 2000

    monkeypatch.setenv("WORKER_NETWORK_PREFIX", "env-prefix")
    from_env = WorkerSettings()
    assert from_env.network_prefix == "env-prefix"
    assert from_env.configuration.execution.capacity.cpu_millicores == 2000

    from_init = WorkerSettings(network_prefix="init-prefix")
    assert from_init.network_prefix == "init-prefix"
    assert from_init.configuration.execution.capacity.cpu_millicores == 2000


def test_worker_settings_reject_unknown_yaml_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "worker.yaml"
    config_path.write_text(
        "configuration:\n  execution:\n    unexpected: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(WORKER_CONFIG_PATH_ENV, str(config_path))

    with pytest.raises(ValidationError, match="unexpected"):
        WorkerSettings()


@pytest.mark.parametrize(
    "build_root",
    ["/dev/shm", "/dev/shm/builds", "/cache", "/cache/builds"],
)
def test_worker_configuration_rejects_non_dedicated_build_root(build_root: str) -> None:
    with pytest.raises(ValidationError, match="image build root"):
        WorkerPathConfiguration(
            image_build_root=Path(build_root),
            cache_root=Path("/cache"),
        )


def test_worker_execution_configuration_requires_unique_candidates_and_default() -> None:
    with pytest.raises(ValidationError, match="must be unique"):
        WorkerExecutionConfiguration(runtimes=[OciRuntimeName.Runc, OciRuntimeName.Runc])

    with pytest.raises(ValidationError, match="must be included"):
        WorkerExecutionConfiguration(
            runtime=OciRuntimeName.Runsc,
            runtimes=[OciRuntimeName.Runc],
        )
