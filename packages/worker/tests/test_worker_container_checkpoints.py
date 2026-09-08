from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from cache.protocol import CacheContentStoreResult, CacheContentStoreStatus
from pydantic import JsonValue, TypeAdapter
from shared.checkpoints import CheckpointRecord
from shared.compute_policy import MachinePool
from shared.container_requests import WORKER_USER_ARTIFACT_VOLUME
from worker.checkpoint_activity import CheckpointLeaseRegistry
from worker.checkpoints import CheckpointStatePayload, WorkerCheckpointStatus
from worker.container_checkpoints import (
    ContainerFilesystemArchiveCreator,
    ContainerImageArchiveResult,
    FilesystemCheckpointPersister,
    RuntimeCheckpointCreator,
)
from worker.container_client.models import ContainerArchiveResponse
from worker.container_service.models import WorkerContainerServiceInstance
from worker.execution import CHECKPOINT_FILESYSTEM_DIR
from worker.image_build_execution import WorkerImageArchivePublishResult

type JsonObject = dict[str, JsonValue]

_JSON_OBJECT: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
_ARTIFACT_DIR = WORKER_USER_ARTIFACT_VOLUME.strip("/")


def test_runtime_checkpoint_creator_runs_runtime_persists_archive_and_records_state(
    tmp_path: Path,
) -> None:
    upper = tmp_path / "upper"
    upper.mkdir()
    (upper / "app.py").write_text("print('ok')", encoding="utf-8")
    (upper / "config.json").write_text("skip", encoding="utf-8")
    (upper / _ARTIFACT_DIR).mkdir()
    (upper / _ARTIFACT_DIR / "result.txt").write_text("skip", encoding="utf-8")
    (upper / "missing-link").symlink_to("missing-target")
    checkpoint_activity = CheckpointLeaseRegistry()
    runtime = RuntimeCheckpoint(checkpoint_activity=checkpoint_activity)
    state = CheckpointState()
    uploader = CheckpointUploader()
    cache = CheckpointCache()
    creator = RuntimeCheckpointCreator(
        runtime=runtime,
        state_sink=state,
        persister=FilesystemCheckpointPersister(uploader=uploader, cache_store=cache),
        checkpoint_root=str(tmp_path / "checkpoints"),
        content_cache_available=True,
        id_factory=lambda: "chk-1",
        checkpoint_activity=checkpoint_activity,
    )
    instance = WorkerContainerServiceInstance(
        container_id="ctr-1",
        root_path=str(upper),
        upper_path=str(upper),
        config_path=str(tmp_path / "bundle" / "config.json"),
        container_ip="192.168.0.2",
        stub_id="stub-1",
        exposed_ports=[8001],
        pool=MachinePool("pool-a"),
        workspace_storage_available=True,
        cache_available=True,
        gpu="l4",
    )

    checkpoint_id = creator.create_checkpoint(instance)

    checkpoint_path = tmp_path / "checkpoints" / "chk-1"
    filesystem_path = checkpoint_path / CHECKPOINT_FILESYSTEM_DIR
    assert checkpoint_id == "chk-1"
    assert runtime.calls == [
        (
            "ctr-1",
            str(checkpoint_path),
            "/tmp/chk-1",
            True,
            True,
            True,
            True,
        )
    ]
    assert (filesystem_path / "app.py").exists()
    assert (filesystem_path / "missing-link").is_symlink()
    assert (filesystem_path / "missing-link").readlink() == Path("missing-target")
    assert not (filesystem_path / "config.json").exists()
    assert not (filesystem_path / _ARTIFACT_DIR).exists()
    assert uploader.calls[0][0] == "checkpoints/chk-1.tar"
    payload = state.payloads[-1]
    assert cache.calls[0][1:] == ("checkpoints/chk-1.tar", payload.cache_hash)
    assert not (tmp_path / "checkpoints" / "chk-1.tar").exists()
    assert payload.checkpoint_id == "chk-1"
    assert payload.status is WorkerCheckpointStatus.Available
    assert payload.container_ip == "192.168.0.2"
    assert payload.exposed_ports == [8001]
    assert payload.accelerator == "L4"
    assert runtime.protected_during_checkpoint == {"chk-1"}
    assert checkpoint_activity.protected_checkpoint_ids() == set()


def test_runtime_checkpoint_creator_records_failed_state_on_runtime_error(
    tmp_path: Path,
) -> None:
    upper = tmp_path / "upper"
    upper.mkdir()
    checkpoint_path = tmp_path / "checkpoints" / "chk-1"
    checkpoint_path.mkdir(parents=True)
    (checkpoint_path / "partial.img").write_bytes(b"partial")
    checkpoint_archive = tmp_path / "checkpoints" / "chk-1.tar"
    checkpoint_archive.write_bytes(b"partial archive")
    runtime = RuntimeCheckpoint(error="checkpoint failed")
    state = CheckpointState()
    creator = RuntimeCheckpointCreator(
        runtime=runtime,
        state_sink=state,
        persister=FilesystemCheckpointPersister(
            uploader=CheckpointUploader(),
            cache_store=CheckpointCache(),
        ),
        checkpoint_root=str(tmp_path / "checkpoints"),
        content_cache_available=True,
        id_factory=lambda: "chk-1",
    )
    instance = WorkerContainerServiceInstance(
        container_id="ctr-1",
        root_path=str(upper),
        upper_path=str(upper),
        workspace_storage_available=True,
        cache_available=True,
    )

    try:
        creator.create_checkpoint(instance)
    except RuntimeError as exc:
        assert str(exc) == "checkpoint failed"
    else:
        raise AssertionError("checkpoint error was not raised")

    assert state.payloads[-1].status is WorkerCheckpointStatus.CheckpointFailed
    assert not checkpoint_path.exists()
    assert not checkpoint_archive.exists()


def test_container_filesystem_archive_creator_rejects_non_running_container(
    tmp_path: Path,
) -> None:
    creator = ContainerFilesystemArchiveCreator(
        runtime=RuntimeStatus(status_value="stopped"),
        archiver=ImageArchiver(),
    )
    instance = WorkerContainerServiceInstance(container_id="ctr-1", root_path=str(tmp_path))

    responses = tuple(creator.archive_container(instance, image_id="image-1"))

    assert responses == (
        ContainerArchiveResponse(
            done=True,
            success=False,
            error_msg="Container not running",
        ),
    )


def test_container_filesystem_archive_creator_requires_durable_publication(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "image-1.rclip"
    archive_path.write_bytes(b"archive")
    creator = ContainerFilesystemArchiveCreator(
        runtime=RuntimeStatus(status_value="running"),
        archiver=ImageArchiver(
            result=ContainerImageArchiveResult(success=True, archive_path=str(archive_path))
        ),
        publisher=ImagePublisher(error="object storage unavailable"),
    )
    instance = WorkerContainerServiceInstance(container_id="ctr-1", root_path=str(tmp_path))

    responses = tuple(creator.archive_container(instance, image_id="image-1"))

    assert responses[-1] == ContainerArchiveResponse(
        done=True,
        success=False,
        error_msg="object storage unavailable",
    )


@dataclass(slots=True)
class RuntimeCheckpoint:
    error: str = ""
    calls: list[tuple[str, str, str, bool, bool, bool, bool]] = field(default_factory=list)
    checkpoint_activity: CheckpointLeaseRegistry | None = None
    protected_during_checkpoint: set[str] = field(default_factory=set)

    def checkpoint_container(
        self,
        container_id: str,
        *,
        image_path: str,
        work_dir: str,
        leave_running: bool = True,
        allow_open_tcp: bool = True,
        skip_in_flight: bool = True,
        link_remap: bool = True,
    ) -> None:
        self.calls.append(
            (
                container_id,
                image_path,
                work_dir,
                leave_running,
                allow_open_tcp,
                skip_in_flight,
                link_remap,
            )
        )
        if self.checkpoint_activity is not None:
            self.protected_during_checkpoint = self.checkpoint_activity.protected_checkpoint_ids()
        if self.error:
            raise RuntimeError(self.error)


@dataclass(slots=True)
class RuntimeStatus:
    status_value: str

    def status(self, container_id: str) -> str:
        _ = container_id
        return self.status_value


@dataclass(slots=True)
class CheckpointState:
    payloads: list[CheckpointStatePayload] = field(default_factory=list)

    def save_checkpoint_state(self, payload: CheckpointStatePayload) -> CheckpointRecord:
        self.payloads.append(payload)
        return CheckpointRecord(checkpoint_id=payload.checkpoint_id)


@dataclass(slots=True)
class CheckpointUploader:
    calls: list[tuple[str, Path]] = field(default_factory=list)

    def upload_file(self, key: str, path: Path) -> None:
        self.calls.append((key, path))


@dataclass(slots=True)
class CheckpointCache:
    calls: list[tuple[Path, str, str]] = field(default_factory=list)

    def store_file(
        self,
        path: Path,
        *,
        cache_path: str,
        routing_key: str,
    ) -> CacheContentStoreResult:
        self.calls.append((path, cache_path, routing_key))
        return CacheContentStoreResult(
            status=CacheContentStoreStatus.Stored,
            content_hash=routing_key,
            cache_path=cache_path,
        )


@dataclass(slots=True)
class ImageArchiver:
    progress_values: list[int] = field(default_factory=list)
    result: ContainerImageArchiveResult = field(
        default_factory=lambda: ContainerImageArchiveResult(success=True)
    )
    calls: list[tuple[Path, str]] = field(default_factory=list)

    def archive_image(
        self,
        source_path: Path,
        image_id: str,
        progress: Callable[[int], None],
    ) -> ContainerImageArchiveResult:
        self.calls.append((source_path, image_id))
        for value in self.progress_values:
            progress(value)
        return self.result


@dataclass(slots=True)
class ImagePublisher:
    error: str = ""
    calls: list[tuple[str, Path, str, str]] = field(default_factory=list)

    def publish_image_archive(
        self,
        *,
        image_id: str,
        archive_path: Path,
        workspace_id: str = "",
        stub_id: str = "",
    ) -> WorkerImageArchivePublishResult:
        self.calls.append((image_id, archive_path, workspace_id, stub_id))
        return WorkerImageArchivePublishResult(
            ok=not self.error,
            image_id=image_id,
            error_message=self.error,
        )
