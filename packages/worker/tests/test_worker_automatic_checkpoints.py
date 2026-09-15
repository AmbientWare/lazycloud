from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from shared.checkpoints import AutomaticCheckpointCreationLease
from shared.container_requests import WorkerStartupKind
from worker.automatic_checkpoints import WorkerAutomaticCheckpointService
from worker.container_execution import ContainerExecutionContext, ContainerMountSetupResult
from worker.container_service.models import WorkerContainerServiceInstance
from worker.container_service.state import LocalWorkerContainerInstanceStore
from worker.events import ContainerRequestContext


@dataclass(slots=True)
class CheckpointCreator:
    calls: list[str] = field(default_factory=list)

    def create_checkpoint(
        self,
        instance: WorkerContainerServiceInstance,
        *,
        checkpoint_id: str = "",
    ) -> str:
        self.calls.append(instance.container_id)
        return checkpoint_id or "checkpoint-1"


@dataclass(slots=True)
class CheckpointLeases:
    acquired: bool = True
    available_checkpoint_id: str = ""
    acquired_by: list[str] = field(default_factory=list)
    released_by: list[str] = field(default_factory=list)

    def acquire(
        self,
        *,
        workspace_id: str,
        stub_id: str,
        owner_token: str,
        ttl_seconds: int,
    ) -> AutomaticCheckpointCreationLease:
        assert workspace_id == "workspace-1"
        assert stub_id == "stub-1"
        assert ttl_seconds > 600
        self.acquired_by.append(owner_token)
        return AutomaticCheckpointCreationLease(
            acquired=self.acquired,
            available_checkpoint_id=self.available_checkpoint_id,
        )

    def release(
        self,
        *,
        workspace_id: str,
        stub_id: str,
        owner_token: str,
    ) -> bool:
        assert workspace_id == "workspace-1"
        assert stub_id == "stub-1"
        self.released_by.append(owner_token)
        return True


@dataclass(slots=True)
class RuntimeState:
    value: str = "running"
    calls: list[str] = field(default_factory=list)

    def status(self, container_id: str) -> str:
        self.calls.append(container_id)
        return self.value


def test_automatic_checkpoint_mounts_signal_and_publishes_after_runner_ready(
    tmp_path: Path,
) -> None:
    store = LocalWorkerContainerInstanceStore()
    store.save_container_instance(
        WorkerContainerServiceInstance(
            container_id="container-1",
            root_path="/rootfs",
            workspace_id="workspace-1",
        )
    )
    creator = CheckpointCreator()
    leases = CheckpointLeases()
    service = WorkerAutomaticCheckpointService(
        instances=store,
        creator=creator,
        leases=leases,
        runtime=RuntimeState(),
        signal_root=str(tmp_path),
    )
    context = ContainerExecutionContext(
        request=ContainerRequestContext(
            container_id="container-1",
            workspace_id="workspace-1",
            stub_id="stub-1",
        ),
        startup_kind=WorkerStartupKind.Endpoint,
        checkpoint_enabled=True,
        checkpoint_readiness_timeout_seconds=1,
        checkpoint_readiness_interval_seconds=0.001,
    )

    mounts = service.prepare_mount(context, ContainerMountSetupResult())
    signal_dir = tmp_path / "container-1" / "criu"
    (signal_dir / "READY_FOR_CHECKPOINT").touch()

    checkpoint_id = service.checkpoint_or_complete_restore(
        context,
        container_hostname="worker.example:43123",
    )

    assert checkpoint_id == "checkpoint-1"
    assert creator.calls == ["container-1"]
    assert leases.acquired_by == ["container-1"]
    assert leases.released_by == ["container-1"]
    assert mounts.oci_mounts[0].source == str(signal_dir)
    assert mounts.oci_mounts[0].destination == "/criu"
    assert (signal_dir / "CHECKPOINT_COMPLETE").is_file()
    assert (signal_dir / "CONTAINER_HOSTNAME").read_text() == "worker.example:43123"


def test_checkpoint_restore_completes_runner_identity_handshake(tmp_path: Path) -> None:
    store = LocalWorkerContainerInstanceStore()
    service = WorkerAutomaticCheckpointService(
        instances=store,
        creator=CheckpointCreator(),
        leases=CheckpointLeases(),
        runtime=RuntimeState(),
        signal_root=str(tmp_path),
    )
    context = ContainerExecutionContext(
        request=ContainerRequestContext(container_id="restored-container"),
        startup_kind=WorkerStartupKind.Endpoint,
        checkpoint_id="checkpoint-1",
    )

    service.prepare_mount(context, ContainerMountSetupResult())
    checkpoint_id = service.checkpoint_or_complete_restore(
        context,
        container_hostname="worker.example:45123",
    )

    signal_dir = tmp_path / "restored-container" / "criu"
    assert checkpoint_id == "checkpoint-1"
    assert (signal_dir / "CONTAINER_ID").read_text() == "restored-container"
    assert (signal_dir / "CONTAINER_HOSTNAME").read_text() == "worker.example:45123"
    assert (signal_dir / "CHECKPOINT_COMPLETE").is_file()


def test_automatic_checkpoint_lease_denial_releases_runner_without_duplicate_creation(
    tmp_path: Path,
) -> None:
    store = LocalWorkerContainerInstanceStore()
    store.save_container_instance(
        WorkerContainerServiceInstance(
            container_id="container-2",
            root_path="/rootfs",
            workspace_id="workspace-1",
        )
    )
    creator = CheckpointCreator()
    leases = CheckpointLeases(
        acquired=False,
        available_checkpoint_id="checkpoint-existing",
    )
    service = WorkerAutomaticCheckpointService(
        instances=store,
        creator=creator,
        leases=leases,
        runtime=RuntimeState(),
        signal_root=str(tmp_path),
    )
    context = ContainerExecutionContext(
        request=ContainerRequestContext(
            container_id="container-2",
            workspace_id="workspace-1",
            stub_id="stub-1",
        ),
        startup_kind=WorkerStartupKind.Endpoint,
        checkpoint_enabled=True,
    )

    service.prepare_mount(context, ContainerMountSetupResult())
    checkpoint_id = service.checkpoint_or_complete_restore(
        context,
        container_hostname="worker.example:43123",
    )

    assert checkpoint_id == "checkpoint-existing"
    assert creator.calls == []
    assert leases.acquired_by == ["container-2"]
    assert leases.released_by == []
    assert (tmp_path / "container-2/criu/CHECKPOINT_COMPLETE").is_file()


def test_automatic_checkpoint_stops_waiting_when_runtime_exits(tmp_path: Path) -> None:
    store = LocalWorkerContainerInstanceStore()
    store.save_container_instance(
        WorkerContainerServiceInstance(
            container_id="container-3",
            root_path="/rootfs",
            workspace_id="workspace-1",
        )
    )
    creator = CheckpointCreator()
    leases = CheckpointLeases()
    runtime = RuntimeState(value="unknown")
    service = WorkerAutomaticCheckpointService(
        instances=store,
        creator=creator,
        leases=leases,
        runtime=runtime,
        signal_root=str(tmp_path),
    )
    context = ContainerExecutionContext(
        request=ContainerRequestContext(
            container_id="container-3",
            workspace_id="workspace-1",
            stub_id="stub-1",
        ),
        startup_kind=WorkerStartupKind.Endpoint,
        checkpoint_enabled=True,
        checkpoint_readiness_timeout_seconds=600,
        checkpoint_readiness_interval_seconds=1,
    )

    service.prepare_mount(context, ContainerMountSetupResult())

    with pytest.raises(
        RuntimeError,
        match=r"container runtime exited before checkpoint readiness: container-3 \(unknown\)",
    ):
        service.checkpoint_or_complete_restore(
            context,
            container_hostname="worker.example:43123",
        )

    assert runtime.calls == ["container-3"]
    assert creator.calls == []
    assert leases.acquired_by == ["container-3"]
    assert leases.released_by == ["container-3"]
