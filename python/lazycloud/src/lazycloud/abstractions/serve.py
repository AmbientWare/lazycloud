"""`lazycloud serve`: a preview container that follows the working tree.

A preview is a release that runs one container until it is stopped. The
session uploads the source as a deploy does, starts the preview, prints how to
call it, syncs each saved file into the container and prints the container's
output until Ctrl+C. A record under the state directory lets later `.remote()`,
`run` and `.request()` calls from this machine use the running preview.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import httpx
from shared.api import Preview, PreviewRequest, PreviewState
from shared.paths import state_home
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from lazycloud.clients.api import ApiClient, ApiConnectionError, ApiError
from lazycloud.clients.endpoints import (
    PREVIEW_WAIT_SECONDS,
    create_preview,
    get_preview,
    stop_preview,
    stream_preview_output,
    sync_preview_files,
)
from lazycloud.control import api_client, require_workspace, resolve_control_client_config
from lazycloud.exceptions import SdkError
from lazycloud.source_sync import (
    SOURCE_IGNORE_FILE_WRITTEN_NOTICE,
    collect_source_files,
    ensure_source_ignore_file,
)
from lazycloud.terminal import Terminal

if TYPE_CHECKING:
    from lazycloud.abstractions.function import Function

DEFAULT_PREVIEW_RECORD_TTL_SECONDS = 24 * 60 * 60
DEFAULT_SYNC_DEBOUNCE_SECONDS = 0.1
# One sync request stays under the API's 64 MiB request limit.
MAX_SYNC_BATCH_BYTES = 32 << 20
REMOVED_RECORD = "LAZYCLOUD.removed"


class ServeError(SdkError):
    pass


@dataclass(frozen=True, slots=True)
class ServePreviewRecord:
    """A running preview this machine started, for later calls to use."""

    kind: str
    name: str
    app: str
    workspace: str
    endpoint: str
    preview_id: str
    container_id: str
    url: str
    created_at: float
    expires_at: float

    def payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "name": self.name,
            "app": self.app,
            "workspace": self.workspace,
            "endpoint": self.endpoint,
            "preview_id": self.preview_id,
            "container_id": self.container_id,
            "url": self.url,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> ServePreviewRecord:
        return cls(
            kind=str(payload["kind"]),
            name=str(payload["name"]),
            app=str(payload["app"]),
            workspace=str(payload["workspace"]),
            endpoint=str(payload["endpoint"]),
            preview_id=str(payload["preview_id"]),
            container_id=str(payload["container_id"]),
            url=str(payload["url"]),
            created_at=float(str(payload["created_at"])),
            expires_at=float(str(payload["expires_at"])),
        )


def _preview_record_path(*, kind: str, name: str, app: str, workspace: str, endpoint: str) -> Path:
    key = json.dumps(
        {
            "app": app,
            "endpoint": endpoint.rstrip("/"),
            "kind": kind,
            "name": name,
            "workspace": workspace,
        },
        sort_keys=True,
    )
    return state_home() / "serve-previews" / f"{hashlib.sha256(key.encode()).hexdigest()}.json"


def write_serve_preview(
    *,
    kind: str,
    name: str,
    app: str,
    workspace: str,
    endpoint: str,
    preview_id: str,
    container_id: str,
    url: str,
    ttl_seconds: float = DEFAULT_PREVIEW_RECORD_TTL_SECONDS,
) -> ServePreviewRecord:
    now = time.time()
    record = ServePreviewRecord(
        kind=kind,
        name=name,
        app=app,
        workspace=workspace,
        endpoint=endpoint,
        preview_id=preview_id,
        container_id=container_id,
        url=url,
        created_at=now,
        expires_at=now + ttl_seconds,
    )
    path = _preview_record_path(
        kind=kind, name=name, app=app, workspace=workspace, endpoint=endpoint
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(record.payload(), sort_keys=True), encoding="utf-8")
    temp.replace(path)
    return record


def read_serve_preview(
    *,
    kind: str,
    name: str,
    app: str,
    workspace: str,
    endpoint: str,
    client: ApiClient | None = None,
) -> ServePreviewRecord | None:
    """The running preview of this workload started from this machine, if any.

    With a client the preview is checked to still run; a check that cannot
    reach the API keeps the record, so an outage never ends a live preview.
    """

    path = _preview_record_path(
        kind=kind, name=name, app=app, workspace=workspace, endpoint=endpoint
    )
    try:
        record = ServePreviewRecord.from_payload(json.loads(path.read_text(encoding="utf-8")))
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        path.unlink(missing_ok=True)
        return None
    if record.expires_at <= time.time():
        path.unlink(missing_ok=True)
        return None
    if client is not None:
        try:
            preview = get_preview(client, workspace, UUID(record.preview_id))
        except ApiConnectionError:
            return record
        except ApiError:
            path.unlink(missing_ok=True)
            return None
        if preview.state is PreviewState.stopped:
            path.unlink(missing_ok=True)
            return None
    return record


def remove_serve_preview(record: ServePreviewRecord) -> None:
    _preview_record_path(
        kind=record.kind,
        name=record.name,
        app=record.app,
        workspace=record.workspace,
        endpoint=record.endpoint,
    ).unlink(missing_ok=True)


def print_invocation_details(
    terminal: Terminal, *, url: str, authorized: bool, token_configured: bool
) -> None:
    terminal.header("Preview URL")
    terminal.line("")
    terminal.line(f"curl -X POST '{url}' \\")
    terminal.line("-H 'Accept: */*' \\")
    if authorized:
        token = "[YOUR_AUTH_TOKEN]" if token_configured else "[TOKEN]"
        terminal.line(f"-H 'Authorization: Bearer {token}' \\")
    terminal.line("-H 'Content-Type: application/json' \\")
    terminal.line("-d '{}'")
    terminal.line("")
    terminal.header("Container output")


@dataclass(frozen=True, slots=True)
class FileState:
    size: int
    mtime_ns: int
    mode: int


def _snapshot(root: Path) -> dict[str, FileState]:
    files: dict[str, FileState] = {}
    for path in collect_source_files(root):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        if path.is_file():
            files[path.relative_to(root).as_posix()] = FileState(
                stat.st_size, stat.st_mtime_ns, stat.st_mode & 0o777
            )
    return files


def _sync_archives(
    root: Path, prefix: tuple[str, ...], changed: list[str], removed: list[str]
) -> list[bytes]:
    """Tars of the changes, each under MAX_SYNC_BATCH_BYTES of file content."""

    def name(relative: str) -> str:
        return "/".join((*prefix, relative))

    batches: list[list[tuple[str, bytes | None, int]]] = [
        [(name(relative), None, 0) for relative in removed]
    ]
    size = 0
    for relative in changed:
        path = root / relative
        try:
            data = path.read_bytes()
            mode = path.stat().st_mode & 0o777
        except FileNotFoundError:
            continue
        if size and size + len(data) > MAX_SYNC_BATCH_BYTES:
            batches.append([])
            size = 0
        batches[-1].append((name(relative), data, mode))
        size += len(data)
    archives: list[bytes] = []
    for batch in batches:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for entry, data, mode in batch:
                info = tarfile.TarInfo(entry)
                if data is None:
                    info.pax_headers = {REMOVED_RECORD: "1"}
                    archive.addfile(info)
                    continue
                info.size, info.mode = len(data), mode
                archive.addfile(info, io.BytesIO(data))
        archives.append(buffer.getvalue())
    return archives


class _Changes(FileSystemEventHandler):
    def __init__(self) -> None:
        self.ready = threading.Event()

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in {"created", "modified", "deleted", "moved"}:
            self.ready.set()


@dataclass
class WorkspaceSyncer:
    """Sends each saved change of a directory to the preview's container."""

    client: ApiClient
    workspace: str
    preview: UUID
    root: Path
    prefix: tuple[str, ...]
    terminal: Terminal
    debounce_seconds: float = DEFAULT_SYNC_DEBOUNCE_SECONDS
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _failure: Exception | None = field(default=None, init=False)

    def start(self) -> None:
        if ensure_source_ignore_file(self.root):
            self.terminal.detail(SOURCE_IGNORE_FILE_WRITTEN_NOTICE)
        self._thread = threading.Thread(target=self._run, name="serve-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise ServeError(f"directory sync failed: {self._failure}") from self._failure

    def _run(self) -> None:
        changes = _Changes()
        observer = Observer()
        try:
            snapshot = _snapshot(self.root)
            self.terminal.detail(f"Watching {len(snapshot)} files")
            observer.schedule(changes, str(self.root), recursive=True)
            observer.start()
            while not self._stop.is_set():
                if not changes.ready.wait(0.5):
                    continue
                if self._stop.wait(self.debounce_seconds):
                    return
                changes.ready.clear()
                current = _snapshot(self.root)
                removed = sorted(set(snapshot) - set(current))
                changed = sorted(p for p, state in current.items() if snapshot.get(p) != state)
                if not removed and not changed:
                    continue
                for archive in _sync_archives(self.root, self.prefix, changed, removed):
                    sync_preview_files(self.client, self.workspace, self.preview, archive)
                snapshot = current
                self.terminal.detail(f"Synced {len(changed)} changed, {len(removed)} removed")
        except Exception as exc:
            self._failure = exc
        finally:
            observer.stop()
            if observer.ident is not None:
                observer.join()


@dataclass
class ServePreviewSession:
    """Follows a running preview until Ctrl+C or its timeout."""

    client: ApiClient
    workspace: str
    preview: Preview
    terminal: Terminal
    authorized: bool
    token_configured: bool
    syncer: WorkspaceSyncer | None = None
    record: ServePreviewRecord | None = None
    reconnect_seconds: float = 0.5

    def run(self) -> None:
        print_invocation_details(
            self.terminal,
            url=self.preview.url,
            authorized=self.authorized,
            token_configured=self.token_configured,
        )
        if self.syncer is not None:
            self.syncer.start()
        try:
            self._follow()
        except KeyboardInterrupt:
            self.terminal.header("Stopping serve container")
            self._stop()
        except Exception:
            self.terminal.header("Stopping serve container")
            self._stop()
            raise
        finally:
            if self.syncer is not None:
                self.syncer.stop()
            if self.record is not None:
                remove_serve_preview(self.record)

    def _stop(self) -> None:
        try:
            stop_preview(self.client, self.workspace, self.preview.id)
        except SdkError as exc:
            self.terminal.warn(f"failed to stop serve container: {exc}")

    def _follow(self) -> None:
        after = 0
        reported = False
        while True:
            if self.syncer is not None:
                self.syncer.raise_if_failed()
            try:
                for entry in stream_preview_output(
                    self.client, self.workspace, self.preview.id, after=after, follow=True
                ):
                    reported = False
                    after = entry.id
                    self.terminal.write(
                        entry.data if entry.data.endswith("\n") else entry.data + "\n"
                    )
                    if self.syncer is not None:
                        self.syncer.raise_if_failed()
            except (ApiConnectionError, httpx.HTTPError):
                if not reported:
                    self.terminal.warn("serve attach connection lost; reconnecting")
                    reported = True
                time.sleep(self.reconnect_seconds)
                continue
            # The stream ends when the preview stops: Ctrl+C elsewhere, its
            # timeout, or a lapsed lease.
            state = get_preview(self.client, self.workspace, self.preview.id)
            if state.state is PreviewState.stopped:
                self.terminal.warn("serve container stopped; its exit code is not reported")
                return
            self.terminal.warn("serve attach stream ended; retrying")
            time.sleep(self.reconnect_seconds)


def serve_workload(
    workload: Function[..., Any] | Any,
    *,
    kind: str,
    authorized: bool,
    timeout: int = 0,
    sync_dir: str | None = None,
    workspace: str | None = None,
) -> Preview:
    """Start a preview of the workload and follow it until Ctrl+C."""

    from lazycloud.session.deployment import prepare_spec

    terminal = workload.terminal or Terminal()
    workload.terminal = terminal
    config = resolve_control_client_config(workspace=workspace, timeout_seconds=60)
    client = api_client(config)
    selected = require_workspace(config)
    root = Path(sync_dir or ".").expanduser().resolve()
    spec, prefix = prepare_spec(
        workload, client=client, workspace=selected, source_root=root, terminal=terminal
    )
    app = workload._app_slug
    with terminal.step("Preview", "starting") as step:
        preview = create_preview(
            client, selected, app, PreviewRequest(spec=spec, timeout_seconds=timeout)
        )
        try:
            while preview.state is PreviewState.starting:
                preview = get_preview(
                    client, selected, preview.id, wait_seconds=PREVIEW_WAIT_SECONDS
                )
        except BaseException:
            stop_preview(client, selected, preview.id)
            raise
        if preview.state is PreviewState.stopped:
            step.fail("stopped before it was ready")
            raise ServeError(f"the preview of {spec.name} stopped before its container was ready")
        step.done("ready")
    record = write_serve_preview(
        kind=kind,
        name=spec.name,
        app=app,
        workspace=selected,
        endpoint=config.endpoint,
        preview_id=str(preview.id),
        container_id=str(preview.container_id or ""),
        url=preview.url,
    )
    ServePreviewSession(
        client=client,
        workspace=selected,
        preview=preview,
        terminal=terminal,
        authorized=authorized,
        token_configured=bool(config.token),
        syncer=WorkspaceSyncer(
            client=client,
            workspace=selected,
            preview=preview.id,
            root=root,
            prefix=prefix,
            terminal=terminal,
        ),
        record=record,
    ).run()
    return preview


__all__ = [
    "ServeError",
    "ServePreviewRecord",
    "ServePreviewSession",
    "WorkspaceSyncer",
    "print_invocation_details",
    "read_serve_preview",
    "remove_serve_preview",
    "serve_workload",
    "write_serve_preview",
]
