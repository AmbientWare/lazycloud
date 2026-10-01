"""Keep a running container's /workspace in step with a local directory.

Files go through the container file operations: each changed file is written
whole, each removed one deleted. Relative paths land under /workspace.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from lazycloud.clients.workloads import WorkloadsClient
from lazycloud.exceptions import SdkError
from lazycloud.session.task import retry_transient
from lazycloud.source_sync import (
    SOURCE_IGNORE_FILE_WRITTEN_NOTICE,
    collect_source_files,
    ensure_source_ignore_file,
)
from lazycloud.terminal import Terminal

DEFAULT_SYNC_DEBOUNCE_SECONDS = 0.1
# Uploads in flight at once; each is one request to the container.
SYNC_CONCURRENCY = 8


class WorkspaceSyncError(SdkError):
    pass


@dataclass(frozen=True, slots=True)
class FileState:
    size: int
    mtime_ns: int
    mode: int


@dataclass(slots=True)
class ContainerWorkspaceSyncer:
    container_id: str
    local_dir: str
    client: WorkloadsClient
    terminal: Terminal | None = None
    debounce_seconds: float = DEFAULT_SYNC_DEBOUNCE_SECONDS
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _snapshot: dict[str, FileState] | None = field(default=None, init=False, repr=False)
    _failure: Exception | None = field(default=None, init=False, repr=False)

    @property
    def root(self) -> Path:
        return Path(self.local_dir).expanduser().resolve()

    def sync_once(self) -> None:
        """Write every selected file of the directory into the container."""
        if ensure_source_ignore_file(self.root):
            self._detail(SOURCE_IGNORE_FILE_WRITTEN_NOTICE)
        snapshot = _snapshot(self.root)
        self._apply(sorted(snapshot), [])
        self._snapshot = snapshot
        self._detail(f"Synced {len(snapshot)} files")

    def start(self) -> None:
        """Follow later changes in a background thread until `stop`."""
        if self._thread is not None:
            return
        if self._snapshot is None:
            self._snapshot = _snapshot(self.root)
        self._thread = threading.Thread(target=self._run, name="workspace-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join()

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise WorkspaceSyncError(f"directory sync failed: {self._failure}") from self._failure

    def _run(self) -> None:
        changes = _Changes()
        observer = Observer()
        try:
            observer.schedule(changes, str(self.root), recursive=True)
            observer.start()
            while not self._stop_event.is_set():
                if not changes.ready.wait(0.5):
                    continue
                if self._stop_event.wait(self.debounce_seconds):
                    return
                changes.ready.clear()
                previous = self._snapshot or {}
                current = _snapshot(self.root)
                removed = sorted(set(previous) - set(current))
                changed = sorted(p for p, state in current.items() if previous.get(p) != state)
                if not removed and not changed:
                    continue
                self._apply(changed, removed)
                self._snapshot = current
                self._detail(f"Synced {len(changed)} changed, {len(removed)} removed")
        except Exception as exc:
            self._failure = exc
        finally:
            observer.stop()
            if observer.ident is not None:
                observer.join()

    def _apply(self, changed: list[str], removed: list[str]) -> None:
        container = UUID(self.container_id)
        root = self.root

        def write(relative: str) -> None:
            path = root / relative
            if not path.resolve().is_relative_to(root):
                raise WorkspaceSyncError(f"source file escapes the directory: {relative}")
            try:
                content = path.read_bytes()
                mode = path.stat().st_mode & 0o777
            except FileNotFoundError:
                return
            retry_transient(
                lambda: self.client.upload_file(container, relative, content, mode=mode),
                not_found=lambda: WorkspaceSyncError(f"container {container} is not running"),
            )

        def delete(relative: str) -> None:
            retry_transient(
                lambda: self.client.delete_file(container, relative),
                not_found=lambda: WorkspaceSyncError(f"container {container} is not running"),
            )

        with ThreadPoolExecutor(max_workers=SYNC_CONCURRENCY) as pool:
            for result in [
                *(pool.submit(delete, relative) for relative in removed),
                *(pool.submit(write, relative) for relative in changed),
            ]:
                result.result()

    def _detail(self, message: str) -> None:
        if self.terminal is not None:
            self.terminal.detail(message)


class _Changes(FileSystemEventHandler):
    def __init__(self) -> None:
        self.ready = threading.Event()

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in {"created", "modified", "deleted", "moved"}:
            self.ready.set()


def sync_local_workspace(
    *,
    container_id: str,
    local_dir: str,
    client: WorkloadsClient,
    terminal: Terminal | None = None,
) -> None:
    ContainerWorkspaceSyncer(
        container_id=container_id, local_dir=local_dir, client=client, terminal=terminal
    ).sync_once()


def _snapshot(root: Path) -> dict[str, FileState]:
    files: dict[str, FileState] = {}
    for path in collect_source_files(root):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        if not path.is_file():
            continue
        files[path.relative_to(root).as_posix()] = FileState(
            size=stat.st_size, mtime_ns=stat.st_mtime_ns, mode=stat.st_mode & 0o777
        )
    return files


__all__ = ["ContainerWorkspaceSyncer", "WorkspaceSyncError", "sync_local_workspace"]
