from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import threading
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from networking.internal_http import InternalHttpClient
from shared.app_identity import SOURCE_CACHE_DIR
from shared.container_requests import WORKER_USER_CODE_VOLUME, RequestMount
from shared.contracts import ContractModel

from worker.events import ContainerRequestContext
from worker.execution import stub_code_cache_key

SOURCE_CACHE_READY_FILE = ".source-cache-ready"
SOURCE_WORKSPACE_OWNER_FILE = ".source-workspace-owner"
WORKSPACE_READY_FILE = ".workspace-ready"
DEFAULT_SOURCE_CACHE_MAX_BYTES = 1024 * 1024 * 1024
DEFAULT_SOURCE_CACHE_MAX_ENTRIES = 32
DEFAULT_SOURCE_CACHE_ROOT = Path("/var/lib/lazycloud/source-cache")


class SourceCodeMaterializationError(RuntimeError):
    pass


class SourceCodeMaterializationResult(ContractModel):
    workspace_path: str
    cache_path: str
    object_id: str
    bytes_read: int = 0
    cache_hit: bool = False


class SourceCachePurgeResult(ContractModel):
    workspace_id: str
    removed_paths: list[str]


class SourceWorkspaceCleanupResult(ContractModel):
    container_id: str
    removed_paths: list[str]
    freed_bytes: int = 0


class SourceTemporaryPathPruneResult(ContractModel):
    active_container_count: int = 0
    removed_paths: list[str]
    freed_bytes: int = 0


class SourceWorkspaceLifecycle(Protocol):
    def cleanup_container(self, container_id: str) -> SourceWorkspaceCleanupResult: ...

    def prune_abandoned_temporary_paths(
        self,
        active_container_ids: set[str],
    ) -> SourceTemporaryPathPruneResult: ...


@dataclass(slots=True)
class SourceCodePackageMaterializer:
    cache_root: Path = field(default_factory=lambda: Path(tempfile.gettempdir()) / SOURCE_CACHE_DIR)
    workspace_root: Path = field(default_factory=lambda: Path(tempfile.gettempdir()))
    cache_max_bytes: int = DEFAULT_SOURCE_CACHE_MAX_BYTES
    cache_max_entries: int = DEFAULT_SOURCE_CACHE_MAX_ENTRIES
    http: InternalHttpClient = field(default_factory=InternalHttpClient)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.cache_root = self.cache_root.expanduser().resolve()
        self.workspace_root = self.workspace_root.expanduser().resolve()
        if self.cache_max_bytes <= 0:
            raise ValueError("source cache max bytes must be positive")
        if self.cache_max_entries <= 0:
            raise ValueError("source cache max entries must be positive")

    def materialize(
        self,
        request: ContainerRequestContext,
        mount: RequestMount,
    ) -> SourceCodeMaterializationResult:
        if mount.mount_path != WORKER_USER_CODE_VOLUME or not mount.source_object_id:
            msg = "request mount is not a source-code mount"
            raise SourceCodeMaterializationError(msg)

        workspace_path = self._container_workspace_path(request.container_id)
        workspace_ready_path = workspace_path.parent / WORKSPACE_READY_FILE
        cache_path = self._cache_path(request, mount)
        cache_ready_path = cache_path / SOURCE_CACHE_READY_FILE

        with self._lock:
            if workspace_path.exists() and workspace_ready_path.exists():
                return SourceCodeMaterializationResult(
                    workspace_path=str(workspace_path),
                    cache_path=str(cache_path),
                    object_id=mount.source_object_id,
                    cache_hit=cache_ready_path.exists(),
                )

            workspace_path.parent.mkdir(parents=True, exist_ok=True)
            (workspace_path.parent / SOURCE_WORKSPACE_OWNER_FILE).write_text(
                request.container_id,
                encoding="utf-8",
            )
            bytes_read = self._ensure_cache(request, mount, cache_path, cache_ready_path)
            _copy_directory_contents_atomic(
                cache_path,
                workspace_path,
                workspace_ready_path,
                request.container_id,
            )
            _touch_cache_entry(cache_path)
            self._enforce_cache_limits()
            return SourceCodeMaterializationResult(
                workspace_path=str(workspace_path),
                cache_path=str(cache_path),
                object_id=mount.source_object_id,
                bytes_read=bytes_read,
                cache_hit=bytes_read == 0,
            )

    def purge(
        self,
        workspace_id: str,
        source_object_ids: list[str],
    ) -> SourceCachePurgeResult:
        normalized_workspace_id = workspace_id.strip()
        if not normalized_workspace_id:
            raise SourceCodeMaterializationError("workspace id is required")
        removed_paths: list[str] = []
        with self._lock:
            for object_id in dict.fromkeys(source_object_ids):
                normalized_object_id = object_id.strip()
                if not normalized_object_id:
                    continue
                cache_path = self.cache_root / stub_code_cache_key(
                    normalized_workspace_id,
                    normalized_object_id,
                )
                if cache_path.exists():
                    shutil.rmtree(cache_path)
                    removed_paths.append(str(cache_path))
        return SourceCachePurgeResult(
            workspace_id=normalized_workspace_id,
            removed_paths=removed_paths,
        )

    def cleanup_container(self, container_id: str) -> SourceWorkspaceCleanupResult:
        normalized_container_id = _validate_container_id(container_id)
        removed_paths: list[str] = []
        freed_bytes = 0
        with self._lock:
            container_root = self.workspace_root / normalized_container_id
            freed_bytes += _remove_owned_path(container_root, removed_paths)
            if self.cache_root.exists():
                suffix = f".tmp.{normalized_container_id}"
                for path in sorted(self.cache_root.iterdir(), key=lambda item: item.name):
                    if path.name.endswith(suffix):
                        freed_bytes += _remove_owned_path(path, removed_paths)
        return SourceWorkspaceCleanupResult(
            container_id=normalized_container_id,
            removed_paths=removed_paths,
            freed_bytes=freed_bytes,
        )

    def prune_abandoned_temporary_paths(
        self,
        active_container_ids: set[str],
    ) -> SourceTemporaryPathPruneResult:
        active = {_validate_container_id(container_id) for container_id in active_container_ids}
        removed_paths: list[str] = []
        freed_bytes = 0
        with self._lock:
            if self.workspace_root.exists():
                for path in sorted(self.workspace_root.iterdir(), key=lambda item: item.name):
                    if path.name in active or not _is_source_workspace_root(path):
                        continue
                    freed_bytes += _remove_owned_path(path, removed_paths)
            if self.cache_root.exists():
                for path in sorted(self.cache_root.iterdir(), key=lambda item: item.name):
                    owner = _temporary_cache_owner(path.name)
                    if owner is None or owner in active:
                        continue
                    freed_bytes += _remove_owned_path(path, removed_paths)
            self._enforce_cache_limits()
        return SourceTemporaryPathPruneResult(
            active_container_count=len(active),
            removed_paths=removed_paths,
            freed_bytes=freed_bytes,
        )

    def _cache_path(self, request: ContainerRequestContext, mount: RequestMount) -> Path:
        if not request.workspace_id:
            raise SourceCodeMaterializationError("workspace id is required")
        return self.cache_root / stub_code_cache_key(request.workspace_id, mount.source_object_id)

    def _container_workspace_path(self, container_id: str) -> Path:
        return self.workspace_root / _validate_container_id(container_id) / "workspace"

    def _ensure_cache(
        self,
        request: ContainerRequestContext,
        mount: RequestMount,
        cache_path: Path,
        ready_path: Path,
    ) -> int:
        if ready_path.exists():
            return 0
        data = self._read_source_bytes(mount)
        _verify_source_hash(data, mount.source_sha256)
        tmp_path = cache_path.with_name(f"{cache_path.name}.tmp.{request.container_id}")
        shutil.rmtree(tmp_path, ignore_errors=True)
        _extract_zip_bytes(data, tmp_path)
        (tmp_path / SOURCE_CACHE_READY_FILE).write_text("ok", encoding="utf-8")
        shutil.rmtree(cache_path, ignore_errors=True)
        tmp_path.replace(cache_path)
        return len(data)

    def _enforce_cache_limits(self) -> None:
        candidates = _source_cache_candidates(self.cache_root)
        total_bytes = sum(size_bytes for _, size_bytes in candidates)
        while candidates and (
            len(candidates) > self.cache_max_entries or total_bytes > self.cache_max_bytes
        ):
            path, size_bytes = candidates.pop(0)
            _remove_path(path)
            total_bytes -= size_bytes

    def _read_source_bytes(self, mount: RequestMount) -> bytes:
        if mount.source_download_url:
            parsed = urlparse(mount.source_download_url)
            if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
                msg = "source download URL must be an HTTP(S) URL with a hostname"
                raise SourceCodeMaterializationError(msg)
            try:
                response = self.http.request(
                    "GET",
                    mount.source_download_url,
                    timeout_seconds=60,
                )
                if response.status_code < 200 or response.status_code >= 300:
                    msg = f"source download returned HTTP {response.status_code}"
                    raise SourceCodeMaterializationError(msg)
                return response.content
            except SourceCodeMaterializationError:
                raise
            except Exception as exc:
                raise SourceCodeMaterializationError(
                    f"source download failed: {type(exc).__name__}"
                ) from None
        if mount.local_path:
            path = Path(mount.local_path)
            if path.is_file():
                return path.read_bytes()
        msg = f"source object is unavailable for mount {mount.mount_path}"
        raise SourceCodeMaterializationError(msg)


def _validate_container_id(container_id: str) -> str:
    normalized = container_id.strip()
    if (
        not normalized
        or normalized in {".", ".."}
        or Path(normalized).name != normalized
        or "/" in normalized
        or "\\" in normalized
    ):
        raise SourceCodeMaterializationError("container id must be a single path segment")
    return normalized


def _source_cache_candidates(root: Path) -> list[tuple[Path, int]]:
    if not root.exists():
        return []
    candidates: list[tuple[Path, int, int]] = []
    for path in root.iterdir():
        if not path.is_dir() or not (path / SOURCE_CACHE_READY_FILE).is_file():
            continue
        candidates.append((path, _path_size(path), path.stat().st_mtime_ns))
    candidates.sort(key=lambda item: (item[2], item[0].name))
    return [(path, size_bytes) for path, size_bytes, _ in candidates]


def _touch_cache_entry(path: Path) -> None:
    os.utime(path)


def _temporary_cache_owner(name: str) -> str | None:
    _, separator, owner = name.rpartition(".tmp.")
    if not separator or not owner:
        return None
    try:
        return _validate_container_id(owner)
    except SourceCodeMaterializationError:
        return None


def _is_source_workspace_root(path: Path) -> bool:
    if not path.is_dir():
        return False
    container_id = path.name
    try:
        _validate_container_id(container_id)
    except SourceCodeMaterializationError:
        return False
    return (
        (path / SOURCE_WORKSPACE_OWNER_FILE).is_file()
        or (path / WORKSPACE_READY_FILE).exists()
        or (path / f"workspace.tmp.{container_id}").exists()
    )


def _remove_owned_path(path: Path, removed_paths: list[str]) -> int:
    if not path.exists() and not path.is_symlink():
        return 0
    size_bytes = _path_size(path)
    _remove_path(path)
    removed_paths.append(str(path))
    return size_bytes


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _path_size(path: Path) -> int:
    if path.is_symlink() or path.is_file():
        return path.lstat().st_size
    if not path.exists():
        return 0
    return sum(item.lstat().st_size for item in path.rglob("*") if not item.is_dir())


def _verify_source_hash(data: bytes, expected_sha256: str) -> None:
    if not expected_sha256:
        return
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha256:
        msg = f"source package hash mismatch: expected {expected_sha256}, got {actual}"
        raise SourceCodeMaterializationError(msg)


def _extract_zip_bytes(data: bytes, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    archive_path = destination.with_suffix(".zip")
    archive_path.write_bytes(data)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                target = _safe_extract_target(destination, member.filename)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    except zipfile.BadZipFile as exc:
        msg = "source package is not a valid zip archive"
        raise SourceCodeMaterializationError(msg) from exc
    finally:
        archive_path.unlink(missing_ok=True)


def _safe_extract_target(root: Path, member_name: str) -> Path:
    target = (root / member_name).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        msg = f"source package path escapes workspace: {member_name}"
        raise SourceCodeMaterializationError(msg)
    return target


def _copy_directory_contents_atomic(
    source: Path,
    destination: Path,
    ready_path: Path,
    container_id: str,
) -> None:
    tmp_path = destination.with_name(f"{destination.name}.tmp.{container_id}")
    shutil.rmtree(tmp_path, ignore_errors=True)
    shutil.rmtree(destination, ignore_errors=True)
    ready_path.unlink(missing_ok=True)
    tmp_path.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        if child.name == SOURCE_CACHE_READY_FILE:
            continue
        target = tmp_path / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)
    os.replace(tmp_path, destination)
    ready_path.write_text("ok", encoding="utf-8")


__all__ = [
    "DEFAULT_SOURCE_CACHE_MAX_BYTES",
    "DEFAULT_SOURCE_CACHE_MAX_ENTRIES",
    "SOURCE_CACHE_READY_FILE",
    "SOURCE_WORKSPACE_OWNER_FILE",
    "SourceCachePurgeResult",
    "SourceCodeMaterializationError",
    "SourceCodeMaterializationResult",
    "SourceCodePackageMaterializer",
    "SourceTemporaryPathPruneResult",
    "SourceWorkspaceCleanupResult",
    "SourceWorkspaceLifecycle",
]
