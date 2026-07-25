from __future__ import annotations

import hashlib
import http.client
import io
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http import HTTPStatus
from pathlib import Path

import pytest
from cache.protocol import (
    CacheContentCompletenessStatus,
    CacheContentReadRequest,
    CacheContentReadStatus,
    CacheContentStoreStatus,
)
from cache.server import (
    CacheCapacityError,
    CacheDiskUsage,
    CacheUnavailableError,
    FileCacheServer,
    WorkerCacheHttpClient,
    WorkerCacheHttpService,
)
from tests.cache_fakes import empty_cache_disk_usage

TOKEN = "current-cache-service-token"
PREVIOUS_TOKEN = "previous-cache-service-token"


@contextmanager
def _running_cache(
    root: Path,
    *,
    max_content_bytes: int = 64,
    max_object_bytes: int = 32,
    max_metadata_entries: int = 4,
) -> Iterator[WorkerCacheHttpService]:
    service = WorkerCacheHttpService(
        FileCacheServer(
            root,
            max_content_bytes=max_content_bytes,
            max_object_bytes=max_object_bytes,
            max_metadata_entries=max_metadata_entries,
            recent_access_guard_seconds=0,
            disk_usage_reader=empty_cache_disk_usage,
        ),
        service_token=TOKEN,
        previous_service_token=PREVIOUS_TOKEN,
        port=0,
    )
    thread = service.start_in_thread()
    try:
        yield service
    finally:
        service.shutdown()
        thread.join(timeout=2)


def test_cache_http_exposes_only_authenticated_health_head_get_put_and_metadata(
    tmp_path: Path,
) -> None:
    with _running_cache(tmp_path / "cache") as service:
        for path, method in (("/health", "GET"), ("/content", "PUT")):
            assert _status(service, method, path) == HTTPStatus.UNAUTHORIZED
            assert _status(service, method, path, token="wrong") == HTTPStatus.UNAUTHORIZED
        assert _status(service, "GET", "/health", token=TOKEN) == HTTPStatus.OK
        assert _status(service, "GET", "/health", token=PREVIOUS_TOKEN) == HTTPStatus.OK
        assert _status(service, "POST", "/content", token=TOKEN, body=b"") in {
            HTTPStatus.NOT_FOUND,
            HTTPStatus.NOT_IMPLEMENTED,
        }
        assert _status(service, "POST", "/drain", token=TOKEN, body=b"") in {
            HTTPStatus.NOT_FOUND,
            HTTPStatus.NOT_IMPLEMENTED,
        }


def test_cache_http_put_validates_hash_and_streams_standard_ranges(tmp_path: Path) -> None:
    payload = b"0123456789"
    digest = hashlib.sha256(payload).hexdigest()
    source = tmp_path / "source.bin"
    source.write_bytes(payload)
    with _running_cache(tmp_path / "cache") as service:
        client = WorkerCacheHttpClient(service.endpoint, service_token=TOKEN)
        rejected = client.store_content_from_local_file(
            source,
            expected_hash="f" * 64,
            cache_path="/images/bad.rclip",
        )
        stored = client.store_content_from_local_file(
            source,
            expected_hash=digest,
            cache_path="/images/good.rclip",
        )
        read = client.read_content(CacheContentReadRequest(content_hash=digest, offset=3, length=4))
        metadata = client.content_metadata("/images/good.rclip")

        assert rejected.status is CacheContentStoreStatus.HashMismatch
        assert not service.cache.content_path("f" * 64).exists()
        assert stored.stored
        assert read.status is CacheContentReadStatus.Hit
        assert read.data == b"3456"
        assert metadata is not None
        assert metadata.content_hash == digest

        connection = _connection(service)
        connection.request(
            "GET",
            f"/content/{digest}",
            headers={"Authorization": f"Bearer {TOKEN}", "Range": "bytes=2-5"},
        )
        response = connection.getresponse()
        body = response.read()
        connection.close()
        assert response.status == HTTPStatus.PARTIAL_CONTENT
        assert response.getheader("Content-Range") == "bytes 2-5/10"
        assert body == b"2345"


def test_cache_client_distinguishes_miss_short_read_corruption_and_outage(tmp_path: Path) -> None:
    payload = b"cache-payload"
    digest = hashlib.sha256(payload).hexdigest()
    with _running_cache(tmp_path / "cache") as service:
        client = WorkerCacheHttpClient(service.endpoint, service_token=TOKEN)
        miss = client.read_content(CacheContentReadRequest(content_hash="a" * 64, length=1))
        completeness = client.has_complete_content("a" * 64)
        service.cache.store_content_bytes(payload, expected_hash=digest)
        short = client.read_content(
            CacheContentReadRequest(content_hash=digest, offset=0, length=len(payload) + 4)
        )
        service.cache.content_path(digest).write_bytes(b"x" * len(payload))
        corrupt = client.read_content(
            CacheContentReadRequest(content_hash=digest, offset=0, length=len(payload))
        )

        assert miss.status is CacheContentReadStatus.Miss
        assert completeness.status is CacheContentCompletenessStatus.Missing
        assert short.status is CacheContentReadStatus.ShortRead
        assert corrupt.status is CacheContentReadStatus.Corrupt

    started = time.monotonic()
    outage = client.read_content(CacheContentReadRequest(content_hash=digest, length=1))
    elapsed = time.monotonic() - started
    assert outage.status is CacheContentReadStatus.Unavailable
    assert elapsed < 1
    with pytest.raises(CacheUnavailableError):
        client.content_metadata("/images/good.rclip")


def test_cache_startup_removes_temporary_writes_and_reconciles_metadata(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    incoming = root / "contents" / ".incoming"
    metadata = root / "metadata" / "paths"
    incoming.mkdir(parents=True)
    metadata.mkdir(parents=True)
    (incoming / "abandoned.tmp").write_bytes(b"partial")
    (metadata / ".metadata.json.abandoned.tmp").write_text("partial", encoding="utf-8")
    (metadata / "invalid.json").write_text("not-json", encoding="utf-8")

    cache = FileCacheServer(root, disk_usage_reader=empty_cache_disk_usage)
    cache.prepare()

    assert not list(incoming.glob("*.tmp"))
    assert not list(metadata.glob("*.tmp"))
    assert not (metadata / "invalid.json").exists()


def test_cache_capacity_reservations_prevent_concurrent_overcommit(tmp_path: Path) -> None:
    cache = FileCacheServer(
        tmp_path / "cache",
        max_content_bytes=8,
        max_object_bytes=8,
        recent_access_guard_seconds=0,
        disk_usage_reader=empty_cache_disk_usage,
    )
    release = threading.Event()
    started = threading.Event()

    class BlockingReader:
        read_once = False

        def read(self, size: int | None = -1, /) -> bytes:
            del size
            if self.read_once:
                return b""
            self.read_once = True
            started.set()
            release.wait(timeout=2)
            return b"12345678"

    thread = threading.Thread(
        target=lambda: cache.store_content_stream(BlockingReader(), expected_size_bytes=8)
    )
    thread.start()
    assert started.wait(timeout=2)
    with pytest.raises(CacheCapacityError):
        cache.store_content_stream(io.BytesIO(b"abcdefgh"), expected_size_bytes=8)
    release.set()
    thread.join(timeout=2)
    assert cache.health().content_bytes == 8
    assert cache._reserved_content_bytes == 0


def test_cache_admission_honors_disk_pressure_and_metadata_bound(tmp_path: Path) -> None:
    pressured = FileCacheServer(
        tmp_path / "pressure",
        disk_usage_reader=lambda path: CacheDiskUsage(total=100, used=91, free=9),
    )
    with pytest.raises(CacheCapacityError):
        pressured.store_content_bytes(b"content")

    bounded = FileCacheServer(
        tmp_path / "bounded",
        max_metadata_entries=2,
        disk_usage_reader=empty_cache_disk_usage,
    )
    for index in range(3):
        bounded.store_content_bytes(bytes([index]), cache_path=f"/images/{index}.rclip")
    assert len(list((bounded.root / "metadata" / "paths").glob("*.json"))) == 2


def test_cache_http_rejects_oversize_and_incomplete_uploads(tmp_path: Path) -> None:
    with _running_cache(tmp_path / "cache", max_content_bytes=16, max_object_bytes=8) as service:
        connection = _connection(service)
        connection.request(
            "PUT",
            "/content",
            body=b"",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Length": "9"},
        )
        response = connection.getresponse()
        response.read()
        connection.close()
        assert response.status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE

        raw = socket.create_connection((service.host, service.port), timeout=2)
        raw.sendall(
            b"PUT /content HTTP/1.1\r\n"
            + f"Host: {service.host}\r\n".encode()
            + f"Authorization: Bearer {TOKEN}\r\n".encode()
            + b"Content-Length: 6\r\nConnection: close\r\n\r\nabc"
        )
        raw.shutdown(socket.SHUT_WR)
        assert b" 400 " in raw.recv(256).split(b"\r\n", 1)[0]
        raw.close()
        assert service.cache.health().content_count == 0


def _connection(service: WorkerCacheHttpService) -> http.client.HTTPConnection:
    return http.client.HTTPConnection(service.host, service.port, timeout=2)


def _status(
    service: WorkerCacheHttpService,
    method: str,
    path: str,
    *,
    token: str = "",
    body: bytes | None = None,
) -> int:
    connection = _connection(service)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    response.read()
    connection.close()
    return response.status
