from __future__ import annotations

import json
import stat
import time
from datetime import timedelta
from pathlib import Path

from foundation.process import ProcessResult
from shared.timestamps import utc_now
from worker.credential_payloads import WorkerCredentialPrincipal
from worker.durable_disk_records import (
    DiskAcquirePayload,
    DiskAcquireResult,
    DiskCollectPayload,
    DiskPublishPayload,
    DiskPublishResult,
    DiskReleasePayload,
)
from worker.durable_disks import (
    ContainerDiskLeases,
    DiskEngine,
    DiskLease,
    DiskStoreFile,
    WorkerDurableDiskService,
)
from worker.tools import (
    ContainerCredentialRequest,
    ContainerCredentials,
    WorkspaceStorageCredentials,
)


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


class _RecordingControlPlane:
    def __init__(self) -> None:
        self.published: list[DiskPublishPayload] = []
        self.released: list[DiskReleasePayload] = []

    def acquire_disk(self, payload: DiskAcquirePayload) -> DiskAcquireResult:
        raise AssertionError("a release does not acquire")

    def publish_disk(self, payload: DiskPublishPayload) -> DiskPublishResult:
        self.published.append(payload)
        return DiskPublishResult(generation=payload.generation)

    def release_disk(self, payload: DiskReleasePayload) -> None:
        self.released.append(payload)

    def collect_disk(self, payload: DiskCollectPayload) -> None:
        raise AssertionError("a layer building on a parent collects nothing")

    def vend(
        self, request: ContainerCredentialRequest, *, principal: WorkerCredentialPrincipal
    ) -> ContainerCredentials:
        return ContainerCredentials(
            workspace_storage=WorkspaceStorageCredentials(
                endpoint_url="http://store", bucket_name="b"
            )
        )


def test_a_release_resumes_from_the_generation_the_engine_committed(tmp_path: Path) -> None:
    """The engine commits a generation before the worker saves it. A worker that died
    between the two restarts with its lease file a generation behind; publishing on
    that would name a parent the engine has moved past, and the release would fail
    forever, pinning the disk."""
    commands: list[list[str]] = []

    def engine(timeout: float, argv: list[str]) -> ProcessResult:
        commands.append(argv[1:])
        stdout = {
            "recover": '{"recovered": ["disk-1"], "pending": {"disk-1": 1}}',
            "published": '{"generation": 4, "chain_depth": 4}',
        }.get(argv[1], "{}")
        if argv[1] == "publish":
            generation, parent = (
                int(argv[argv.index(flag) + 1]) for flag in ("--generation", "--parent")
            )
            stdout = json.dumps(
                {
                    "manifest_key": f"disks/disk-1/manifests/{generation}.json",
                    "manifest_sha256": "a" * 64,
                    "stored_bytes_added": 10,
                    "generation": generation,
                    "parent_generation": parent,
                }
            )
        return ProcessResult(args=argv, exit_code=0, stdout=stdout, stderr="")

    control_plane = _RecordingControlPlane()
    service = WorkerDurableDiskService(
        engine=DiskEngine(run_root=tmp_path / "run", run_command=engine),
        leases=control_plane,
        credentials=control_plane,
        layers_root=tmp_path / "layers",
        lease_root=tmp_path / "leases",
        mount_root=tmp_path / "mounts",
    )
    service.lease_root.mkdir()
    (service.lease_root / "container-1.json").write_text(
        ContainerDiskLeases(
            container_id="container-1",
            workspace_id="workspace-1",
            stub_id="stub-1",
            disks=[
                DiskLease(
                    disk_id="disk-1",
                    name="data",
                    mount_path="/data",
                    size_bytes=1,
                    lease_token="token",
                    generation=3,
                    chain_depth=3,
                    mountpoint="/mounts/container-1/disk-1",
                )
            ],
        ).model_dump_json()
    )

    service.recover()
    service.release("container-1")

    assert [(item.generation, item.parent_generation) for item in control_plane.published] == [
        (5, 4)
    ]
    assert [
        "commit-published",
        "--root",
        str(service.layers_root),
        "--disk",
        "disk-1",
        "--generation",
        "5",
    ] in commands
    assert [item.disk_id for item in control_plane.released] == ["disk-1"]


def test_an_engine_call_that_outlives_its_grant_reads_a_renewed_one(tmp_path: Path) -> None:
    """A workspace storage grant can run out long before a restore or flatten does.
    The engine re-reads its credentials file near expiry, so the worker must have
    replaced it by then, or the call fails partway with an expired token."""
    grants: list[DiskStoreFile] = []

    def vend() -> DiskStoreFile:
        grants.append(
            DiskStoreFile(
                endpoint_url="http://store",
                region="region",
                bucket="b",
                access_key=f"key-{len(grants)}",
                secret_key="secret",
                session_token="token",
                force_path_style=True,
                expires_at=utc_now() + timedelta(seconds=2),
            )
        )
        return grants[-1]

    seen: list[tuple[str, int, bool]] = []

    def engine(timeout: float, argv: list[str]) -> ProcessResult:
        path = Path(argv[argv.index("--store") + 1])
        deadline = time.monotonic() + 10
        store = DiskStoreFile.model_validate_json(path.read_text(encoding="utf-8"))
        while store.access_key == "key-0" and time.monotonic() < deadline:
            time.sleep(0.05)
            store = DiskStoreFile.model_validate_json(path.read_text(encoding="utf-8"))
        before_expiry = grants[0].expires_at is not None and utc_now() < grants[0].expires_at
        seen.append((store.access_key, stat.S_IMODE(path.stat().st_mode), before_expiry))
        stdout = '{"removed_bytes": 0, "removed_chunks": 0, "removed_manifests": 0}'
        return ProcessResult(args=argv, exit_code=0, stdout=stdout, stderr="")

    DiskEngine(run_root=tmp_path / "run", run_command=engine).collect(
        tmp_path / "layers", "disk-1", generation=1, store=vend
    )

    assert seen == [("key-1", 0o600, True)]
    assert list((tmp_path / "run" / "disk-1").iterdir()) == []
