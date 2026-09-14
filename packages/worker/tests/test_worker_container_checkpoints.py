from __future__ import annotations

import io
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from networking.internal_http import InternalHttpClient
from pydantic import JsonValue, TypeAdapter
from shared.checkpoints import CheckpointRecord
from shared.compute_policy import MachinePool
from shared.container_requests import WORKER_USER_ARTIFACT_VOLUME
from worker.checkpoint_activity import CheckpointLeaseRegistry
from worker.checkpoint_transfer import RemoteCheckpointPersister
from worker.checkpoints import CheckpointStatePayload, WorkerCheckpointStatus
from worker.container_checkpoints import (
    RuntimeCheckpointCreator,
)
from worker.container_service.models import WorkerContainerServiceInstance
from worker.execution import CHECKPOINT_FILESYSTEM_DIR
from worker.repository_client import WorkerRepositoryHttpClient, WorkerRepositoryHttpTransport

type JsonObject = dict[str, JsonValue]

_JSON_OBJECT: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
_ARTIFACT_DIR = WORKER_USER_ARTIFACT_VOLUME.strip("/")


def test_runtime_checkpoint_creator_runs_runtime_persists_archive_and_records_state(
    tmp_path: Path,
) -> None:
    upper = tmp_path / "upper"
    upper.mkdir()
    (upper / "app.py").write_text("print('ok')", encoding="utf-8")
    (upper / "config.json").write_text("application config", encoding="utf-8")
    nested = upper / "workspace"
    nested.mkdir()
    (nested / "config.json").write_text("nested config", encoding="utf-8")
    (nested / "snapshot").write_bytes(b"application snapshot")
    (nested / _ARTIFACT_DIR).mkdir()
    (nested / _ARTIFACT_DIR / "data").write_bytes(b"application data")
    (upper / _ARTIFACT_DIR).mkdir()
    (upper / _ARTIFACT_DIR / "result.txt").write_text("skip", encoding="utf-8")
    (upper / "missing-link").symlink_to("missing-target")
    checkpoint_activity = CheckpointLeaseRegistry()
    runtime = RuntimeCheckpoint(checkpoint_activity=checkpoint_activity)
    state = CheckpointState()
    uploaded: list[bytes] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            uploaded.append(request.read())
            return httpx.Response(204)
        if request.url.path.endswith("prepare-checkpoint-archive-upload"):
            return httpx.Response(200, json={"upload_url": "https://storage.test/archive"})
        return httpx.Response(200, json={"accelerator": "L4"})

    http = InternalHttpClient()
    creator = RuntimeCheckpointCreator(
        runtime=runtime,
        state_sink=state,
        persister=RemoteCheckpointPersister(
            repository=WorkerRepositoryHttpClient(
                WorkerRepositoryHttpTransport("https://repository.test", "worker-token", http=http)
            ),
            internal_http=http,
            cache_namespace="checkpoints",
        ),
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

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        http._client = client
        checkpoint_id = creator.create_checkpoint(instance)

    checkpoint_path = tmp_path / "checkpoints" / "chk-1"
    filesystem_path = checkpoint_path / CHECKPOINT_FILESYSTEM_DIR
    assert checkpoint_id == "chk-1"
    assert (filesystem_path / "app.py").exists()
    assert (filesystem_path / "missing-link").is_symlink()
    assert (filesystem_path / "missing-link").readlink() == Path("missing-target")
    assert (filesystem_path / "config.json").read_text() == "application config"
    assert (filesystem_path / "workspace" / "config.json").read_text() == "nested config"
    assert (filesystem_path / "workspace" / "snapshot").read_bytes() == b"application snapshot"
    assert (
        filesystem_path / "workspace" / _ARTIFACT_DIR / "data"
    ).read_bytes() == b"application data"
    assert not (filesystem_path / _ARTIFACT_DIR).exists()
    payload = state.payloads[-1]
    with tarfile.open(fileobj=io.BytesIO(uploaded[0])) as archive:
        assert "chk-1/filesystem/app.py" in archive.getnames()
    assert payload.origin_key == "checkpoints/chk-1.tar"
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
        persister=RemoteCheckpointPersister(
            repository=WorkerRepositoryHttpClient(
                WorkerRepositoryHttpTransport("https://repository.test", "worker-token")
            ),
            internal_http=InternalHttpClient(),
            cache_namespace="checkpoints",
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
class CheckpointState:
    payloads: list[CheckpointStatePayload] = field(default_factory=list)

    def save_checkpoint_state(self, payload: CheckpointStatePayload) -> CheckpointRecord:
        self.payloads.append(payload)
        return CheckpointRecord(checkpoint_id=payload.checkpoint_id)
