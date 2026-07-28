from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from foundation.process import ProcessOutputSink
from pydantic import JsonValue, TypeAdapter
from shared.checkpoints import CheckpointRecord, CheckpointStatus
from shared.container_requests import WorkerStartupKind
from worker.checkpoint_activity import CheckpointLeaseRegistry
from worker.checkpoint_restore import RuntimeCheckpointRestorer
from worker.checkpoints import CheckpointStatePayload, WorkerCheckpointStatus
from worker.container_execution import (
    ContainerExecutionContext,
    ContainerRuntimeRunResult,
)
from worker.events import ContainerRequestContext
from worker.oci_spec import OciRuntimeContainerSpec
from worker.runtime_config import OciRuntimeName, RuntimeBinaryConfig

type JsonObject = dict[str, JsonValue]

_JSON_OBJECT: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


def test_runtime_checkpoint_restorer_reuses_owned_rootfs_after_source_image_eviction(
    tmp_path: Path,
) -> None:
    checkpoint, archive = _checkpoint_archive(tmp_path)
    checkpoint_activity = CheckpointLeaseRegistry()
    source = _Source(
        checkpoint=checkpoint,
        archive=archive,
        checkpoint_activity=checkpoint_activity,
    )
    states = _StateSink()
    runtime = _Runtime(checkpoint_activity=checkpoint_activity)
    restorer = RuntimeCheckpointRestorer(
        source=source,
        state_sink=states,
        runtime=runtime,
        checkpoint_root=str(tmp_path / "checkpoints"),
        checkpoint_activity=checkpoint_activity,
    )
    evicted_root = tmp_path / "evicted-image-mount"
    started: list[int] = []

    for bundle_name in ("first-bundle", "second-bundle"):
        bundle = tmp_path / bundle_name
        bundle.mkdir()
        fresh_rootfs = bundle / "rootfs"
        fresh_rootfs.mkdir()
        (fresh_rootfs / "fresh.txt").write_text("fresh-image", encoding="utf-8")
        config_path = bundle / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "root": {"path": str(evicted_root), "readonly": False},
                    "linux": {"namespaces": [{"type": "cgroup"}]},
                }
            ),
            encoding="utf-8",
        )

        result = restorer.restore(
            _context(checkpoint.checkpoint_id),
            _spec(bundle, config_path),
            on_started=started.append,
        )

        assert result is not None
        assert result.exit_code == 0
        assert (
            bundle / "checkpoint-rootfs" / "workspace" / "state.txt"
        ).read_text() == "warm-state"
        assert (fresh_rootfs / "fresh.txt").read_text() == "fresh-image"
        restored_config = _JSON_OBJECT.validate_json(config_path.read_text(encoding="utf-8"))
        assert restored_config["root"] == {
            "path": str(bundle / "checkpoint-rootfs"),
            "readonly": False,
        }
        assert restored_config["linux"] == {"namespaces": [{"type": "cgroup"}]}
        assert not evicted_root.exists()

    assert started == [4321, 4321]
    assert source.lookups == [
        (checkpoint.checkpoint_id, "workspace-1"),
        (checkpoint.checkpoint_id, "workspace-1"),
    ]
    assert runtime.image_paths == [
        str(tmp_path / "checkpoints" / checkpoint.checkpoint_id),
        str(tmp_path / "checkpoints" / checkpoint.checkpoint_id),
    ]
    assert [payload.status for payload in states.payloads] == [
        WorkerCheckpointStatus.Available,
        WorkerCheckpointStatus.Available,
    ]
    assert states.payloads[-1].update_last_restored_at
    assert source.protected_during_download == {checkpoint.checkpoint_id}
    assert runtime.protected_during_restore == {checkpoint.checkpoint_id}
    assert checkpoint_activity.protected_checkpoint_ids() == set()


def test_runtime_checkpoint_restorer_rejects_corrupt_archive_and_marks_failure(
    tmp_path: Path,
) -> None:
    checkpoint, archive = _checkpoint_archive(tmp_path)
    archive.write_bytes(b"corrupt")
    source = _Source(checkpoint=checkpoint, archive=archive)
    states = _StateSink()
    restorer = RuntimeCheckpointRestorer(
        source=source,
        state_sink=states,
        runtime=_Runtime(),
        checkpoint_root=str(tmp_path / "checkpoints"),
    )
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    config_path = bundle / "config.json"
    config_path.write_text(
        json.dumps({"root": {"path": "/evicted/image/root"}}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match=r"hash|size"):
        restorer.restore(
            _context(checkpoint.checkpoint_id),
            _spec(bundle, config_path),
            on_started=lambda _pid: None,
        )

    assert states.payloads[-1].status is WorkerCheckpointStatus.RestoreFailed


def test_runtime_checkpoint_restorer_keeps_checkpoint_available_after_started_exit(
    tmp_path: Path,
) -> None:
    checkpoint, archive = _checkpoint_archive(tmp_path)
    states = _StateSink()
    restorer = RuntimeCheckpointRestorer(
        source=_Source(checkpoint=checkpoint, archive=archive),
        state_sink=states,
        runtime=_Runtime(error_after_started="restored container exited"),
        checkpoint_root=str(tmp_path / "checkpoints"),
    )
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    config_path = bundle / "config.json"
    config_path.write_text(
        json.dumps({"root": {"path": "/evicted/image/root"}}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="restored container exited"):
        restorer.restore(
            _context(checkpoint.checkpoint_id),
            _spec(bundle, config_path),
            on_started=lambda _pid: None,
        )

    assert [payload.status for payload in states.payloads] == [
        WorkerCheckpointStatus.Available,
    ]
    assert states.payloads[-1].update_last_restored_at


def test_runtime_checkpoint_restorer_preserves_fresh_rootfs_for_deployment_fallback(
    tmp_path: Path,
) -> None:
    checkpoint, archive = _checkpoint_archive(tmp_path)
    states = _StateSink()
    restorer = RuntimeCheckpointRestorer(
        source=_Source(checkpoint=checkpoint, archive=archive),
        state_sink=states,
        runtime=_Runtime(error_before_started="restore rejected"),
        checkpoint_root=str(tmp_path / "checkpoints"),
    )
    bundle = tmp_path / "bundle"
    fresh_rootfs = bundle / "rootfs"
    fresh_rootfs.mkdir(parents=True)
    (fresh_rootfs / "fresh.txt").write_text("fresh-image", encoding="utf-8")
    config_path = bundle / "config.json"
    original_config = json.dumps(
        {"root": {"path": str(fresh_rootfs), "readonly": False}},
        indent=2,
    )
    config_path.write_text(original_config, encoding="utf-8")

    result = restorer.restore(
        _context(checkpoint.checkpoint_id, startup_kind=WorkerStartupKind.Endpoint),
        _spec(bundle, config_path),
        on_started=lambda _pid: None,
    )

    assert result is None
    assert states.payloads[-1].status is WorkerCheckpointStatus.RestoreFailed
    assert config_path.read_text(encoding="utf-8") == original_config
    assert (fresh_rootfs / "fresh.txt").read_text(encoding="utf-8") == "fresh-image"
    assert not (bundle / "checkpoint-rootfs").exists()


def test_concurrent_restores_materialize_once_then_run_independently(
    tmp_path: Path,
) -> None:
    checkpoint, archive = _checkpoint_archive(tmp_path)
    activity = CheckpointLeaseRegistry()
    source = _ConcurrentSource(checkpoint=checkpoint, archive=archive)
    states = _StateSink()
    runtime = _ConcurrentRuntime(expected_starts=2)
    restorer = RuntimeCheckpointRestorer(
        source=source,
        state_sink=states,
        runtime=runtime,
        checkpoint_root=str(tmp_path / "checkpoints"),
        checkpoint_activity=activity,
    )
    outcomes: list[tuple[str, ContainerRuntimeRunResult | None]] = []
    errors: list[tuple[str, Exception]] = []

    first = threading.Thread(
        target=_restore_in_thread,
        args=(restorer, tmp_path, "container-a", outcomes, errors),
    )
    second = threading.Thread(
        target=_restore_in_thread,
        args=(restorer, tmp_path, "container-b", outcomes, errors),
    )
    first.start()
    assert source.download_started.wait(timeout=1)
    second.start()
    assert source.two_lookups.wait(timeout=1)
    assert activity.protected_checkpoint_ids() == {checkpoint.checkpoint_id}
    source.release_download.set()

    first.join(timeout=3)
    second.join(timeout=3)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert {container_id for container_id, result in outcomes if result is not None} == {
        "container-a",
        "container-b",
    }
    assert source.download_calls == 1
    assert set(runtime.started_containers) == {"container-a", "container-b"}
    assert runtime.concurrent_start_proven
    assert [payload.status for payload in states.payloads] == [
        WorkerCheckpointStatus.Available,
        WorkerCheckpointStatus.Available,
    ]
    for container_id in ("container-a", "container-b"):
        restored_state = tmp_path / container_id / "checkpoint-rootfs" / "workspace" / "state.txt"
        assert restored_state.read_text(encoding="utf-8") == "warm-state"
    materialized = tmp_path / "checkpoints" / checkpoint.checkpoint_id
    assert (materialized / "filesystem").is_dir()
    assert (materialized / "inventory.img").read_bytes() == b"runtime-state"
    assert not (tmp_path / "checkpoints" / f".{checkpoint.checkpoint_id}.extract").exists()
    assert activity.protected_checkpoint_ids() == set()


def test_failed_materialization_releases_owner_for_waiting_restore_retry(
    tmp_path: Path,
) -> None:
    checkpoint, archive = _checkpoint_archive(tmp_path)
    activity = CheckpointLeaseRegistry()
    source = _ConcurrentSource(
        checkpoint=checkpoint,
        archive=archive,
        fail_first_download=True,
    )
    states = _StateSink()
    runtime = _Runtime()
    restorer = RuntimeCheckpointRestorer(
        source=source,
        state_sink=states,
        runtime=runtime,
        checkpoint_root=str(tmp_path / "checkpoints"),
        checkpoint_activity=activity,
    )
    outcomes: list[tuple[str, ContainerRuntimeRunResult | None]] = []
    errors: list[tuple[str, Exception]] = []
    first = threading.Thread(
        target=_restore_in_thread,
        args=(restorer, tmp_path, "container-failed", outcomes, errors),
    )
    second = threading.Thread(
        target=_restore_in_thread,
        args=(restorer, tmp_path, "container-retry", outcomes, errors),
    )
    first.start()
    assert source.download_started.wait(timeout=1)
    second.start()
    assert source.two_lookups.wait(timeout=1)
    source.release_download.set()

    first.join(timeout=3)
    second.join(timeout=3)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(errors) == 1
    assert errors[0][0] == "container-failed"
    assert str(errors[0][1]) == "checkpoint download failed"
    assert outcomes[0][0] == "container-retry"
    assert outcomes[0][1] is not None
    assert source.download_calls == 2
    assert [payload.status for payload in states.payloads] == [
        WorkerCheckpointStatus.RestoreFailed,
        WorkerCheckpointStatus.Available,
    ]
    assert (
        tmp_path
        / "checkpoints"
        / checkpoint.checkpoint_id
        / "filesystem"
        / "workspace"
        / "state.txt"
    ).read_text(encoding="utf-8") == "warm-state"
    assert not (tmp_path / "checkpoints" / f".{checkpoint.checkpoint_id}.extract").exists()
    assert activity.protected_checkpoint_ids() == set()


def test_materialization_ownership_is_per_checkpoint_and_retention_safe() -> None:
    activity = CheckpointLeaseRegistry()
    first = activity.acquire_materialization("checkpoint-a")
    same_acquired = threading.Event()
    same_release = threading.Event()

    def acquire_same() -> None:
        with activity.acquire_materialization("checkpoint-a"):
            same_acquired.set()
            assert same_release.wait(timeout=1)

    waiter = threading.Thread(target=acquire_same)
    waiter.start()
    assert not same_acquired.wait(timeout=0.05)
    assert activity.protected_checkpoint_ids() == {"checkpoint-a"}
    with activity.retention_guard("checkpoint-a") as removable:
        assert not removable

    with activity.acquire_materialization("checkpoint-b"):
        assert activity.protected_checkpoint_ids() == {"checkpoint-a", "checkpoint-b"}

    first.close()
    assert same_acquired.wait(timeout=1)
    same_release.set()
    waiter.join(timeout=1)
    assert not waiter.is_alive()
    assert activity.protected_checkpoint_ids() == set()


def _checkpoint_archive(tmp_path: Path) -> tuple[CheckpointRecord, Path]:
    checkpoint_id = "checkpoint-1"
    checkpoint_source = tmp_path / "source" / checkpoint_id
    filesystem = checkpoint_source / "filesystem" / "workspace"
    filesystem.mkdir(parents=True)
    (filesystem / "state.txt").write_text("warm-state", encoding="utf-8")
    (checkpoint_source / "inventory.img").write_bytes(b"runtime-state")
    archive = tmp_path / "checkpoint.tar"
    with tarfile.open(archive, "w") as target:
        target.add(tmp_path / "source" / checkpoint_id, arcname=checkpoint_id)
    payload = archive.read_bytes()
    return (
        CheckpointRecord(
            checkpoint_id=checkpoint_id,
            status=CheckpointStatus.Available,
            workspace_id="workspace-1",
            stub_id="stub-1",
            stub_type="sandbox",
            cache_hash=hashlib.sha256(payload).hexdigest(),
            cache_size_bytes=len(payload),
            origin_key=f"checkpoints/{checkpoint_id}.tar",
        ),
        archive,
    )


def _context(
    checkpoint_id: str,
    *,
    startup_kind: WorkerStartupKind = WorkerStartupKind.Sandbox,
    container_id: str = "container-restored",
) -> ContainerExecutionContext:
    return ContainerExecutionContext(
        request=ContainerRequestContext(
            container_id=container_id,
            workspace_id="workspace-1",
            stub_id="stub-1",
        ),
        startup_kind=startup_kind,
        runtime=OciRuntimeName.Runc,
        checkpoint_id=checkpoint_id,
    )


def _spec(
    bundle: Path,
    config_path: Path,
    *,
    container_id: str = "container-restored",
) -> OciRuntimeContainerSpec:
    return OciRuntimeContainerSpec(
        container_id=container_id,
        runtime=RuntimeBinaryConfig(runtime=OciRuntimeName.Runc),
        bundle_path=str(bundle),
        config_path=str(config_path),
        process_spec_dir=str(bundle / "processes"),
        spec={"root": {"path": "/evicted/image/root"}},
    )


@dataclass(slots=True)
class _Source:
    checkpoint: CheckpointRecord
    archive: Path
    lookups: list[tuple[str, str]] = field(default_factory=list)
    checkpoint_activity: CheckpointLeaseRegistry | None = None
    protected_during_download: set[str] = field(default_factory=set)

    def get_checkpoint(self, checkpoint_id: str, *, workspace_id: str) -> CheckpointRecord:
        self.lookups.append((checkpoint_id, workspace_id))
        return self.checkpoint

    def download_checkpoint(self, checkpoint: CheckpointRecord, target: Path) -> None:
        _ = checkpoint
        if self.checkpoint_activity is not None:
            self.protected_during_download = self.checkpoint_activity.protected_checkpoint_ids()
        shutil.copyfile(self.archive, target)


@dataclass(slots=True)
class _StateSink:
    payloads: list[CheckpointStatePayload] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def save_checkpoint_state(self, payload: CheckpointStatePayload) -> CheckpointRecord:
        with self._lock:
            self.payloads.append(payload)
        return CheckpointRecord(checkpoint_id=payload.checkpoint_id)


@dataclass(slots=True)
class _Runtime:
    image_paths: list[str] = field(default_factory=list)
    error_before_started: str = ""
    error_after_started: str = ""
    checkpoint_activity: CheckpointLeaseRegistry | None = None
    protected_during_restore: set[str] = field(default_factory=set)

    def restore_container(
        self,
        container_id: str,
        *,
        image_path: str,
        work_dir: str,
        bundle_path: str,
        on_started: Callable[[int], None],
        output_sink: ProcessOutputSink | None = None,
        tcp_close: bool = True,
        link_remap: bool = True,
    ) -> ContainerRuntimeRunResult:
        _ = (container_id, work_dir, bundle_path, output_sink, tcp_close, link_remap)
        self.image_paths.append(image_path)
        if self.checkpoint_activity is not None:
            self.protected_during_restore = self.checkpoint_activity.protected_checkpoint_ids()
        if self.error_before_started:
            raise RuntimeError(self.error_before_started)
        on_started(4321)
        if self.error_after_started:
            raise RuntimeError(self.error_after_started)
        return ContainerRuntimeRunResult(exit_code=0, started_pid=4321)


@dataclass(slots=True)
class _ConcurrentSource:
    checkpoint: CheckpointRecord
    archive: Path
    fail_first_download: bool = False
    download_started: threading.Event = field(default_factory=threading.Event)
    release_download: threading.Event = field(default_factory=threading.Event)
    two_lookups: threading.Event = field(default_factory=threading.Event)
    download_calls: int = 0
    _lookup_count: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get_checkpoint(self, checkpoint_id: str, *, workspace_id: str) -> CheckpointRecord:
        assert checkpoint_id == self.checkpoint.checkpoint_id
        assert workspace_id == self.checkpoint.workspace_id
        with self._lock:
            self._lookup_count += 1
            if self._lookup_count >= 2:
                self.two_lookups.set()
        return self.checkpoint

    def download_checkpoint(self, checkpoint: CheckpointRecord, target: Path) -> None:
        assert checkpoint.checkpoint_id == self.checkpoint.checkpoint_id
        with self._lock:
            self.download_calls += 1
            call = self.download_calls
        if call == 1:
            self.download_started.set()
            assert self.release_download.wait(timeout=2)
            if self.fail_first_download:
                target.write_bytes(b"incomplete")
                raise RuntimeError("checkpoint download failed")
        shutil.copyfile(self.archive, target)


@dataclass(slots=True)
class _ConcurrentRuntime:
    expected_starts: int
    started_containers: list[str] = field(default_factory=list)
    concurrent_start_proven: bool = False
    _barrier: threading.Barrier = field(init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self._barrier = threading.Barrier(self.expected_starts, timeout=2)

    def restore_container(
        self,
        container_id: str,
        *,
        image_path: str,
        work_dir: str,
        bundle_path: str,
        on_started: Callable[[int], None],
        output_sink: ProcessOutputSink | None = None,
        tcp_close: bool = True,
        link_remap: bool = True,
    ) -> ContainerRuntimeRunResult:
        _ = (image_path, work_dir, bundle_path, output_sink, tcp_close, link_remap)
        on_started(4321)
        with self._lock:
            self.started_containers.append(container_id)
        self._barrier.wait()
        with self._lock:
            self.concurrent_start_proven = len(self.started_containers) == self.expected_starts
        return ContainerRuntimeRunResult(exit_code=0, started_pid=4321)


def _restore_in_thread(
    restorer: RuntimeCheckpointRestorer,
    tmp_path: Path,
    container_id: str,
    outcomes: list[tuple[str, ContainerRuntimeRunResult | None]],
    errors: list[tuple[str, Exception]],
) -> None:
    bundle = tmp_path / container_id
    bundle.mkdir()
    config_path = bundle / "config.json"
    config_path.write_text(
        json.dumps({"root": {"path": str(bundle / "rootfs")}}),
        encoding="utf-8",
    )
    try:
        result = restorer.restore(
            _context("checkpoint-1", container_id=container_id),
            _spec(bundle, config_path, container_id=container_id),
            on_started=lambda _pid: None,
        )
    except Exception as exc:
        errors.append((container_id, exc))
    else:
        outcomes.append((container_id, result))
