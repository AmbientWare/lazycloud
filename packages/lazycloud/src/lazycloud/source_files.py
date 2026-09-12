from __future__ import annotations

import fnmatch
import os
import posixpath
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath

SOURCE_IGNORE_FILE = ".lazycloudignore"

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
    ".env.*",
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


def collect_source_files(
    root: Path,
    *,
    ignore_patterns: Sequence[str] | None = None,
    include_patterns: Sequence[str] | None = None,
) -> tuple[Path, ...]:
    root = root.expanduser().resolve()
    selected_ignore_patterns = (
        *DEFAULT_IGNORE_PATTERNS,
        *(ignore_patterns if ignore_patterns is not None else _ignore_patterns_from_file(root)),
    )
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
        return ()
    _assert_source_confined(root, ignore_file)
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
        for dirname in dirs:
            _assert_source_confined(root, current / dirname)
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
            _assert_source_confined(root, path)
            yield path


def _assert_source_confined(root: Path, path: Path) -> None:
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"source path escapes its root: {path.relative_to(root)}")


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
