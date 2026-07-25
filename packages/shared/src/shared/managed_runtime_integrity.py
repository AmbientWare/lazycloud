from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol

MANAGED_PACKAGE_NAMES = ("shared", "foundation", "lazycloud", "runner")

_IGNORED_DIRECTORY_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
}
_IGNORED_DIRECTORY_SUFFIXES = (".dist-info", ".egg-info")
_IGNORED_SUFFIXES = {".pyc", ".pyo"}


class _Digest(Protocol):
    def update(self, data: bytes, /) -> None: ...


def managed_package_source_digest(packages_root: Path | None = None) -> str:
    root = packages_root.resolve() if packages_root is not None else _find_packages_root()
    if root is None:
        return ""
    sources = tuple(root / name for name in MANAGED_PACKAGE_NAMES)
    if not all(_is_python_package_source(source) for source in sources):
        return ""
    digest = hashlib.sha256()
    for source in sources:
        _update_source_digest(digest, source)
    return digest.hexdigest()


def managed_runtime_artifact_digest(root: Path) -> str:
    artifact_root = root.resolve()
    if not artifact_root.is_dir():
        return ""
    digest = hashlib.sha256()
    for path in sorted(artifact_root.rglob("*")):
        relative = path.relative_to(artifact_root)
        _update_path_digest(digest, path, relative.as_posix())
    return digest.hexdigest()


def _find_packages_root() -> Path | None:
    for packages_root in _candidate_packages_roots():
        sources = tuple(packages_root / name for name in MANAGED_PACKAGE_NAMES)
        if all(_is_python_package_source(source) for source in sources):
            return packages_root
    return None


def _candidate_packages_roots() -> tuple[Path, ...]:
    module_path = Path(__file__).resolve()
    candidates = [Path("/app/packages"), Path.cwd() / "packages"]
    candidates.extend(parent / "packages" for parent in module_path.parents)
    result: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        path = candidate.resolve()
        if path in seen:
            continue
        seen.add(path)
        result.append(path)
    return tuple(result)


def _is_python_package_source(path: Path) -> bool:
    return path.is_dir() and (path / "pyproject.toml").is_file() and (path / "src").is_dir()


def _update_source_digest(digest: _Digest, source: Path) -> None:
    root = source.resolve()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if _ignored(relative):
            continue
        _update_path_digest(digest, path, f"{source.name}/{relative.as_posix()}")


def _update_path_digest(digest: _Digest, path: Path, name: str) -> None:
    if path.is_dir():
        digest.update(f"{name}/".encode())
        digest.update(b"\0")
        return
    if not path.is_file():
        return
    digest.update(name.encode())
    digest.update(b"\0")
    digest.update(path.read_bytes())
    digest.update(b"\0")


def _ignored(relative: Path) -> bool:
    if any(
        part in _IGNORED_DIRECTORY_NAMES or part.endswith(_IGNORED_DIRECTORY_SUFFIXES)
        for part in relative.parts
    ):
        return True
    return relative.suffix in _IGNORED_SUFFIXES


__all__ = [
    "MANAGED_PACKAGE_NAMES",
    "managed_package_source_digest",
    "managed_runtime_artifact_digest",
]
