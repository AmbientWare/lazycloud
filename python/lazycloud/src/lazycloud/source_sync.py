from __future__ import annotations

import hashlib
import os
import time
import zipfile
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from pathspec import PathSpec

from lazycloud.terminal import humanize_bytes

SOURCE_IGNORE_FILE = ".lazycloudignore"
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
ARCHIVE_CHUNK_SIZE = 1024 * 1024

# Applied on every sync, whether or not a `.lazycloudignore` exists and whatever it
# says. A trimmed file must not start shipping the virtualenv or a `.env`.
BASELINE_IGNORE_PATTERNS: tuple[str, ...] = (
    ".git",
    SOURCE_IGNORE_FILE,
    ".venv",
    "venv",
    "**/.venv/",
    "__pycache__",
    "**/__pycache__/",
    "*.pyc",
    ".env",
    ".env.local",
    ".envrc",
    ".lazycloud/",
)

DEFAULT_IGNORE_PATTERNS: tuple[str, ...] = (
    *BASELINE_IGNORE_PATTERNS,
    ".idea",
    ".python-version",
    ".vscode",
    ".DS_Store",
    ".config",
    ".coverage",
    ".pytest_cache",
    ".ruff_cache",
    ".dockerignore",
    ".ipynb_checkpoints",
    "**/.pytest_cache/",
    "**/node_modules/",
    "**/playwright-report/",
    "**/test-results/",
    ".next/",
    ".circleci",
)

SOURCE_IGNORE_FILE_HEADER = (
    "# Written by the LazyCloud SDK. Edit and commit it; patterns use gitignore syntax.",
    "# A short baseline (.git, .venv, __pycache__, .env, .lazycloud/) always applies.",
)
SOURCE_IGNORE_FILE_WRITTEN_NOTICE = (
    f"Wrote {SOURCE_IGNORE_FILE} with the default ignore patterns. Edit and commit it."
)


class SourcePackageSyncError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SourcePackageArchive:
    path: Path
    sha256: str
    size: int
    files: tuple[str, ...]


@contextmanager
def build_source_package_archive(
    root: Path,
    *,
    archive_prefix: Sequence[str] = (),
    ignore_patterns: Sequence[str] | None = None,
    include_patterns: Sequence[str] | None = None,
    progress: Callable[[str], None] | None = None,
) -> Iterator[SourcePackageArchive]:
    reporter = _ArchiveProgress(progress)
    reporter.update("collecting files", force=True)
    selected_archive_prefix = _validate_archive_prefix(archive_prefix)
    selected_ignore_patterns = effective_ignore_patterns(root, ignore_patterns)
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
    with TemporaryDirectory(prefix="lazycloud-source-") as temporary:
        path = Path(temporary) / "source.zip"
        _zip_files(files, archived_files, path=path, total_bytes=total_bytes, progress=reporter)
        size = path.stat().st_size
        digest = hashlib.sha256()
        reporter.update(f"hashing   0% · {humanize_bytes(size)}", force=True)
        completed = 0
        with path.open("rb") as source:
            while chunk := source.read(ARCHIVE_CHUNK_SIZE):
                digest.update(chunk)
                completed += len(chunk)
                reporter.update(f"hashing {completed * 100 // size:3}% · {humanize_bytes(size)}")
        reporter.update(f"hashing 100% · {humanize_bytes(size)}", force=True)
        yield SourcePackageArchive(
            path=path, sha256=digest.hexdigest(), size=size, files=archived_files
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
    selected_ignore_patterns = effective_ignore_patterns(root, ignore_patterns)
    selected_include_patterns = tuple(include_patterns or ())
    return tuple(
        _collect_source_files(
            root,
            ignore_patterns=selected_ignore_patterns,
            include_patterns=selected_include_patterns,
        )
    )


def ensure_source_ignore_file(root: Path) -> bool:
    """Write the default `.lazycloudignore` when the root has none. Returns whether
    a file was written. Only user-facing sync entry points call this; collection
    itself never writes into a source tree."""
    ignore_file = root / SOURCE_IGNORE_FILE
    if ignore_file.exists():
        return False
    ignore_file.write_text(
        "\n".join((*SOURCE_IGNORE_FILE_HEADER, *DEFAULT_IGNORE_PATTERNS)) + "\n",
        encoding="utf-8",
    )
    return True


def effective_ignore_patterns(
    root: Path, ignore_patterns: Sequence[str] | None = None
) -> tuple[str, ...]:
    if ignore_patterns:
        return (*BASELINE_IGNORE_PATTERNS, *ignore_patterns)
    ignore_file = root / SOURCE_IGNORE_FILE
    if not ignore_file.is_file():
        return DEFAULT_IGNORE_PATTERNS
    lines = tuple(
        line
        for line in (raw.strip() for raw in ignore_file.read_text(encoding="utf-8").splitlines())
        if line and not line.startswith("#")
    )
    return (*BASELINE_IGNORE_PATTERNS, *lines)


def _collect_source_files(
    root: Path,
    *,
    ignore_patterns: Sequence[str],
    include_patterns: Sequence[str],
    progress: _ArchiveProgress | None = None,
) -> Iterable[Path]:
    file_count = 0
    directory_count = 0
    ignored = _compile_patterns(ignore_patterns)
    included = _compile_patterns(include_patterns) if include_patterns else None
    for current_root, dirs, files in os.walk(root):
        directory_count += 1
        if progress is not None:
            progress.collection(file_count, directory_count)
        current = Path(current_root)
        dirs[:] = [
            dirname
            for dirname in sorted(dirs)
            if not _matches(ignored, _relative_posix(root, current / dirname), directory=True)
        ]
        for filename in sorted(files):
            path = current / filename
            relative = _relative_posix(root, path)
            if _matches(ignored, relative, directory=False):
                continue
            if included is not None and not _matches(included, relative, directory=False):
                continue
            file_count += 1
            if progress is not None:
                progress.collection(file_count, directory_count)
            yield path


def _zip_files(
    files: Sequence[Path],
    archived_files: Sequence[str],
    *,
    path: Path,
    total_bytes: int,
    progress: _ArchiveProgress,
) -> None:
    completed_bytes = 0
    progress.compression(0, len(files), 0, total_bytes, force=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
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


def _compile_patterns(patterns: Sequence[str]) -> PathSpec:
    return PathSpec.from_lines("gitwildmatch", patterns)


def _matches(spec: PathSpec, relative_path: str, *, directory: bool) -> bool:
    normalized = _normalize_relative_path(relative_path)
    if not normalized:
        return False
    # A trailing slash is how gitwildmatch tells `build/` (directory only) from a
    # file of the same name, and lets a pruned directory take its contents with it.
    return spec.match_file(f"{normalized}/" if directory else normalized)


def _normalize_relative_path(path: str) -> str:
    value = path.replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value.strip("/")


def _relative_posix(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


__all__ = [
    "BASELINE_IGNORE_PATTERNS",
    "DEFAULT_IGNORE_PATTERNS",
    "SOURCE_IGNORE_FILE",
    "SOURCE_IGNORE_FILE_WRITTEN_NOTICE",
    "SourcePackageArchive",
    "SourcePackageSyncError",
    "build_source_package_archive",
    "collect_source_files",
    "effective_ignore_patterns",
    "ensure_source_ignore_file",
]
