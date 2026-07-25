from __future__ import annotations

import hashlib
import hmac
import http.client
import os
import shutil
import threading
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from shared.contracts import ContractModel
from shared.timestamps import utc_now

from cache.protocol import (
    DEFAULT_CONTENT_CHUNK_BYTES,
    CacheContentCompleteness,
    CacheContentCompletenessStatus,
    CacheContentMetadata,
    CacheContentReadRequest,
    CacheContentReadResult,
    CacheContentReadStatus,
    CacheContentStoreResult,
    CacheContentStoreStatus,
)


class BinaryReadable(Protocol):
    def read(self, size: int | None = -1, /) -> bytes: ...


class WorkerCacheServiceStatus(StrEnum):
    Healthy = "healthy"


class CacheDiskEvictionResult(ContractModel):
    scanned: int = 0
    evicted: int = 0
    freed_bytes: int = 0
    content_bytes: int = 0
    pressure: bool = False


@dataclass(frozen=True, slots=True)
class CacheDiskUsage:
    total: int
    used: int
    free: int


class CacheDiskUsageReader(Protocol):
    def __call__(self, path: Path) -> CacheDiskUsage: ...


def read_cache_disk_usage(path: Path) -> CacheDiskUsage:
    usage = shutil.disk_usage(path)
    return CacheDiskUsage(total=usage.total, used=usage.used, free=usage.free)


class CacheMetadataReconciliationResult(ContractModel):
    scanned: int = 0
    retained: int = 0
    removed_invalid: int = 0
    removed_orphaned: int = 0
    removed_excess: int = 0


class CacheWriteError(RuntimeError):
    """A cache write rejected before content becomes visible."""


class CacheObjectTooLargeError(CacheWriteError):
    pass


class CacheCapacityError(CacheWriteError):
    pass


class CacheUploadIncompleteError(CacheWriteError):
    pass


class CacheLengthRequiredError(CacheWriteError):
    pass


class CacheUnavailableError(RuntimeError):
    """The node-local cache could not answer; callers should use origin."""


@dataclass(slots=True)
class _CacheWriteReservation:
    size_bytes: int = 0
    released: bool = False


class WorkerCacheEndpoint(ContractModel):
    url: str


class WorkerCacheServiceHealth(ContractModel):
    status: WorkerCacheServiceStatus
    root: str
    content_count: int = 0
    content_bytes: int = 0

    @property
    def healthy(self) -> bool:
        return self.status is WorkerCacheServiceStatus.Healthy


@dataclass
class FileCacheServer:
    root: Path
    max_content_bytes: int = 20 * 1024 * 1024 * 1024
    max_object_bytes: int = 8 * 1024 * 1024 * 1024
    max_metadata_entries: int = 100_000
    max_cache_path_bytes: int = 4096
    max_read_bytes: int = 64 * 1024 * 1024
    disk_max_usage_pct: float = 0.90
    disk_evict_watermark_pct: float = 0.85
    recent_access_guard_seconds: int = 10 * 60
    access_touch_interval_seconds: int = 5 * 60
    max_access_touch_entries: int = 65_536
    disk_usage_reader: CacheDiskUsageReader = read_cache_disk_usage
    _mutation_lock: threading.RLock = field(default_factory=threading.RLock, init=False)
    _access_touches: dict[str, datetime] = field(default_factory=dict, init=False)
    _reserved_content_bytes: int = field(default=0, init=False)
    _metadata_count: int | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        if self.max_content_bytes <= 0 or self.max_object_bytes <= 0:
            msg = "cache content limits must be positive"
            raise ValueError(msg)
        if (
            self.max_metadata_entries <= 0
            or self.max_cache_path_bytes <= 0
            or self.max_read_bytes <= 0
            or self.max_access_touch_entries <= 0
        ):
            msg = "cache metadata and read limits must be positive"
            raise ValueError(msg)
        if not 0 < self.disk_evict_watermark_pct <= self.disk_max_usage_pct <= 1:
            msg = "cache disk watermarks must satisfy 0 < evict <= max <= 1"
            raise ValueError(msg)
        if self.recent_access_guard_seconds < 0 or self.access_touch_interval_seconds < 0:
            msg = "cache access retention intervals cannot be negative"
            raise ValueError(msg)

    def prepare(self) -> None:
        """Prepare a process-owned root before the HTTP server becomes reachable."""
        with self._mutation_lock:
            self.root.mkdir(parents=True, exist_ok=True)
            for path in (
                self._path("contents/.incoming").glob("*.tmp"),
                self._path("contents").glob(".*.tmp"),
                self._path("metadata/paths").glob(".*.tmp"),
            ):
                for temporary in path:
                    temporary.unlink(missing_ok=True)
            self._reconcile_metadata_locked()
            self.evict_for_pressure()

    def has_complete_content(
        self,
        content_hash: str,
        size_bytes: int = 0,
    ) -> CacheContentCompleteness:
        path = self._content_path(content_hash)
        if not path.is_file():
            return CacheContentCompleteness(
                status=CacheContentCompletenessStatus.Missing,
                content_hash=content_hash,
                expected_size_bytes=size_bytes,
                reason="content is not present",
            )
        actual_size = path.stat().st_size
        if size_bytes > 0 and actual_size != size_bytes:
            return CacheContentCompleteness(
                status=CacheContentCompletenessStatus.SizeMismatch,
                content_hash=content_hash,
                size_bytes=actual_size,
                expected_size_bytes=size_bytes,
                reason="content size does not match expected size",
            )
        return CacheContentCompleteness(
            status=CacheContentCompletenessStatus.Complete,
            content_hash=content_hash,
            size_bytes=actual_size,
            expected_size_bytes=size_bytes,
            reason="content is complete",
        )

    def read_content(self, request: CacheContentReadRequest) -> CacheContentReadResult:
        if request.length > self.max_read_bytes:
            return CacheContentReadResult(
                status=CacheContentReadStatus.Error,
                content_hash=request.content_hash,
                offset=request.offset,
                reason=f"cache read exceeds {self.max_read_bytes} bytes",
            )
        path = self._content_path(request.content_hash)
        if not path.is_file():
            return CacheContentReadResult(
                status=CacheContentReadStatus.Miss,
                content_hash=request.content_hash,
                offset=request.offset,
                reason="content is not present",
            )
        with path.open("rb") as handle:
            handle.seek(request.offset)
            data = handle.read(request.length)
        if data:
            self.touch_content_access(request.content_hash)
        status = (
            CacheContentReadStatus.Hit
            if len(data) == request.length
            else CacheContentReadStatus.ShortRead
        )
        return CacheContentReadResult(
            status=status,
            content_hash=request.content_hash,
            data=data,
            offset=request.offset,
            length=len(data),
            reason=(
                "content read"
                if status is CacheContentReadStatus.Hit
                else "content read returned fewer bytes than requested"
            ),
        )

    def store_content_bytes(
        self,
        data: bytes,
        *,
        expected_hash: str = "",
        cache_path: str = "",
    ) -> CacheContentStoreResult:
        return self.store_content_stream(
            _BytesReader(data),
            expected_hash=expected_hash,
            cache_path=cache_path,
            expected_size_bytes=len(data),
        )

    def store_content_from_local_file(
        self,
        path: str | Path,
        *,
        expected_hash: str = "",
        cache_path: str = "",
    ) -> CacheContentStoreResult:
        source_path = Path(path).expanduser().resolve()
        if not source_path.is_file():
            return CacheContentStoreResult(
                status=CacheContentStoreStatus.SourceMissing,
                content_hash=expected_hash,
                cache_path=cache_path,
                reason="source file is not present",
            )
        with source_path.open("rb") as source:
            return self.store_content_stream(
                source,
                expected_hash=expected_hash,
                cache_path=cache_path,
                expected_size_bytes=source_path.stat().st_size,
            )

    def store_content_stream(
        self,
        source: BinaryReadable,
        *,
        expected_hash: str = "",
        cache_path: str = "",
        chunk_size: int = 8 * 1024 * 1024,
        expected_size_bytes: int | None = None,
    ) -> CacheContentStoreResult:
        self._validate_cache_path(cache_path)
        if expected_hash:
            _normalize_content_hash(expected_hash)
        if expected_size_bytes is not None and expected_size_bytes < 0:
            msg = "cache upload content length cannot be negative"
            raise ValueError(msg)
        reservation = self._begin_write_reservation(expected_size_bytes or 0)
        incoming = self._path("contents/.incoming")
        incoming.mkdir(parents=True, exist_ok=True)
        temporary = incoming / f"{uuid4().hex}.tmp"
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            with temporary.open("wb") as target:
                while chunk := source.read(chunk_size):
                    if expected_size_bytes is None:
                        self._extend_write_reservation(reservation, len(chunk))
                    target.write(chunk)
                    digest.update(chunk)
                    size_bytes += len(chunk)
                target.flush()
                os.fsync(target.fileno())
            if expected_size_bytes is not None and size_bytes != expected_size_bytes:
                raise CacheUploadIncompleteError(
                    f"cache upload ended after {size_bytes} of {expected_size_bytes} bytes"
                )
            actual_hash = digest.hexdigest()
            normalized_expected = _normalize_content_hash(expected_hash) if expected_hash else ""
            if normalized_expected and normalized_expected != actual_hash:
                return CacheContentStoreResult(
                    status=CacheContentStoreStatus.HashMismatch,
                    content_hash=normalized_expected,
                    actual_hash=actual_hash,
                    cache_path=cache_path,
                    size_bytes=size_bytes,
                    reason="content hash does not match expected hash",
                )
            content_hash = normalized_expected or actual_hash
            destination = self._content_path(content_hash)
            with self._mutation_lock:
                if (
                    destination.is_file()
                    and destination.stat().st_size == size_bytes
                    and _sha256_file(destination) == actual_hash
                ):
                    self.touch_content_access(content_hash)
                    self._record_cache_path_metadata(cache_path, content_hash, size_bytes)
                    return CacheContentStoreResult(
                        status=CacheContentStoreStatus.AlreadyPresent,
                        content_hash=content_hash,
                        actual_hash=actual_hash,
                        cache_path=cache_path,
                        size_bytes=size_bytes,
                        reason="content is already present",
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary.replace(destination)
                self._record_cache_path_metadata(cache_path, content_hash, size_bytes)
            return CacheContentStoreResult(
                status=CacheContentStoreStatus.Stored,
                content_hash=content_hash,
                actual_hash=actual_hash,
                cache_path=cache_path,
                size_bytes=size_bytes,
                reason="content stored",
            )
        finally:
            temporary.unlink(missing_ok=True)
            self._release_write_reservation(reservation)

    def content_path(self, content_hash: str) -> Path:
        return self._content_path(content_hash)

    def content_metadata(self, cache_path: str) -> CacheContentMetadata | None:
        self._validate_cache_path(cache_path)
        metadata_path = self._cache_path_metadata_path(cache_path)
        if not metadata_path.is_file():
            return None
        try:
            metadata = CacheContentMetadata.model_validate_json(
                metadata_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            with self._mutation_lock:
                metadata_path.unlink(missing_ok=True)
                self._metadata_count = None
            return None
        if not self._content_path(metadata.content_hash).is_file():
            with self._mutation_lock:
                metadata_path.unlink(missing_ok=True)
                self._metadata_count = None
            return None
        os.utime(metadata_path, None)
        return metadata

    def reconcile_metadata(self) -> CacheMetadataReconciliationResult:
        with self._mutation_lock:
            return self._reconcile_metadata_locked()

    def touch_content_access(self, content_hash: str, *, now: datetime | None = None) -> None:
        normalized = _normalize_content_hash(content_hash)
        current = now or utc_now()
        with self._mutation_lock:
            prior = self._access_touches.get(normalized)
            if prior is not None and current - prior < timedelta(
                seconds=self.access_touch_interval_seconds
            ):
                return
            path = self._content_path(normalized)
            if not path.is_file():
                return
            os.utime(path, times=(current.timestamp(), current.timestamp()))
            self._access_touches[normalized] = current
            excess = len(self._access_touches) - self.max_access_touch_entries
            if excess > 0:
                for key, _value in sorted(
                    self._access_touches.items(), key=lambda item: (item[1], item[0])
                )[:excess]:
                    del self._access_touches[key]

    def evict_for_pressure(
        self,
        *,
        protected_hashes: set[str] | None = None,
        now: datetime | None = None,
    ) -> CacheDiskEvictionResult:
        current = now or utc_now()
        protected = {_normalize_content_hash(item) for item in protected_hashes or set()}
        with self._mutation_lock:
            self.root.mkdir(parents=True, exist_ok=True)
            candidates = self._eviction_candidates()
            content_bytes = sum(size for _hash, _path, _accessed, size in candidates)
            disk = self.disk_usage_reader(self.root)
            disk_pressure = disk.total > 0 and disk.used / disk.total > self.disk_max_usage_pct
            content_pressure = content_bytes > self.max_content_bytes
            if not disk_pressure and not content_pressure:
                return CacheDiskEvictionResult(scanned=len(candidates), content_bytes=content_bytes)
            disk_target = (
                max(disk.used - int(disk.total * self.disk_evict_watermark_pct), 0)
                if disk_pressure
                else 0
            )
            content_target = (
                max(content_bytes - int(self.max_content_bytes * 0.85), 0)
                if content_pressure
                else 0
            )
            bytes_to_free = max(disk_target, content_target)
            recent_cutoff = current - timedelta(seconds=self.recent_access_guard_seconds)
            freed = 0
            evicted: set[str] = set()
            for content_hash, path, accessed_at, size in sorted(
                candidates, key=lambda item: (item[2], item[0])
            ):
                if freed >= bytes_to_free:
                    break
                if content_hash in protected or accessed_at > recent_cutoff:
                    continue
                path.unlink(missing_ok=True)
                self._access_touches.pop(content_hash, None)
                evicted.add(content_hash)
                freed += size
            if evicted:
                self._delete_metadata_for_hashes(evicted)
            return CacheDiskEvictionResult(
                scanned=len(candidates),
                evicted=len(evicted),
                freed_bytes=freed,
                content_bytes=max(content_bytes - freed, 0),
                pressure=True,
            )

    def health(self) -> WorkerCacheServiceHealth:
        candidates = self._eviction_candidates()
        return WorkerCacheServiceHealth(
            status=WorkerCacheServiceStatus.Healthy,
            root=str(self.root),
            content_count=len(candidates),
            content_bytes=sum(item[3] for item in candidates),
        )

    @property
    def _effective_max_object_bytes(self) -> int:
        return min(self.max_object_bytes, self.max_content_bytes)

    def _path(self, relative_path: str) -> Path:
        root = self.root.resolve()
        target = (root / relative_path).resolve()
        if root != target and root not in target.parents:
            msg = f"cache path escapes root: {relative_path}"
            raise ValueError(msg)
        return target

    def _content_path(self, content_hash: str) -> Path:
        return self._path(f"contents/{_normalize_content_hash(content_hash)}")

    def _validate_cache_path(self, cache_path: str) -> None:
        if len(cache_path.encode("utf-8")) > self.max_cache_path_bytes:
            msg = f"cache path exceeds {self.max_cache_path_bytes} bytes"
            raise ValueError(msg)

    def _begin_write_reservation(self, size_bytes: int) -> _CacheWriteReservation:
        if size_bytes > self._effective_max_object_bytes:
            raise CacheObjectTooLargeError(
                f"cache object exceeds {self._effective_max_object_bytes} bytes"
            )
        reservation = _CacheWriteReservation()
        if size_bytes:
            self._extend_write_reservation(reservation, size_bytes)
        return reservation

    def _extend_write_reservation(
        self,
        reservation: _CacheWriteReservation,
        size_bytes: int,
    ) -> None:
        if reservation.released:
            msg = "cache write reservation is already released"
            raise RuntimeError(msg)
        if size_bytes < 0:
            msg = "cache write reservation cannot shrink"
            raise ValueError(msg)
        if reservation.size_bytes + size_bytes > self._effective_max_object_bytes:
            raise CacheObjectTooLargeError(
                f"cache object exceeds {self._effective_max_object_bytes} bytes"
            )
        with self._mutation_lock:
            self.root.mkdir(parents=True, exist_ok=True)
            self._evict_for_admission_locked(size_bytes, now=utc_now())
            reservation.size_bytes += size_bytes
            self._reserved_content_bytes += size_bytes

    def _release_write_reservation(self, reservation: _CacheWriteReservation) -> None:
        with self._mutation_lock:
            if reservation.released:
                return
            self._reserved_content_bytes = max(
                self._reserved_content_bytes - reservation.size_bytes, 0
            )
            reservation.released = True

    def _evict_for_admission_locked(self, incoming_bytes: int, *, now: datetime) -> None:
        candidates = self._eviction_candidates()
        content_bytes = sum(size for _hash, _path, _accessed, size in candidates)
        disk = self.disk_usage_reader(self.root)
        content_shortage = max(
            content_bytes + self._reserved_content_bytes + incoming_bytes - self.max_content_bytes,
            0,
        )
        disk_shortage = max(
            disk.used + incoming_bytes - int(disk.total * self.disk_max_usage_pct), 0
        )
        bytes_to_free = max(content_shortage, disk_shortage)
        if bytes_to_free <= 0:
            return
        recent_cutoff = now - timedelta(seconds=self.recent_access_guard_seconds)
        freed = 0
        evicted: set[str] = set()
        for content_hash, path, accessed_at, size in sorted(
            candidates, key=lambda item: (item[2], item[0])
        ):
            if accessed_at > recent_cutoff:
                continue
            path.unlink(missing_ok=True)
            self._access_touches.pop(content_hash, None)
            evicted.add(content_hash)
            freed += size
            if freed >= bytes_to_free:
                break
        if evicted:
            self._delete_metadata_for_hashes(evicted)
        if freed < bytes_to_free:
            raise CacheCapacityError(
                "cache capacity is exhausted and no eligible content can be evicted"
            )

    def _eviction_candidates(self) -> list[tuple[str, Path, datetime, int]]:
        contents = self._path("contents")
        if not contents.exists():
            return []
        candidates: list[tuple[str, Path, datetime, int]] = []
        for path in contents.iterdir():
            if not path.is_file() or path.name.startswith("."):
                continue
            stat = path.stat()
            candidates.append(
                (
                    path.name,
                    path,
                    datetime.fromtimestamp(stat.st_mtime, tz=utc_now().tzinfo),
                    stat.st_size,
                )
            )
        return candidates

    def _record_cache_path_metadata(
        self,
        cache_path: str,
        content_hash: str,
        size_bytes: int,
    ) -> None:
        if not cache_path:
            return
        metadata = CacheContentMetadata(
            content_hash=content_hash,
            size_bytes=size_bytes,
            cache_path=cache_path,
        )
        path = self._cache_path_metadata_path(cache_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if self._metadata_count is None:
            self._reconcile_metadata_locked()
        if not path.exists() and (self._metadata_count or 0) >= self.max_metadata_entries:
            self._remove_oldest_metadata_locked(1)
        existed = path.exists()
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(metadata.model_dump_json(), encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        if not existed:
            self._metadata_count = (self._metadata_count or 0) + 1

    def _reconcile_metadata_locked(self) -> CacheMetadataReconciliationResult:
        metadata_root = self._path("metadata/paths")
        if not metadata_root.exists():
            self._metadata_count = 0
            return CacheMetadataReconciliationResult()
        paths = [path for path in metadata_root.glob("*.json") if path.is_file()]
        invalid = 0
        orphaned = 0
        retained: list[Path] = []
        for path in paths:
            try:
                metadata = CacheContentMetadata.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                path.unlink(missing_ok=True)
                invalid += 1
                continue
            if not self._content_path(metadata.content_hash).is_file():
                path.unlink(missing_ok=True)
                orphaned += 1
                continue
            retained.append(path)
        excess = max(len(retained) - self.max_metadata_entries, 0)
        if excess:
            retained.sort(key=lambda item: (item.stat().st_mtime_ns, item.name))
            for path in retained[:excess]:
                path.unlink(missing_ok=True)
            retained = retained[excess:]
        self._metadata_count = len(retained)
        return CacheMetadataReconciliationResult(
            scanned=len(paths),
            retained=len(retained),
            removed_invalid=invalid,
            removed_orphaned=orphaned,
            removed_excess=excess,
        )

    def _delete_metadata_for_hashes(self, hashes: set[str]) -> None:
        metadata_root = self._path("metadata/paths")
        if not metadata_root.exists():
            return
        for path in metadata_root.glob("*.json"):
            try:
                metadata = CacheContentMetadata.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                path.unlink(missing_ok=True)
                continue
            if metadata.content_hash in hashes:
                path.unlink(missing_ok=True)
        self._metadata_count = None

    def _remove_oldest_metadata_locked(self, count: int) -> None:
        metadata_root = self._path("metadata/paths")
        paths = sorted(
            (path for path in metadata_root.glob("*.json") if path.is_file()),
            key=lambda item: (item.stat().st_mtime_ns, item.name),
        )
        for path in paths[:count]:
            path.unlink(missing_ok=True)
        self._metadata_count = max((self._metadata_count or len(paths)) - count, 0)

    def _cache_path_metadata_path(self, cache_path: str) -> Path:
        digest = hashlib.sha256(cache_path.encode()).hexdigest()
        return self._path(f"metadata/paths/{digest}.json")


@dataclass(slots=True)
class WorkerCacheHttpService:
    cache: FileCacheServer
    service_token: str
    previous_service_token: str = ""
    host: str = "127.0.0.1"
    port: int = 8090
    _server: ThreadingHTTPServer | None = None

    def __post_init__(self) -> None:
        if not self.service_token:
            msg = "cache service token is required"
            raise ValueError(msg)
        if self.previous_service_token == self.service_token:
            self.previous_service_token = ""

    @property
    def endpoint(self) -> WorkerCacheEndpoint:
        return WorkerCacheEndpoint(url=f"http://{self.host}:{self.port}")

    def health(self) -> WorkerCacheServiceHealth:
        return self.cache.health()

    def authorized(self, authorization: str | None) -> bool:
        supplied = ""
        if authorization is not None and authorization.startswith("Bearer "):
            supplied = authorization.removeprefix("Bearer ")
        current = hmac.compare_digest(supplied, self.service_token)
        previous = bool(self.previous_service_token) and hmac.compare_digest(
            supplied, self.previous_service_token
        )
        return bool(supplied) and (current or previous)

    def serve_forever(self) -> None:
        server = self._build_server()
        try:
            server.serve_forever()
        finally:
            server.server_close()
            self._server = None

    def start_in_thread(self) -> threading.Thread:
        server = self._build_server()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return thread

    def shutdown(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def _build_server(self) -> ThreadingHTTPServer:
        if self._server is not None:
            return self._server
        service = self

        class Handler(WorkerCacheHttpHandler):
            cache_service = service

        self.cache.prepare()
        server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._server = server
        actual_host, actual_port = server.server_address[:2]
        self.host = str(actual_host)
        self.port = int(actual_port)
        return server


class WorkerCacheHttpHandler(BaseHTTPRequestHandler):
    cache_service: WorkerCacheHttpService
    server_version = "LazyCloudCache/1.0"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if not self._authorize():
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            self._write_json(HTTPStatus.OK, self.cache_service.health())
        elif parsed.path == "/metadata":
            self._handle_metadata(parsed)
        elif parsed.path.startswith("/content/"):
            self._handle_content_get(parsed)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_HEAD(self) -> None:
        if not self._authorize():
            return
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.startswith("/content/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_hash = urllib.parse.unquote(parsed.path.removeprefix("/content/"))
        try:
            completeness = self.cache_service.cache.has_complete_content(content_hash)
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST, "invalid content hash")
            return
        if not completeness.complete:
            self.send_error(HTTPStatus.NOT_FOUND, "content is not present")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Length", "0")
        self.send_header("X-Content-Hash", completeness.content_hash)
        self.send_header("X-Content-Length", str(completeness.size_bytes))
        self.end_headers()

    def do_PUT(self) -> None:
        if not self._authorize():
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/content":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        query = urllib.parse.parse_qs(parsed.query)
        try:
            source, content_length = _request_body_reader(
                self.rfile,
                content_length_header=self.headers.get("Content-Length"),
                transfer_encoding=self.headers.get("Transfer-Encoding"),
                max_bytes=self.cache_service.cache._effective_max_object_bytes,
            )
            result = self.cache_service.cache.store_content_stream(
                source,
                expected_hash=_query_text(query, "expected_hash"),
                cache_path=_query_text(query, "cache_path"),
                expected_size_bytes=content_length,
            )
        except CacheObjectTooLargeError as exc:
            self._write_store_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, str(exc))
            return
        except CacheCapacityError as exc:
            self._write_store_error(HTTPStatus.INSUFFICIENT_STORAGE, str(exc))
            return
        except CacheLengthRequiredError as exc:
            self._write_store_error(HTTPStatus.LENGTH_REQUIRED, str(exc))
            return
        except (CacheUploadIncompleteError, ValueError) as exc:
            self._write_store_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        status = HTTPStatus.CREATED if result.stored else HTTPStatus.CONFLICT
        self._write_json(status, result)

    def _handle_metadata(self, parsed: urllib.parse.ParseResult) -> None:
        cache_path = _query_text(urllib.parse.parse_qs(parsed.query), "cache_path")
        if not cache_path:
            self.send_error(HTTPStatus.BAD_REQUEST, "cache_path is required")
            return
        metadata = self.cache_service.cache.content_metadata(cache_path)
        if metadata is None:
            self.send_error(HTTPStatus.NOT_FOUND, "cache metadata is not present")
            return
        self._write_json(HTTPStatus.OK, metadata)

    def _handle_content_get(self, parsed: urllib.parse.ParseResult) -> None:
        content_hash = urllib.parse.unquote(parsed.path.removeprefix("/content/"))
        try:
            path = self.cache_service.cache.content_path(content_hash)
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST, "invalid content hash")
            return
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "content is not present")
            return
        size = path.stat().st_size
        try:
            offset, length = _parse_range(self.headers.get("Range"), size)
        except ValueError as exc:
            self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, str(exc))
            return
        if length > self.cache_service.cache.max_read_bytes:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "cache range is too large")
            return
        status = HTTPStatus.PARTIAL_CONTENT if self.headers.get("Range") else HTTPStatus.OK
        self.send_response(status)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Hash", _normalize_content_hash(content_hash))
        self.send_header("X-Content-Size", str(size))
        if status is HTTPStatus.PARTIAL_CONTENT:
            end = offset + max(length - 1, 0)
            self.send_header("Content-Range", f"bytes {offset}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as source:
            source.seek(offset)
            remaining = length
            while remaining:
                chunk = source.read(min(DEFAULT_CONTENT_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        if length:
            self.cache_service.cache.touch_content_access(content_hash)

    def _authorize(self) -> bool:
        if self.cache_service.authorized(self.headers.get("Authorization")):
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", "Bearer")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _write_store_error(self, status: HTTPStatus, reason: str) -> None:
        self._write_json(
            status,
            CacheContentStoreResult(status=CacheContentStoreStatus.Error, reason=reason),
        )

    def _write_json(self, status: HTTPStatus, payload: ContractModel) -> None:
        body = payload.model_dump_json().encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@dataclass(slots=True)
class WorkerCacheHttpClient:
    endpoint: WorkerCacheEndpoint | str
    service_token: str
    connect_timeout_seconds: float = 0.5
    inactivity_timeout_seconds: float = 2.0

    def __post_init__(self) -> None:
        if not self.service_token:
            msg = "cache service token is required"
            raise ValueError(msg)
        if self.connect_timeout_seconds <= 0 or self.inactivity_timeout_seconds <= 0:
            msg = "cache HTTP timeouts must be positive"
            raise ValueError(msg)

    @property
    def base_url(self) -> str:
        value = (
            self.endpoint.url if isinstance(self.endpoint, WorkerCacheEndpoint) else self.endpoint
        )
        return value.rstrip("/")

    def health(self) -> WorkerCacheServiceHealth:
        status, _headers, body = self._request_bytes("GET", "/health")
        if status != HTTPStatus.OK:
            raise CacheUnavailableError(f"cache health returned HTTP {status}")
        return WorkerCacheServiceHealth.model_validate_json(body)

    def has_complete_content(
        self,
        content_hash: str,
        size_bytes: int = 0,
    ) -> CacheContentCompleteness:
        try:
            status, headers, _body = self._request_bytes(
                "HEAD", f"/content/{urllib.parse.quote(content_hash, safe=':')}"
            )
        except CacheUnavailableError as exc:
            return CacheContentCompleteness(
                status=CacheContentCompletenessStatus.Unavailable,
                content_hash=content_hash,
                expected_size_bytes=size_bytes,
                reason=str(exc),
            )
        if status == HTTPStatus.NOT_FOUND:
            return CacheContentCompleteness(
                status=CacheContentCompletenessStatus.Missing,
                content_hash=content_hash,
                expected_size_bytes=size_bytes,
                reason="content is not present",
            )
        if status != HTTPStatus.OK:
            return CacheContentCompleteness(
                status=CacheContentCompletenessStatus.Unavailable,
                content_hash=content_hash,
                expected_size_bytes=size_bytes,
                reason=f"cache HEAD returned HTTP {status}",
            )
        actual_size = int(headers.get("x-content-length", "0") or "0")
        if size_bytes > 0 and actual_size != size_bytes:
            return CacheContentCompleteness(
                status=CacheContentCompletenessStatus.SizeMismatch,
                content_hash=content_hash,
                size_bytes=actual_size,
                expected_size_bytes=size_bytes,
                reason="content size does not match expected size",
            )
        return CacheContentCompleteness(
            status=CacheContentCompletenessStatus.Complete,
            content_hash=content_hash,
            size_bytes=actual_size,
            expected_size_bytes=size_bytes,
            reason="content is complete",
        )

    def content_metadata(self, cache_path: str) -> CacheContentMetadata | None:
        query = urllib.parse.urlencode({"cache_path": cache_path})
        status, _headers, body = self._request_bytes("GET", f"/metadata?{query}")
        if status == HTTPStatus.NOT_FOUND:
            return None
        if status != HTTPStatus.OK:
            raise CacheUnavailableError(f"cache metadata returned HTTP {status}")
        return CacheContentMetadata.model_validate_json(body)

    def read_content(self, request: CacheContentReadRequest) -> CacheContentReadResult:
        path = f"/content/{urllib.parse.quote(request.content_hash, safe=':')}"
        end = request.offset + request.length - 1
        try:
            status, headers, data = self._request_bytes(
                "GET", path, headers={"Range": f"bytes={request.offset}-{end}"}
            )
        except CacheUnavailableError as exc:
            return CacheContentReadResult(
                status=CacheContentReadStatus.Unavailable,
                content_hash=request.content_hash,
                offset=request.offset,
                reason=str(exc),
            )
        if status == HTTPStatus.NOT_FOUND:
            return CacheContentReadResult(
                status=CacheContentReadStatus.Miss,
                content_hash=request.content_hash,
                offset=request.offset,
                reason="content is not present",
            )
        if status >= 500:
            return CacheContentReadResult(
                status=CacheContentReadStatus.Unavailable,
                content_hash=request.content_hash,
                offset=request.offset,
                reason=f"cache read returned HTTP {status}",
            )
        if status != HTTPStatus.PARTIAL_CONTENT:
            return CacheContentReadResult(
                status=CacheContentReadStatus.Error,
                content_hash=request.content_hash,
                offset=request.offset,
                reason=f"cache range read returned HTTP {status}",
            )
        response_hash = headers.get("x-content-hash", "")
        if response_hash != _normalize_content_hash(request.content_hash):
            return CacheContentReadResult(
                status=CacheContentReadStatus.Corrupt,
                content_hash=request.content_hash,
                data=data,
                offset=request.offset,
                length=len(data),
                reason="cache response content identity does not match the request",
            )
        if len(data) != request.length:
            return CacheContentReadResult(
                status=CacheContentReadStatus.ShortRead,
                content_hash=request.content_hash,
                data=data,
                offset=request.offset,
                length=len(data),
                reason="content read returned fewer bytes than requested",
            )
        total_size = int(headers.get("x-content-size", "0") or "0")
        if request.offset == 0 and total_size == request.length:
            actual_hash = hashlib.sha256(data).hexdigest()
            if actual_hash != _normalize_content_hash(request.content_hash):
                return CacheContentReadResult(
                    status=CacheContentReadStatus.Corrupt,
                    content_hash=request.content_hash,
                    data=data,
                    offset=request.offset,
                    length=len(data),
                    reason="cache response bytes do not match the content hash",
                )
        return CacheContentReadResult(
            status=CacheContentReadStatus.Hit,
            content_hash=request.content_hash,
            data=data,
            offset=request.offset,
            length=len(data),
            reason="content read",
        )

    def store_content_from_local_file(
        self,
        path: str | Path,
        *,
        expected_hash: str = "",
        cache_path: str = "",
    ) -> CacheContentStoreResult:
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            return CacheContentStoreResult(
                status=CacheContentStoreStatus.SourceMissing,
                content_hash=expected_hash,
                cache_path=cache_path,
                reason="source file is not present",
            )
        query = urllib.parse.urlencode({"expected_hash": expected_hash, "cache_path": cache_path})
        connection = self._connection()
        try:
            connection.putrequest("PUT", f"/content?{query}")
            connection.putheader("Authorization", f"Bearer {self.service_token}")
            connection.putheader("Content-Length", str(source.stat().st_size))
            connection.endheaders()
            with source.open("rb") as handle:
                while chunk := handle.read(DEFAULT_CONTENT_CHUNK_BYTES):
                    connection.send(chunk)
            response = connection.getresponse()
            self._set_read_timeout(connection)
            body = response.read()
            if response.status >= 500:
                return CacheContentStoreResult(
                    status=CacheContentStoreStatus.Unavailable,
                    content_hash=expected_hash,
                    cache_path=cache_path,
                    reason=f"cache store returned HTTP {response.status}",
                )
            return CacheContentStoreResult.model_validate_json(body)
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            return CacheContentStoreResult(
                status=CacheContentStoreStatus.Unavailable,
                content_hash=expected_hash,
                cache_path=cache_path,
                reason=str(exc),
            )
        finally:
            connection.close()

    def _request_bytes(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = self._connection()
        request_headers = {"Authorization": f"Bearer {self.service_token}"}
        request_headers.update(headers or {})
        try:
            connection.request(method, path, headers=request_headers)
            response = connection.getresponse()
            self._set_read_timeout(connection)
            body = response.read()
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            return response.status, response_headers, body
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise CacheUnavailableError(str(exc)) from exc
        finally:
            connection.close()

    def _connection(self) -> http.client.HTTPConnection:
        parsed = urllib.parse.urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            msg = "cache endpoint must be an absolute HTTP(S) URL"
            raise ValueError(msg)
        connection_type = (
            http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        )
        return connection_type(
            parsed.hostname,
            parsed.port,
            timeout=self.connect_timeout_seconds,
        )

    def _set_read_timeout(self, connection: http.client.HTTPConnection) -> None:
        if connection.sock is not None:
            connection.sock.settimeout(self.inactivity_timeout_seconds)


class _BytesReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def read(self, size: int | None = -1, /) -> bytes:
        if self.offset >= len(self.data):
            return b""
        end = len(self.data) if size is None or size < 0 else self.offset + size
        chunk = self.data[self.offset : end]
        self.offset += len(chunk)
        return chunk


class _ExactLengthReader:
    def __init__(self, source: BinaryReadable, remaining: int) -> None:
        self.source = source
        self.remaining = remaining

    def read(self, size: int | None = -1, /) -> bytes:
        if self.remaining <= 0:
            return b""
        read_size = self.remaining if size is None or size < 0 else min(size, self.remaining)
        data = self.source.read(read_size)
        if not data:
            raise CacheUploadIncompleteError(
                f"cache upload ended with {self.remaining} declared bytes missing"
            )
        self.remaining -= len(data)
        return data


class _ChunkedReader:
    def __init__(self, source: BinaryReadable, *, max_bytes: int) -> None:
        self.source = source
        self.max_bytes = max_bytes
        self.chunk_remaining = 0
        self.total_bytes = 0
        self.finished = False

    def read(self, size: int | None = -1, /) -> bytes:
        if self.finished:
            return b""
        if self.chunk_remaining == 0:
            self._read_chunk_header()
            if self.finished:
                return b""
        read_size = (
            self.chunk_remaining if size is None or size < 0 else min(size, self.chunk_remaining)
        )
        data = self.source.read(read_size)
        if not data:
            raise CacheUploadIncompleteError("cache chunk ended before its declared size")
        self.chunk_remaining -= len(data)
        self.total_bytes += len(data)
        if self.total_bytes > self.max_bytes:
            raise CacheObjectTooLargeError(f"cache object exceeds {self.max_bytes} bytes")
        if self.chunk_remaining == 0 and self.source.read(2) != b"\r\n":
            raise CacheUploadIncompleteError("cache chunk is missing its trailing delimiter")
        return data

    def _read_chunk_header(self) -> None:
        line = _read_http_line(self.source, max_bytes=128)
        try:
            chunk_size = int(line.split(b";", 1)[0].strip(), 16)
        except ValueError as exc:
            raise CacheUploadIncompleteError("cache chunk size is invalid") from exc
        if chunk_size < 0 or self.total_bytes + chunk_size > self.max_bytes:
            raise CacheObjectTooLargeError(f"cache object exceeds {self.max_bytes} bytes")
        if chunk_size == 0:
            trailer_bytes = 0
            while True:
                trailer = _read_http_line(self.source, max_bytes=4096)
                trailer_bytes += len(trailer)
                if trailer_bytes > 8192:
                    raise CacheUploadIncompleteError("cache chunk trailers are too large")
                if not trailer:
                    break
            self.finished = True
            return
        self.chunk_remaining = chunk_size


def _normalize_content_hash(content_hash: str) -> str:
    normalized = content_hash.strip().removeprefix("sha256:").lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        msg = "cache content hash must be a SHA-256 digest"
        raise ValueError(msg)
    return normalized


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _request_body_reader(
    source: BinaryReadable,
    *,
    content_length_header: str | None,
    transfer_encoding: str | None,
    max_bytes: int,
) -> tuple[BinaryReadable, int | None]:
    encoding = (transfer_encoding or "").strip().lower()
    if encoding and content_length_header is not None:
        msg = "cache upload cannot use both Content-Length and Transfer-Encoding"
        raise ValueError(msg)
    if encoding:
        if encoding != "chunked":
            msg = f"unsupported cache upload transfer encoding: {encoding}"
            raise ValueError(msg)
        return _ChunkedReader(source, max_bytes=max_bytes), None
    if content_length_header is None:
        raise CacheLengthRequiredError(
            "cache upload requires Content-Length or chunked transfer encoding"
        )
    if not content_length_header.isdigit():
        msg = "cache upload Content-Length is invalid"
        raise ValueError(msg)
    content_length = int(content_length_header)
    if content_length > max_bytes:
        raise CacheObjectTooLargeError(f"cache object exceeds {max_bytes} bytes")
    return _ExactLengthReader(source, content_length), content_length


def _read_http_line(source: BinaryReadable, *, max_bytes: int) -> bytes:
    line = bytearray()
    while len(line) <= max_bytes:
        char = source.read(1)
        if not char:
            raise CacheUploadIncompleteError("cache chunk framing ended unexpectedly")
        line.extend(char)
        if line.endswith(b"\r\n"):
            return bytes(line[:-2])
    raise CacheUploadIncompleteError("cache chunk framing line is too large")


def _parse_range(value: str | None, size: int) -> tuple[int, int]:
    if value is None:
        return 0, size
    if not value.startswith("bytes=") or "," in value:
        msg = "cache supports one explicit byte range"
        raise ValueError(msg)
    start_text, separator, end_text = value.removeprefix("bytes=").partition("-")
    if not separator or not start_text.isdigit() or (end_text and not end_text.isdigit()):
        msg = "cache byte range is invalid"
        raise ValueError(msg)
    start = int(start_text)
    end = int(end_text) if end_text else size - 1
    if start < 0 or start >= size or end < start:
        msg = "cache byte range is outside content"
        raise ValueError(msg)
    bounded_end = min(end, size - 1)
    return start, bounded_end - start + 1


def _query_text(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key, [])
    return values[0] if values else ""
