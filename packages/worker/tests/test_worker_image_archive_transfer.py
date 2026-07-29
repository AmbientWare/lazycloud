from __future__ import annotations

import hashlib
from base64 import b64encode
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from worker.image_archive_transfer import (
    ImageArchiveIntegrityError,
    ImageArchiveTransferError,
    download_image_archive,
    upload_image_archive,
)

from worker import image_archive_transfer


@dataclass(slots=True)
class _ArchiveServerState:
    content: bytes
    get_statuses: list[int] = field(default_factory=lambda: [200])
    put_statuses: list[int] = field(default_factory=lambda: [200])
    get_count: int = 0
    put_count: int = 0
    uploaded: bytes = b""
    upload_headers: dict[str, str] = field(default_factory=dict)


@contextmanager
def _serve_archives(state: _ArchiveServerState) -> Iterator[str]:
    class ArchiveHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status = state.get_statuses[min(state.get_count, len(state.get_statuses) - 1)]
            state.get_count += 1
            self.send_response(status)
            if status == 200:
                self.send_header("content-length", str(len(state.content)))
            self.end_headers()
            if status == 200:
                self.wfile.write(state.content)

        def do_PUT(self) -> None:
            length = int(self.headers["content-length"])
            state.uploaded = self.rfile.read(length)
            state.upload_headers = {name.lower(): value for name, value in self.headers.items()}
            status = state.put_statuses[min(state.put_count, len(state.put_statuses) - 1)]
            state.put_count += 1
            self.send_response(status)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            _ = format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), ArchiveHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/archive"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_image_archive_transfers_retry_transient_responses_and_preserve_signed_headers(
    tmp_path: Path,
) -> None:
    content = b"verified archive bytes"
    digest = hashlib.sha256(content).hexdigest()
    source = tmp_path / "source.rclip"
    source.write_bytes(content)
    target = tmp_path / "cache" / "image.rclip"
    state = _ArchiveServerState(
        content=content,
        get_statuses=[503, 200],
        put_statuses=[503, 200],
    )
    signed_headers = {
        "content-type": "application/x-tar",
        "content-length": str(len(content)),
        "x-amz-checksum-sha256": b64encode(bytes.fromhex(digest)).decode(),
        "x-amz-meta-artifact-sha256": digest,
        "x-amz-meta-owner": "image-build",
    }

    with _serve_archives(state) as url:
        upload_image_archive(
            url,
            source,
            headers=signed_headers,
            archive_size_bytes=len(content),
            archive_sha256=digest,
            content_type="application/x-tar",
            timeout_seconds=10,
            retry_delays_seconds=(0,),
        )
        written = download_image_archive(
            url,
            target,
            archive_size_bytes=len(content),
            archive_sha256=digest,
            timeout_seconds=10,
            retry_delays_seconds=(0,),
        )

    assert state.put_count == 2
    assert state.get_count == 2
    assert state.uploaded == content
    assert state.upload_headers["x-amz-meta-artifact-sha256"] == digest
    assert state.upload_headers["x-amz-meta-owner"] == "image-build"
    assert written == len(content)
    assert target.read_bytes() == content
    assert not list(target.parent.glob(".*.partial"))


def test_image_archive_download_integrity_failure_is_terminal_and_preserves_target(
    tmp_path: Path,
) -> None:
    expected = b"expected archive"
    corrupted = b"corrupted bytes"
    target = tmp_path / "image.rclip"
    target.write_bytes(b"existing archive")
    state = _ArchiveServerState(content=corrupted)

    with (
        _serve_archives(state) as url,
        pytest.raises(ImageArchiveIntegrityError, match="content length"),
    ):
        download_image_archive(
            url,
            target,
            archive_size_bytes=len(expected),
            archive_sha256=hashlib.sha256(expected).hexdigest(),
            timeout_seconds=10,
            retry_delays_seconds=(0, 0),
        )

    assert state.get_count == 1
    assert target.read_bytes() == b"existing archive"
    assert not list(tmp_path.glob(".*.partial"))


def test_image_archive_transfer_error_never_discloses_capability_query(
    tmp_path: Path,
) -> None:
    content = b"archive"
    target = tmp_path / "image.rclip"
    state = _ArchiveServerState(content=content, get_statuses=[403])
    sentinel = "never-log-this-signature"

    with (
        _serve_archives(state) as base_url,
        pytest.raises(ImageArchiveTransferError) as caught,
    ):
        download_image_archive(
            f"{base_url}?X-Amz-Signature={sentinel}",
            target,
            archive_size_bytes=len(content),
            archive_sha256=hashlib.sha256(content).hexdigest(),
            timeout_seconds=10,
            retry_delays_seconds=(0, 0),
        )

    assert state.get_count == 1
    assert sentinel not in str(caught.value)
    assert not target.exists()


def test_image_archive_unexpected_client_error_never_discloses_capability_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"archive"
    digest = hashlib.sha256(content).hexdigest()
    source = tmp_path / "image.rclip"
    source.write_bytes(content)
    sentinel = "never-log-this-client-signature"

    class FailingConnection:
        def __init__(
            self,
            host: str,
            port: int | None = None,
            timeout: float | None = None,
        ) -> None:
            _ = host, port, timeout

        def request(self, *args: object, **kwargs: object) -> None:
            _ = kwargs
            raise RuntimeError(f"failed request {args!r} with {sentinel}")

        def close(self) -> None:
            return

    monkeypatch.setattr(
        image_archive_transfer.http.client,
        "HTTPSConnection",
        FailingConnection,
    )

    with pytest.raises(ImageArchiveTransferError) as caught:
        upload_image_archive(
            f"https://objects.example.test/archive?X-Amz-Signature={sentinel}",
            source,
            headers={
                "content-type": "application/x-tar",
                "content-length": str(len(content)),
                "x-amz-checksum-sha256": b64encode(bytes.fromhex(digest)).decode(),
                "x-amz-meta-artifact-sha256": digest,
            },
            archive_size_bytes=len(content),
            archive_sha256=digest,
            content_type="application/x-tar",
            timeout_seconds=10,
            retry_delays_seconds=(),
        )

    assert sentinel not in str(caught.value)
    assert caught.value.__cause__ is None
