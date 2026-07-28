from __future__ import annotations

import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from lazycloud.abstractions.artifact import (
    Artifact,
)
from shared.http.artifacts import (
    ArtifactPublicUrlRequest,
    ArtifactPublicUrlResponse,
    ArtifactSaveResponse,
    ArtifactStat,
    ArtifactStatRequest,
    ArtifactStatResponse,
)
from shared.http.errors import HttpApiError


@dataclass(frozen=True)
class SavedChunks:
    task_id: str
    filename: str
    chunks: tuple[bytes, ...]
    content_type: str


@dataclass
class FakeOutputClient:
    save_response: ArtifactSaveResponse = field(
        default_factory=lambda: ArtifactSaveResponse(id="out_123")
    )
    stat_response: ArtifactStatResponse = field(
        default_factory=lambda: ArtifactStatResponse(
            stat=ArtifactStat(
                mode="0644",
                size=7,
                atime=datetime(2026, 1, 1, tzinfo=UTC),
                mtime=datetime(2026, 1, 2, tzinfo=UTC),
            ),
        )
    )
    public_url_response: ArtifactPublicUrlResponse = field(
        default_factory=lambda: ArtifactPublicUrlResponse(
            public_url="https://objects.example/out_123",
        )
    )
    save_requests: list[SavedChunks] = field(default_factory=list)
    stat_requests: list[ArtifactStatRequest] = field(default_factory=list)
    public_url_requests: list[ArtifactPublicUrlRequest] = field(default_factory=list)

    def artifact_save_stream(
        self,
        task_id: str,
        filename: str,
        chunks: Iterable[bytes],
        *,
        content_type: str = "application/octet-stream",
    ) -> ArtifactSaveResponse:
        self.save_requests.append(
            SavedChunks(
                task_id=task_id,
                filename=filename,
                chunks=tuple(chunks),
                content_type=content_type,
            )
        )
        return self.save_response

    def artifact_stat(self, request: ArtifactStatRequest) -> ArtifactStatResponse:
        self.stat_requests.append(request)
        return self.stat_response

    def artifact_public_url(self, request: ArtifactPublicUrlRequest) -> ArtifactPublicUrlResponse:
        self.public_url_requests.append(request)
        return self.public_url_response


def test_artifact_save_remote_chunks_file_and_returns_saved_metadata(tmp_path: Path) -> None:
    path = tmp_path / "report.txt"
    path.write_bytes(b"abcdefg")
    client = FakeOutputClient()
    artifact = Artifact(
        path=path,
        content_type="text/plain",
        task_id="task_123",
    )._bind_control(client)

    saved = artifact.save(chunk_size=3)
    stat = artifact.stat()
    public_url = artifact.public_url(
        expires=120,
    )

    assert saved.remote is True
    assert saved.artifact_id == "out_123"
    assert artifact.id == "out_123"
    assert saved.task_id == "task_123"
    assert saved.filename == "report.txt"
    assert list(client.save_requests[0].chunks) == [b"abc", b"def", b"g"]
    assert client.save_requests[0].content_type == "text/plain"
    assert stat.size == 7
    assert public_url == "https://objects.example/out_123"
    assert client.stat_requests[0].id == "out_123"
    assert client.public_url_requests[0].expires == 120


def test_artifact_save_remote_packages_directories_and_empty_files(tmp_path: Path) -> None:
    directory = tmp_path / "results"
    directory.mkdir()
    (directory / "a.txt").write_text("a", encoding="utf-8")
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")

    directory_client = FakeOutputClient()
    empty_client = FakeOutputClient()

    directory_saved = (
        Artifact(path=directory, task_id="task_123")._bind_control(directory_client).save()
    )
    empty_saved = Artifact.file(empty).save_remote(empty_client, task_id="task_123")

    assert directory_saved.path.suffix == ".zip"
    assert directory_saved.stat.packaged is True
    assert len(directory_client.save_requests) == 1
    assert directory_client.save_requests[0].filename == "results.zip"
    with zipfile.ZipFile(directory_saved.path) as archive:
        assert archive.namelist() == ["a.txt"]
    assert empty_saved.filename == "empty.txt"
    assert empty_client.save_requests[0].chunks == (b"",)


class _DenyingSaveClient(FakeOutputClient):
    def artifact_save_stream(
        self,
        task_id: str,
        filename: str,
        chunks: Iterable[bytes],
        *,
        content_type: str = "application/octet-stream",
    ) -> ArtifactSaveResponse:
        raise HttpApiError("denied", status_code=403)


class _MissingStatClient(FakeOutputClient):
    def artifact_stat(self, request: ArtifactStatRequest) -> ArtifactStatResponse:
        raise HttpApiError("missing", status_code=404)


class _FailingPublicUrlClient(FakeOutputClient):
    def artifact_public_url(self, request: ArtifactPublicUrlRequest) -> ArtifactPublicUrlResponse:
        raise HttpApiError("failed", status_code=500)
