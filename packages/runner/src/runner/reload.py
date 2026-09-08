from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from stat import S_ISREG

from shared.env import (
    HOT_RELOAD_DIR_ENV,
    HOT_RELOAD_ENV,
    truthy_env_value,
)

from foundation import handler_loading

DEFAULT_RELOAD_POLL_SECONDS = 0.5
IGNORED_PARTS = frozenset(
    {
        "__pycache__",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "node_modules",
        "venv",
    }
)


@dataclass(slots=True)
class SourceChangeWatcher:
    root: Path
    on_change: Callable[[], None]
    poll_seconds: float = DEFAULT_RELOAD_POLL_SECONDS
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _snapshot: dict[str, FileState] = field(default_factory=dict, init=False, repr=False)

    def start(self) -> None:
        if self._thread is not None:
            return
        self.root = self.root.expanduser().resolve()
        self._snapshot = _snapshot(self.root, {})
        self._thread = threading.Thread(target=self._run, name="runner-hot-reload", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop_event.wait(self.poll_seconds):
            next_snapshot = _snapshot(self.root, self._snapshot)
            changed = next_snapshot != self._snapshot
            self._snapshot = next_snapshot
            if not changed:
                continue
            try:
                self.on_change()
            except Exception as exc:
                print(f"hot reload failed: {type(exc).__name__}: {exc}", file=sys.stderr)


@dataclass(frozen=True, slots=True)
class FileState:
    size: int = field(compare=False)
    mtime_ns: int = field(compare=False)
    content_hash: bytes


def hot_reload_enabled(env: dict[str, str] | None = None) -> bool:
    source = env or os.environ
    return truthy_env_value(source.get(HOT_RELOAD_ENV))


def hot_reload_root(env: dict[str, str] | None = None) -> Path:
    source = env or os.environ
    raw = source.get(HOT_RELOAD_DIR_ENV)
    return Path(raw).expanduser() if raw else handler_loading.USER_CODE_DIR


def _snapshot(root: Path, previous: dict[str, FileState]) -> dict[str, FileState]:
    if not root.exists():
        return {}
    files: dict[str, FileState] = {}
    for path in root.rglob("*.py"):
        if _ignored(path):
            continue
        try:
            stat = path.stat()
            if not S_ISREG(stat.st_mode):
                continue
            relative = path.relative_to(root).as_posix()
            cached = previous.get(relative)
            if cached is not None and (cached.size, cached.mtime_ns) == (
                stat.st_size,
                stat.st_mtime_ns,
            ):
                files[relative] = cached
                continue
            digest = sha256()
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
        except FileNotFoundError:
            continue
        files[relative] = FileState(
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            content_hash=digest.digest(),
        )
    return files


def _ignored(path: Path) -> bool:
    return any(part in IGNORED_PARTS for part in path.parts)


__all__ = [
    "SourceChangeWatcher",
    "hot_reload_enabled",
    "hot_reload_root",
]
