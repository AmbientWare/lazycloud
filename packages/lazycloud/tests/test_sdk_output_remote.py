from __future__ import annotations

import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from lazycloud.abstractions.output import (
    Output,
)
from shared.http.errors import HttpApiError
from shared.http.outputs import (
    OutputPublicUrlRequest,
    OutputPublicUrlResponse,
    OutputSaveResponse,
    OutputStat,
    OutputStatRequest,
    OutputStatResponse,
)


@dataclass(frozen=True)
class SavedChunks:
    task_id: str
    filename: str
    chunks: tuple[bytes, ...]
    content_type: str


@dataclass
class FakeOutputClient:
    save_response: OutputSaveResponse = field(
        default_factory=lambda: OutputSaveResponse(id="out_123")
    )
    stat_response: OutputStatResponse = field(
        default_factory=lambda: OutputStatResponse(
            stat=OutputStat(
                mode="0644",
                size=7,
                atime=datetime(2026, 1, 1, tzinfo=UTC),
                mtime=datetime(2026, 1, 2, tzinfo=UTC),
            ),
        )
    )
    public_url_response: OutputPublicUrlResponse = field(
        default_factory=lambda: OutputPublicUrlResponse(
            public_url="https://objects.example/out_123",
        )
    )
    save_requests: list[SavedChunks] = field(default_factory=list)
    stat_requests: list[OutputStatRequest] = field(default_factory=list)
    public_url_requests: list[OutputPublicUrlRequest] = field(default_factory=list)

    def output_save_stream(
        self,
        task_id: str,
        filename: str,
        chunks: Iterable[bytes],
        *,
        content_type: str = "application/octet-stream",
    ) -> OutputSaveResponse:
        self.save_requests.append(
            SavedChunks(
                task_id=task_id,
                filename=filename,
                chunks=tuple(chunks),
                content_type=content_type,
            )
        )
        return self.save_response

    def output_stat(self, request: OutputStatRequest) -> OutputStatResponse:
        self.stat_requests.append(request)
        return self.stat_response

    def output_public_url(self, request: OutputPublicUrlRequest) -> OutputPublicUrlResponse:
        self.public_url_requests.append(request)
        return self.public_url_response


def test_output_save_remote_chunks_file_and_returns_saved_metadata(tmp_path: Path) -> None:
    path = tmp_path / "report.txt"
    path.write_bytes(b"abcdefg")
    client = FakeOutputClient()
    output = Output(
        path=path,
        content_type="text/plain",
        task_id="task_123",
    )._bind_control(client)

    saved = output.save(chunk_size=3)
    stat = output.stat()
    public_url = output.public_url(
        expires=120,
        gateway_external_url="https://gateway.example",
    )

    assert saved.remote is True
    assert saved.output_id == "out_123"
    assert output.id == "out_123"
    assert saved.task_id == "task_123"
    assert saved.filename == "report.txt"
    assert list(client.save_requests[0].chunks) == [b"abc", b"def", b"g"]
    assert client.save_requests[0].content_type == "text/plain"
    assert stat.size == 7
    assert public_url == "https://objects.example/out_123"
    assert client.stat_requests[0].id == "out_123"
    assert client.public_url_requests[0].expires == 120
    assert client.public_url_requests[0].gateway_external_url == "https://gateway.example"


def test_output_save_remote_packages_directories_and_empty_files(tmp_path: Path) -> None:
    directory = tmp_path / "results"
    directory.mkdir()
    (directory / "a.txt").write_text("a", encoding="utf-8")
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")

    directory_client = FakeOutputClient()
    empty_client = FakeOutputClient()

    directory_saved = (
        Output(path=directory, task_id="task_123")._bind_control(directory_client).save()
    )
    empty_saved = Output.file(empty).save_remote(empty_client, task_id="task_123")

    assert directory_saved.path.suffix == ".zip"
    assert directory_saved.stat.packaged is True
    assert len(directory_client.save_requests) == 1
    assert directory_client.save_requests[0].filename == "results.zip"
    with zipfile.ZipFile(directory_saved.path) as archive:
        assert archive.namelist() == ["a.txt"]
    assert empty_saved.filename == "empty.txt"
    assert empty_client.save_requests[0].chunks == (b"",)


class _DenyingSaveClient(FakeOutputClient):
    def output_save_stream(
        self,
        task_id: str,
        filename: str,
        chunks: Iterable[bytes],
        *,
        content_type: str = "application/octet-stream",
    ) -> OutputSaveResponse:
        raise HttpApiError("denied", status_code=403)


class _MissingStatClient(FakeOutputClient):
    def output_stat(self, request: OutputStatRequest) -> OutputStatResponse:
        raise HttpApiError("missing", status_code=404)


class _FailingPublicUrlClient(FakeOutputClient):
    def output_public_url(self, request: OutputPublicUrlRequest) -> OutputPublicUrlResponse:
        raise HttpApiError("failed", status_code=500)


def test_output_from_pil_image_and_zip_helpers(tmp_path: Path) -> None:
    directory = tmp_path / "bundle"
    directory.mkdir()
    (directory / "result.txt").write_text("result", encoding="utf-8")
    output = Output(path=directory)

    assert output.zipped_path.name == "bundle.zip"
    archive_path = output.zip_dir(directory, target_dir=tmp_path)
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == ["result.txt"]

    image_output = Output.from_pil_image(FakeImage(), format="png")
    assert image_output.path.suffix == ".png"
    assert image_output.stat().size == 5


class FakeImage:
    def save(self, fp: str | Path, format: str | None = None, **params: object) -> None:
        _ = (format, params)
        Path(fp).write_bytes(b"image")
