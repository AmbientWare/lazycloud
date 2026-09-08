from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Never

import pytest
from worker.adapters import (
    WorkerFinalizationCleanup,
    WorkerRuntimeContainerStopper,
)
from worker.container_service.models import WorkerContainerServiceInstance
from worker.container_service.state import LocalWorkerContainerInstanceStore
from worker.events import ContainerRequestContext
from worker.runtime_config import RuntimeContainerStatus


def test_worker_finalization_rejects_a_bundle_path_owned_by_another_container(
    tmp_path: Path,
) -> None:
    store = LocalWorkerContainerInstanceStore()
    foreign_bundle = tmp_path / "bundles" / "ctr-2"
    foreign_bundle.mkdir(parents=True)
    store.save_container_instance(
        WorkerContainerServiceInstance(
            container_id="ctr-1",
            root_path=str(foreign_bundle / "rootfs"),
            bundle_path=str(foreign_bundle),
        )
    )

    with pytest.raises(RuntimeError, match="bundle path does not belong"):
        WorkerFinalizationCleanup(instances=store).delete_local_state("ctr-1")

    assert foreign_bundle.exists()
    assert store.get_container_instance("ctr-1") is not None


def test_worker_controller_finalization_cleanup_force_stops_live_runtime_states() -> None:
    cleanup = WorkerFinalizationCleanup(runtime=_RuntimeController())

    for status in (
        RuntimeContainerStatus.Creating,
        RuntimeContainerStatus.Created,
        RuntimeContainerStatus.Running,
        RuntimeContainerStatus.Paused,
    ):
        cleanup.runtime = _RuntimeController(status_value=status.value)
        cleanup.force_stop_if_running(f"ctr-{status.value}")
        assert cleanup.runtime.kill_calls == [(f"ctr-{status.value}", 9, True)]

    cleanup.runtime = _RuntimeController(status_value=RuntimeContainerStatus.Stopped.value)
    cleanup.force_stop_if_running("ctr-stopped")
    assert cleanup.runtime.kill_calls == []


def test_worker_runtime_container_stopper_escalates_ignored_graceful_signal() -> None:
    runtime = _RuntimeController(status_value=RuntimeContainerStatus.Running.value)
    stopper = WorkerRuntimeContainerStopper(
        runtime,
        graceful_timeout_seconds=0,
        poll_interval_seconds=0,
    )

    stopper.stop_container("ctr-1", force=False)

    assert runtime.kill_calls == [
        ("ctr-1", 15, False),
        ("ctr-1", 9, True),
    ]


def test_worker_runtime_container_stopper_does_not_force_exited_container() -> None:
    runtime = _RuntimeController(status_value=RuntimeContainerStatus.Stopped.value)
    stopper = WorkerRuntimeContainerStopper(
        runtime,
        graceful_timeout_seconds=0,
        poll_interval_seconds=0,
    )

    stopper.stop_container("ctr-1", force=False)

    assert runtime.kill_calls == [("ctr-1", 15, False)]


def test_worker_runtime_container_stopper_treats_missing_assignment_as_already_stopped() -> None:
    """A container this worker holds no assignment for is already not running here.

    Nothing is killed — that is the safety property, and blindly killing an id
    this worker cannot vouch for is what it prevents. But it is reported as done
    rather than refused: the control plane waits on this acknowledgement to
    confirm a shutdown, and a delete that got exactly what it asked for would
    otherwise time out reporting that no worker ever confirmed it.
    """

    runtime = _RuntimeController(status_value=RuntimeContainerStatus.Running.value)
    stopper = WorkerRuntimeContainerStopper(runtime, instances=_DeleteStore())

    stopper.stop_container("ctr-missing", force=True)

    assert runtime.kill_calls == []


def test_worker_runtime_container_stopper_rejects_foreign_assignment() -> None:
    runtime = _RuntimeController(status_value=RuntimeContainerStatus.Running.value)
    store = LocalWorkerContainerInstanceStore()
    store.save_container_instance(
        WorkerContainerServiceInstance(
            container_id="ctr-1",
            root_path="/tmp/rootfs",
            worker_id="worker-2",
        )
    )
    stopper = WorkerRuntimeContainerStopper(
        runtime,
        instances=store,
        worker_id="worker-1",
    )

    with pytest.raises(RuntimeError, match="assigned to worker 'worker-2'"):
        stopper.stop_container("ctr-1", force=True)

    assert runtime.kill_calls == []


def _request() -> ContainerRequestContext:
    return ContainerRequestContext(
        container_id="ctr-1",
        workspace_id="workspace-1",
        stub_id="stub-1",
        app_id="app-1",
    )


@dataclass(slots=True)
class _RuntimeController:
    status_value: str = "created"
    kill_calls: list[tuple[str, int, bool]] = field(default_factory=list)

    def status(self, container_id: str) -> str:
        _ = container_id
        return self.status_value

    def exec_container(
        self,
        container_id: str,
        *,
        argv: list[str],
        env: list[str],
        cwd: str,
    ) -> Never:
        _ = container_id, argv, env, cwd
        raise NotImplementedError

    def kill_container(self, container_id: str, *, signal: int, force_delete: bool) -> None:
        self.kill_calls.append((container_id, signal, force_delete))
        if force_delete:
            self.status_value = RuntimeContainerStatus.Stopped.value


@dataclass(slots=True)
class _DeleteStore:
    deleted: list[str] = field(default_factory=list)

    def get_container_instance(self, container_id: str) -> WorkerContainerServiceInstance | None:
        _ = container_id
        return None

    def save_container_instance(self, instance: WorkerContainerServiceInstance) -> None:
        _ = instance

    def list_container_instances(self) -> list[WorkerContainerServiceInstance]:
        return []

    def delete_container_instance(self, container_id: str) -> bool:
        self.deleted.append(container_id)
        return True
