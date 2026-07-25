from __future__ import annotations

from pathlib import Path

import pytest
from shared.container_requests import WorkerStartupKind
from shared.image_building.authoring import LinuxArchitecture
from tests.managed_runtime_fakes import managed_runtime_catalog_root
from worker.managed_runtime import (
    MANAGED_RUNTIME_CONTAINER_ROOT,
    MANAGED_RUNTIME_LAUNCHER_PATH,
    plan_managed_runtime,
)
from worker.managed_runtime_artifacts import load_managed_runtime_catalog


@pytest.mark.parametrize(
    ("startup_kind", "module"),
    [
        (WorkerStartupKind.Function, "runner.function"),
        (WorkerStartupKind.Endpoint, "runner.serve"),
        (WorkerStartupKind.Asgi, "runner.serve"),
        (WorkerStartupKind.TaskQueue, "runner.taskqueue"),
    ],
)
def test_managed_runtime_wraps_target_interpreter_and_preserves_user_paths(
    tmp_path: Path,
    startup_kind: WorkerStartupKind,
    module: str,
) -> None:
    root, manifest = managed_runtime_catalog_root(tmp_path)
    catalog = load_managed_runtime_catalog(
        root,
        "3.10",
        LinuxArchitecture.Amd64,
    )

    plan = plan_managed_runtime(
        startup_kind,
        catalog=catalog,
        architecture=LinuxArchitecture.Amd64,
        command=["python3.10", "-m", module, "--once"],
        user_code_path="/workspace/app",
    )

    assert plan.enabled
    assert plan.digest == manifest.digest
    assert plan.command == ["python3.10", MANAGED_RUNTIME_LAUNCHER_PATH, module, "--once"]
    assert plan.python_path("/image/site-packages:/workspace/app") == (
        "/workspace/app:/image/site-packages"
    )
    assert len(plan.mounts) == 1
    assert plan.mounts[0].source == str(root)
    assert plan.mounts[0].destination == MANAGED_RUNTIME_CONTAINER_ROOT
    assert plan.mounts[0].options == [
        "ro",
        "rbind",
        "rprivate",
        "nosuid",
        "nodev",
    ]
