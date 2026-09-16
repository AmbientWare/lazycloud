from __future__ import annotations

import fnmatch
import hashlib
import os
import posixpath
import time
import zipfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
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
ARCHIVE_CHUNK_SIZE = 1024 * 1024

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
    ".env",
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
class SourcePackageSyncer:
    object_client: SourcePackageUploadClient
    root_dir: str | Path = "."
    archive_prefix: tuple[str, ...] = ()
    terminal: SourceSyncTerminal | None = None

    def sync(
        self,
        *,
        ignore_patterns: Sequence[str] | None = None,
        include_patterns: Sequence[str] | None = None,
    ) -> SourcePackageSyncResult:
        root = Path(self.root_dir).expanduser().resolve()
        if not root.exists():
            msg = f"source root does not exist: {root}"
            raise SourcePackageSyncError(msg)
        if not root.is_dir():
            msg = f"source root is not a directory: {root}"
            raise SourcePackageSyncError(msg)

        with self._step("Source", "collecting files") as step:
            archive = build_source_package_archive(
                root,
                archive_prefix=self.archive_prefix,
                ignore_patterns=ignore_patterns,
                include_patterns=include_patterns,
                progress=step.update,
            )
            plural = "s" if len(archive.files) != 1 else ""
            description = f"{len(archive.files):,} file{plural}, {humanize_bytes(archive.size)}"
            step.update(f"syncing {description}")
            object_name = f"{SOURCE_PACKAGE_PREFIX}/{archive.sha256}.zip"
            uploaded = _upload_source_package(
                self.object_client,
                archive,
                object_name=object_name,
                progress=lambda completed: step.update(
                    f"syncing {min(100, completed * 100 // archive.size):3}% · {description}"
                ),
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
    progress: Callable[[str], None] | None = None,
) -> SourcePackageArchive:
    reporter = _ArchiveProgress(progress)
    reporter.update("collecting files", force=True)
    selected_archive_prefix = _validate_archive_prefix(archive_prefix)
    selected_ignore_patterns = tuple(ignore_patterns or _ignore_patterns_from_file(root))
    selected_include_patterns = tuple(include_patterns or ())
    files: list[Path] = []
    total_bytes = 0
    for path in _collect_source_files(
        root,
        ignore_patterns=selected_ignore_patterns,
        include_patterns=selected_include_patterns,
        progress=reporter,
    ):
        files.append(path)
        total_bytes += path.stat().st_size
    archived_files = tuple(
        _archive_path(selected_archive_prefix, _relative_posix(root, item)) for item in files
    )
    data = _zip_files(files, archived_files, total_bytes=total_bytes, progress=reporter)
    digest = hashlib.sha256()
    reporter.update(f"hashing   0% · {humanize_bytes(len(data))}", force=True)
    view = memoryview(data)
    for offset in range(0, len(data), ARCHIVE_CHUNK_SIZE):
        chunk = view[offset : offset + ARCHIVE_CHUNK_SIZE]
        digest.update(chunk)
        reporter.update(
            f"hashing {(offset + len(chunk)) * 100 // len(data):3}% · {humanize_bytes(len(data))}"
        )
    reporter.update(f"hashing 100% · {humanize_bytes(len(data))}", force=True)
    return SourcePackageArchive(
        data=data,
        sha256=digest.hexdigest(),
        size=len(data),
        files=archived_files,
    )


@dataclass(slots=True)
class _ArchiveProgress:
    callback: Callable[[str], None] | None
    last_update: float = 0

    def update(self, summary: str, *, force: bool = False) -> None:
        if self.callback is None:
            return
        now = time.monotonic()
        if force or now - self.last_update >= 0.25:
            self.callback(summary)
            self.last_update = now

    def collection(self, files: int, folders: int) -> None:
        file_label = "file" if files == 1 else "files"
        folder_label = "folder" if folders == 1 else "folders"
        self.update(f"collecting · {files:,} {file_label}, {folders:,} {folder_label}")

    def compression(
        self,
        completed_files: int,
        total_files: int,
        completed_bytes: int,
        total_bytes: int,
        *,
        force: bool = False,
    ) -> None:
        percent = (
            min(100, completed_bytes * 100 // total_bytes)
            if total_bytes
            else (100 if completed_files == total_files else 0)
        )
        file_label = "file" if total_files == 1 else "files"
        self.update(
            f"compressing {percent:3}% · {completed_files:,}/{total_files:,} {file_label} · "
            f"{humanize_bytes(completed_bytes)}/{humanize_bytes(total_bytes)}",
            force=force,
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
    progress: _ArchiveProgress | None = None,
) -> Iterable[Path]:
    file_count = 0
    directory_count = 0
    if _matches_all(ignore_patterns):
        return
    for current_root, dirs, files in os.walk(root):
        directory_count += 1
        if progress is not None:
            progress.collection(file_count, directory_count)
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
            file_count += 1
            if progress is not None:
                progress.collection(file_count, directory_count)
            yield path


def _zip_files(
    files: Sequence[Path],
    archived_files: Sequence[str],
    *,
    total_bytes: int,
    progress: _ArchiveProgress,
) -> bytes:
    completed_bytes = 0
    progress.compression(0, len(files), 0, total_bytes, force=True)
    with SpooledTemporaryFile(max_size=16 * 1024 * 1024) as handle:
        with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for index, (file, archived_file) in enumerate(zip(files, archived_files, strict=True)):
                info = zipfile.ZipInfo(archived_file, ZIP_EPOCH)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                info.file_size = file.stat().st_size
                with file.open("rb") as source, archive.open(info, "w") as target:
                    while chunk := source.read(ARCHIVE_CHUNK_SIZE):
                        target.write(chunk)
                        completed_bytes += len(chunk)
                        progress.compression(index, len(files), completed_bytes, total_bytes)
                progress.compression(index + 1, len(files), completed_bytes, total_bytes)
        progress.compression(len(files), len(files), completed_bytes, total_bytes, force=True)
        progress.update("finalizing archive", force=True)
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
    "SourcePackageSyncError",
    "SourcePackageSyncResult",
    "SourcePackageSyncer",
    "SourcePackageUploadClient",
    "build_source_package_archive",
    "collect_source_files",
]
