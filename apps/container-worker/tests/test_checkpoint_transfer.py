from __future__ import annotations

import tarfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import cast

import container_worker_app.checkpoint_transfer as checkpoint_transfer
import pytest
from container_worker_app.checkpoint_transfer import RemoteCheckpointPersister
from networking.internal_http import InternalHttpClient
from pydantic import JsonValue
from worker.checkpoints import CheckpointPersistenceAction, CheckpointPersistencePlan
from worker.repository_client import WorkerRepositoryHttpClient
from worker.repository_payloads import (
    PersistCheckpointArchiveResponse,
    PrepareCheckpointArchiveUploadResponse,
)


def test_remote_checkpoint_persister_streams_archive_to_presigned_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint_root = tmp_path / "checkpoints"
    checkpoint_path = checkpoint_root / "checkpoint-1"
    checkpoint_path.mkdir(parents=True)
    (checkpoint_path / "state.txt").write_text("ready\n", encoding="utf-8")
    archive_path = tmp_path / "checkpoint-1.tar"
    transport = _FakeWorkerRepositoryTransport(
        posts={
            "/worker-repository/prepare-checkpoint-archive-upload": (
                PrepareCheckpointArchiveUploadResponse(
                    upload_url="http://storage/checkpoint-1",
                ).model_dump(mode="json")
            ),
            "/worker-repository/persist-checkpoint-archive": (
                PersistCheckpointArchiveResponse().model_dump(mode="json")
            ),
        }
    )
    uploaded: list[bytes] = []

    def capture_upload(
        _http: InternalHttpClient,
        _url: str,
        path: Path,
        *,
        content_length: int,
    ) -> None:
        uploaded.append(path.read_bytes() if path.stat().st_size == content_length else b"")

    monkeypatch.setattr(
        checkpoint_transfer,
        "_put_presigned_checkpoint_archive",
        capture_upload,
    )
    plan = CheckpointPersistencePlan(
        action=CheckpointPersistenceAction.Persist,
        checkpoint_id="checkpoint-1",
        checkpoint_path=str(checkpoint_path),
        archive_path=str(archive_path),
        origin_key="checkpoints/checkpoint-1.tar",
        create_archive=True,
        upload_to_origin_storage=True,
        store_archive_in_cache=True,
    )

    result = RemoteCheckpointPersister(
        WorkerRepositoryHttpClient(transport),
        InternalHttpClient(),
        cache_namespace="checkpoints",
    ).persist_checkpoint(plan)

    payload_tar = tmp_path / "payload.tar"
    payload_tar.write_bytes(uploaded[0])
    with tarfile.open(payload_tar) as archive:
        names = archive.getnames()
    assert result.checkpoint_id == "checkpoint-1"
    assert result.origin_key == "checkpoints/checkpoint-1.tar"
    assert "checkpoint-1/state.txt" in names


def test_checkpoint_transfer_errors_never_disclose_capability_query(tmp_path: Path) -> None:
    sentinel = "never-log-this-checkpoint-signature"

    class _FailingHttp:
        """An internal client whose failure carries the signed URL."""

        def request(self, *args: object, **kwargs: object) -> object:
            _ = args, kwargs
            raise RuntimeError(f"failed request with {sentinel}")

        def stream(self, *args: object, **kwargs: object) -> object:
            _ = args, kwargs
            raise RuntimeError(f"failed request with {sentinel}")

    failing = cast(InternalHttpClient, _FailingHttp())
    capability = f"https://objects.example.test/archive?X-Amz-Signature={sentinel}"
    checkpoint = tmp_path / "checkpoint.tar"
    checkpoint.write_bytes(b"checkpoint")

    with pytest.raises(RuntimeError) as upload_error:
        checkpoint_transfer._put_presigned_checkpoint_archive(
            failing,
            capability,
            checkpoint,
            content_length=checkpoint.stat().st_size,
        )
    with pytest.raises(RuntimeError) as download_error:
        checkpoint_transfer._download_presigned_url(
            failing,
            capability,
            tmp_path / "download.tar",
            timeout_seconds=5,
            resource_name="checkpoint archive",
        )

    assert sentinel not in str(upload_error.value)
    assert sentinel not in str(download_error.value)
    assert upload_error.value.__cause__ is None
    assert download_error.value.__cause__ is None


class _FakeWorkerRepositoryTransport:
    def __init__(
        self,
        *,
        posts: Mapping[str, Mapping[str, JsonValue]] | None = None,
    ) -> None:
        self._posts = posts or {}
        self.posts: list[tuple[str, dict[str, JsonValue]]] = []
        self.bearer_token = "bootstrap-token"

    def set_bearer_token(self, token: str) -> None:
        self.bearer_token = token

    def post(
        self,
        path: str,
        payload: Mapping[str, JsonValue],
    ) -> dict[str, JsonValue]:
        self.posts.append((path, dict(payload)))
        return dict(self._posts.get(path, {"ok": True}))

    def stream(
        self,
        path: str,
        payload: Mapping[str, JsonValue],
    ) -> Iterator[dict[str, JsonValue]]:
        _ = path, payload
        return iter(())
