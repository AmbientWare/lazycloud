from __future__ import annotations

from pathlib import Path

import pytest
from container_worker_app.production import ProductionWorkerSettings
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
    config_path = tmp_path / "worker.yaml"
    config_path.write_text(
        serialize_worker_configuration(
            WorkerConfiguration(
                execution=WorkerExecutionConfiguration(
                    capacity=WorkerCapacityConfiguration(cpu_millicores=2000),
                )
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(WORKER_CONFIG_PATH_ENV, str(config_path))

    from_yaml = ProductionWorkerSettings()
    assert from_yaml.resolved_cpu_millicores == 2000

    monkeypatch.setenv("WORKER_CPU_MILLICORES", "3000")
    from_env = ProductionWorkerSettings()
    assert from_env.resolved_cpu_millicores == 3000

    from_init = ProductionWorkerSettings(cpu_millicores=4000)
    assert from_init.resolved_cpu_millicores == 4000


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
        ProductionWorkerSettings()


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
