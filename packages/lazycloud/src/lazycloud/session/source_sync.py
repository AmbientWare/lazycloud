from __future__ import annotations

import hashlib
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import SpooledTemporaryFile
from types import TracebackType
from typing import Protocol

from shared.app_identity import SOURCE_PACKAGE_BUCKET
from typing_extensions import Self

from lazycloud.json_contracts import validate_json_object
from lazycloud.source_files import collect_source_files
from lazycloud.terminal import ProgressCallback, humanize_bytes

SOURCE_PACKAGE_PREFIX = "sources"
SOURCE_PACKAGE_CONTENT_TYPE = "application/zip"
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


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
    root: Path
    archive_prefix: tuple[str, ...]


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

        archive = build_source_package_archive(
            root,
            archive_prefix=self.archive_prefix,
            ignore_patterns=ignore_patterns,
            include_patterns=include_patterns,
        )
        plural = "s" if len(archive.files) != 1 else ""
        description = f"{len(archive.files)} file{plural}, {humanize_bytes(archive.size)}"
        with self._step("Source", description) as step:
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
                root=root,
                archive_prefix=self.archive_prefix,
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
    root = root.expanduser().resolve()
    selected_archive_prefix = _validate_archive_prefix(archive_prefix)
    files = collect_source_files(
        root,
        ignore_patterns=ignore_patterns,
        include_patterns=include_patterns,
    )
    archived_files = tuple(
        _archive_path(selected_archive_prefix, item.relative_to(root).as_posix()) for item in files
    )
    data = _zip_files(root, files, archived_files)
    digest = hashlib.sha256(data).hexdigest()
    return SourcePackageArchive(
        data=data,
        sha256=digest,
        size=len(data),
        files=archived_files,
    )


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
    "SOURCE_PACKAGE_BUCKET",
    "SOURCE_PACKAGE_CONTENT_TYPE",
    "SOURCE_PACKAGE_PREFIX",
    "SourcePackageArchive",
    "SourcePackageSyncError",
    "SourcePackageSyncResult",
    "SourcePackageSyncer",
    "SourcePackageUploadClient",
    "build_source_package_archive",
]
