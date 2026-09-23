from __future__ import annotations

from pathlib import Path

from foundation.process import ProcessResult
from worker.credential_payloads import WorkerCredentialPrincipal
from worker.durable_disk_records import (
    DiskAcquirePayload,
    DiskAcquireResult,
    DiskCollectPayload,
    DiskPublishPayload,
    DiskPublishResult,
    DiskReleasePayload,
)
from worker.durable_disks import DiskEngine, WorkerDurableDiskService
from worker.tools import ContainerCredentialRequest, ContainerCredentials


class _NoControlPlane:
    def acquire_disk(self, payload: DiskAcquirePayload) -> DiskAcquireResult:
        raise AssertionError("recovery does not acquire")

    def publish_disk(self, payload: DiskPublishPayload) -> DiskPublishResult:
        raise AssertionError("recovery does not publish")

    def release_disk(self, payload: DiskReleasePayload) -> None:
        raise AssertionError("recovery does not release")

    def collect_disk(self, payload: DiskCollectPayload) -> None:
        raise AssertionError("recovery does not collect")

    def vend(
        self, request: ContainerCredentialRequest, *, principal: WorkerCredentialPrincipal
    ) -> ContainerCredentials:
        raise AssertionError("recovery vends no credentials")


def test_a_disk_that_cannot_be_recovered_does_not_stop_the_worker_starting(
    tmp_path: Path,
) -> None:
    """Recovery runs at every worker start. Raising from it would fail every start,
    and no lease on the machine would ever be given back."""

    def engine_fails(timeout: float, argv: list[str]) -> ProcessResult:
        return ProcessResult(args=argv, exit_code=1, stdout="", stderr="disk is unreadable")

    control_plane = _NoControlPlane()
    service = WorkerDurableDiskService(
        engine=DiskEngine(run_root=tmp_path / "run", run_command=engine_fails),
        leases=control_plane,
        credentials=control_plane,
        layers_root=tmp_path / "layers",
        lease_root=tmp_path / "leases",
        mount_root=tmp_path / "mounts",
    )

    service.recover()
