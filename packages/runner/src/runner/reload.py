from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

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
        self._snapshot = _snapshot(self.root)
        self._thread = threading.Thread(target=self._run, name="runner-hot-reload", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop_event.wait(self.poll_seconds):
            next_snapshot = _snapshot(self.root)
            if next_snapshot == self._snapshot:
                continue
            self._snapshot = next_snapshot
            try:
                self.on_change()
            except Exception as exc:
                print(f"hot reload failed: {type(exc).__name__}: {exc}", file=sys.stderr)


@dataclass(frozen=True, slots=True)
class FileState:
    size: int
    mtime_ns: int


def hot_reload_enabled(env: dict[str, str] | None = None) -> bool:
    source = env or os.environ
    return truthy_env_value(source.get(HOT_RELOAD_ENV))


def hot_reload_root(env: dict[str, str] | None = None) -> Path:
    source = env or os.environ
    raw = source.get(HOT_RELOAD_DIR_ENV)
    return Path(raw).expanduser() if raw else handler_loading.USER_CODE_DIR


def _snapshot(root: Path) -> dict[str, FileState]:
    if not root.exists():
        return {}
    files: dict[str, FileState] = {}
    for path in root.rglob("*.py"):
        if _ignored(path):
            continue
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        files[path.relative_to(root).as_posix()] = FileState(
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        )
    return files


def _ignored(path: Path) -> bool:
    return any(part in IGNORED_PARTS for part in path.parts)


__all__ = [
    "SourceChangeWatcher",
    "hot_reload_enabled",
    "hot_reload_root",
]
