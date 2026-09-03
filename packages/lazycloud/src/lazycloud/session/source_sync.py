from __future__ import annotations

import fnmatch
import hashlib
import os
import posixpath
import threading
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from tempfile import SpooledTemporaryFile
from types import TracebackType
from typing import Protocol

from shared.app_identity import SOURCE_PACKAGE_BUCKET
from typing_extensions import Self

from lazycloud.json_contracts import validate_json_object
from lazycloud.terminal import ProgressCallback, humanize_bytes

SOURCE_PACKAGE_PREFIX = "sources"
SOURCE_PACKAGE_CONTENT_TYPE = "application/zip"
SOURCE_IGNORE_FILE = ".lazycloudignore"
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

DEFAULT_IGNORE_PATTERNS: tuple[str, ...] = (
    SOURCE_IGNORE_FILE,
    ".git",
    ".idea",
    ".python-version",
    ".vscode",
    ".venv",
    "venv",
    ".lazycloud/",
    "__pycache__",
    ".DS_Store",
    ".config",
    ".coverage",
    ".pytest_cache",
    ".ruff_cache",
    ".dockerignore",
    ".ipynb_checkpoints",
    ".env.local",
    ".envrc",
    "**/__pycache__/",
    "**/.pytest_cache/",
    "**/node_modules/",
    "**/.venv/",
    "**/playwright-report/",
    "**/test-results/",
    "*.pyc",
    ".next/",
    ".circleci",
)


class SourcePackageUploadClient(Protocol):
    def upload_bytes(
        self,
        data: bytes,
        *,
        name: str,
        bucket: str = "default",
        overwrite: bool = False,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        progress: ProgressCallback | None = None,
    ) -> object: ...


class SourceSyncStep(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    def update(self, summary: str) -> None: ...

    def progress(self, completed: int, total: int) -> None: ...

    def done(self, summary: str = "") -> None: ...


class SourceSyncTerminal(Protocol):
    def step(self, name: str, summary: str = "") -> SourceSyncStep: ...


class SourcePackageSyncError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SourcePackageArchive:
    data: bytes
    sha256: str
    size: int
    files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourcePackageSyncResult:
    object_id: str
    sha256: str
    size: int
    files: tuple[str, ...]


@dataclass(slots=True)
class SourcePackageSyncCache:
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _objects: dict[tuple[str, str], SourcePackageSyncResult] = field(default_factory=dict)

    def get(self, root: Path, digest: str) -> SourcePackageSyncResult | None:
        key = (str(root), digest)
        with self._lock:
            return self._objects.get(key)

    def put(self, root: Path, result: SourcePackageSyncResult) -> None:
        key = (str(root), result.sha256)
        with self._lock:
            self._objects[key] = result


_default_cache = SourcePackageSyncCache()


@dataclass(slots=True)
class SourcePackageSyncer:
    object_client: SourcePackageUploadClient
    root_dir: str | Path = "."
    archive_prefix: tuple[str, ...] = ()
    cache: SourcePackageSyncCache = field(default_factory=lambda: _default_cache)
    terminal: SourceSyncTerminal | None = None

    def sync(
        self,
        *,
        ignore_patterns: Sequence[str] | None = None,
        include_patterns: Sequence[str] | None = None,
        cache_object_id: bool = True,
    ) -> SourcePackageSyncResult:
        root = Path(self.root_dir).expanduser().resolve()
        if not root.exists():
            msg = f"source root does not exist: {root}"
            raise SourcePackageSyncError(msg)
        if not root.is_dir():
            msg = f"source root is not a directory: {root}"
            raise SourcePackageSyncError(msg)

        archive = build_source_package_archive(
            root,
            archive_prefix=self.archive_prefix,
            ignore_patterns=ignore_patterns,
            include_patterns=include_patterns,
        )
        plural = "s" if len(archive.files) != 1 else ""
        description = f"{len(archive.files)} file{plural}, {humanize_bytes(archive.size)}"
        with self._step("Source", description) as step:
            cached = self.cache.get(root, archive.sha256) if cache_object_id else None
            if cached is not None:
                step.done(f"{description} · cached")
                return cached
            object_name = f"{SOURCE_PACKAGE_PREFIX}/{archive.sha256}.zip"
            uploaded = _upload_source_package(
                self.object_client,
                archive,
                object_name=object_name,
                progress=lambda completed: step.progress(completed, archive.size),
            )
            object_id = _uploaded_object_id(uploaded)
            if not object_id:
                msg = "source package upload did not return an object_id"
                raise SourcePackageSyncError(msg)
            result = SourcePackageSyncResult(
                object_id=object_id,
                sha256=archive.sha256,
                size=archive.size,
                files=archive.files,
            )
            step.done(description)
        if cache_object_id:
            self.cache.put(root, result)
        return result

    def _step(self, name: str, summary: str) -> SourceSyncStep:
        if self.terminal is None:
            return _SilentStep()
        return self.terminal.step(name, summary)


class _SilentStep:
    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def update(self, summary: str) -> None:
        return None

    def progress(self, completed: int, total: int) -> None:
        return None

    def done(self, summary: str = "") -> None:
        return None


def _upload_source_package(
    client: SourcePackageUploadClient,
    archive: SourcePackageArchive,
    *,
    object_name: str,
    progress: ProgressCallback | None,
) -> object:
    metadata = {
        "kind": "source-package",
        "sha256": archive.sha256,
        "file_count": str(len(archive.files)),
    }
    return client.upload_bytes(
        archive.data,
        name=object_name,
        bucket=SOURCE_PACKAGE_BUCKET,
        overwrite=False,
        content_type=SOURCE_PACKAGE_CONTENT_TYPE,
        metadata=metadata,
        progress=progress,
    )


def build_source_package_archive(
    root: Path,
    *,
    archive_prefix: Sequence[str] = (),
    ignore_patterns: Sequence[str] | None = None,
    include_patterns: Sequence[str] | None = None,
) -> SourcePackageArchive:
    selected_archive_prefix = _validate_archive_prefix(archive_prefix)
    selected_ignore_patterns = tuple(ignore_patterns or _ignore_patterns_from_file(root))
    selected_include_patterns = tuple(include_patterns or ())
    files = tuple(
        _collect_source_files(
            root,
            ignore_patterns=selected_ignore_patterns,
            include_patterns=selected_include_patterns,
        )
    )
    archived_files = tuple(
        _archive_path(selected_archive_prefix, _relative_posix(root, item)) for item in files
    )
    data = _zip_files(root, files, archived_files)
    digest = hashlib.sha256(data).hexdigest()
    return SourcePackageArchive(
        data=data,
        sha256=digest,
        size=len(data),
        files=archived_files,
    )


def collect_source_files(
    root: Path,
    *,
    ignore_patterns: Sequence[str] | None = None,
    include_patterns: Sequence[str] | None = None,
) -> tuple[Path, ...]:
    root = root.expanduser().resolve()
    selected_ignore_patterns = tuple(ignore_patterns or _ignore_patterns_from_file(root))
    selected_include_patterns = tuple(include_patterns or ())
    return tuple(
        _collect_source_files(
            root,
            ignore_patterns=selected_ignore_patterns,
            include_patterns=selected_include_patterns,
        )
    )


def _ignore_patterns_from_file(root: Path) -> tuple[str, ...]:
    ignore_file = root / SOURCE_IGNORE_FILE
    if not ignore_file.is_file():
        return DEFAULT_IGNORE_PATTERNS
    return tuple(
        line
        for line in (raw.strip() for raw in ignore_file.read_text(encoding="utf-8").splitlines())
        if line and not line.startswith("#")
    )


def _collect_source_files(
    root: Path,
    *,
    ignore_patterns: Sequence[str],
    include_patterns: Sequence[str],
) -> Iterable[Path]:
    if _matches_all(ignore_patterns):
        return
    for current_root, dirs, files in os.walk(root):
        current = Path(current_root)
        dirs[:] = [
            dirname
            for dirname in sorted(dirs)
            if not _matches_patterns(
                _relative_posix(root, current / dirname),
                ignore_patterns,
                directory=True,
            )
        ]
        for filename in sorted(files):
            path = current / filename
            relative = _relative_posix(root, path)
            if _matches_patterns(relative, ignore_patterns, directory=False):
                continue
            if include_patterns and not _matches_patterns(
                relative,
                include_patterns,
                directory=False,
            ):
                continue
            yield path


def _zip_files(root: Path, files: Sequence[Path], archived_files: Sequence[str]) -> bytes:
    with SpooledTemporaryFile(max_size=16 * 1024 * 1024) as handle:
        with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file, archived_file in zip(files, archived_files, strict=True):
                info = zipfile.ZipInfo(archived_file, ZIP_EPOCH)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, file.read_bytes())
        handle.seek(0)
        return handle.read()


def _validate_archive_prefix(parts: Sequence[str]) -> tuple[str, ...]:
    selected = tuple(parts)
    if any(not part or not part.isidentifier() for part in selected):
        raise SourcePackageSyncError("source archive prefix must contain importable module names")
    return selected


def _archive_path(prefix: tuple[str, ...], relative_path: str) -> str:
    path = PurePosixPath(*prefix, relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise SourcePackageSyncError("source archive path escapes its package root")
    return path.as_posix()


def _matches_all(patterns: Sequence[str]) -> bool:
    return any(pattern.strip() in {"*", "**", "**/*"} for pattern in patterns)


def _matches_patterns(relative_path: str, patterns: Sequence[str], *, directory: bool) -> bool:
    normalized = _normalize_relative_path(relative_path)
    parts = PurePosixPath(normalized).parts
    for raw_pattern in patterns:
        pattern = _normalize_pattern(raw_pattern)
        if not pattern:
            continue
        directory_pattern = pattern.endswith("/")
        candidate = pattern.rstrip("/")
        if not candidate:
            continue
        if "/" not in candidate:
            if any(fnmatch.fnmatchcase(part, candidate) for part in parts):
                return True
            if directory and fnmatch.fnmatchcase(posixpath.basename(normalized), candidate):
                return True
            continue
        if fnmatch.fnmatchcase(normalized, candidate):
            return True
        if directory_pattern and (
            normalized == candidate or normalized.startswith(candidate + "/")
        ):
            return True
        if not any(character in candidate for character in "*?[") and (
            normalized == candidate or normalized.startswith(candidate + "/")
        ):
            return True
    return False


def _normalize_pattern(pattern: str) -> str:
    value = pattern.strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value.lstrip("/")


def _normalize_relative_path(path: str) -> str:
    value = path.replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value.strip("/")


def _relative_posix(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _uploaded_object_id(uploaded: object) -> str:
    object_id = getattr(uploaded, "object_id", "")
    if object_id:
        return str(object_id)
    try:
        payload = validate_json_object(uploaded)
    except ValueError:
        return ""
    object_id = payload.get("object_id")
    if isinstance(object_id, str):
        return object_id
    return ""


__all__ = [
    "SOURCE_IGNORE_FILE",
    "SOURCE_PACKAGE_BUCKET",
    "SOURCE_PACKAGE_CONTENT_TYPE",
    "SOURCE_PACKAGE_PREFIX",
    "SourcePackageArchive",
    "SourcePackageSyncCache",
    "SourcePackageSyncError",
    "SourcePackageSyncResult",
    "SourcePackageSyncer",
    "SourcePackageUploadClient",
    "build_source_package_archive",
    "collect_source_files",
]
