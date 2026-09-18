from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol
from uuid import uuid4

from shared.containers import ContainerStatus
from shared.deployment_records import CpuRequest, MemoryRequest
from shared.deployments import DeploymentKind
from shared.gpu import GpuInput
from shared.http.compute import ContainerResponse, ContainerWithAppPageResponse
from shared.http.gateway import (
    AttachToContainerResponse,
    GatewayUrlKind,
    GetUrlRequest,
    GetUrlResponse,
)
from shared.http.workspace_sync import (
    MAX_SYNC_BATCH_BYTES,
    MAX_SYNC_CHANGES,
    MAX_SYNC_MANIFEST_BYTES,
    SYNC_CHUNK_BYTES,
    WorkspaceSyncBatch,
    WorkspaceSyncEntry,
    WorkspaceSyncManifest,
    WorkspaceSyncOperation,
    WorkspaceSyncResponse,
)
from shared.paths import state_home
from shared.transport_retry import (
    TRANSIENT_TRANSPORT_ERRORS,
    TransientRetry,
    call_with_transient_retry,
    is_transient_transport_error,
)
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from lazycloud.abstractions.image import Image
from lazycloud.abstractions.metadata import MachineInput
from lazycloud.json_contracts import parse_json_object
from lazycloud.source_sync import SOURCE_IGNORE_FILE, SourceFileFilter
from lazycloud.terminal import Terminal

LOGGER = logging.getLogger(__name__)

DEFAULT_ATTACH_POLL_SECONDS = 0.5
DEFAULT_SYNC_DEBOUNCE_SECONDS = 0.1
DEFAULT_INITIAL_SYNC_TIMEOUT_SECONDS = 30.0
DEFAULT_PREVIEW_RECORD_TTL_SECONDS = 24 * 60 * 60
ACTIVE_PREVIEW_CONTAINER_STATUSES = {ContainerStatus.Pending, ContainerStatus.Running}


class PreviewRuntime(Protocol):
    image: Image
    cpu: CpuRequest | None
    memory: MemoryRequest | None
    gpu: GpuInput
    gpu_count: int
    env: dict[str, str]
    secrets: list[str]
    region: str | None
    machine: MachineInput


@dataclass(frozen=True, slots=True)
class ServeOptions:
    image: Image | None = None
    cpu: CpuRequest | None = None
    memory: MemoryRequest | None = None
    gpu: GpuInput = None
    gpu_count: int | None = None
    env: Mapping[str, str] = field(default_factory=dict[str, str], repr=False)
    secrets: Sequence[str] = ()
    keep_warm: int | None = None
    region: str | None = None
    machine: MachineInput = None
    sync_dir: str | None = None

    def apply(self, owner: PreviewRuntime) -> None:
        if self.image is not None:
            owner.image = self.image
        if self.cpu is not None:
            owner.cpu = self.cpu
        if self.memory is not None:
            owner.memory = self.memory
        if self.gpu is not None:
            owner.gpu = self.gpu
        if self.gpu_count is not None:
            owner.gpu_count = self.gpu_count
        owner.env.update(self.env)
        owner.secrets.extend(secret for secret in self.secrets if secret not in owner.secrets)
        if self.region is not None:
            owner.region = self.region
        if self.machine is not None:
            owner.machine = self.machine


class ServeUrlClient(Protocol):
    def get_url(self, request: GetUrlRequest) -> GetUrlResponse: ...


class WorkspaceSyncClient(Protocol):
    def sync_container_workspace(
        self,
        body: WorkspaceSyncBatch,
    ) -> WorkspaceSyncResponse: ...


class PreviewContainerClient(Protocol):
    def list_containers(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> ContainerWithAppPageResponse: ...


class ContainerLifecycleClient(Protocol):
    def stop_container(self, container_id: str) -> ContainerResponse: ...


class ServeResourceClient(PreviewContainerClient, ContainerLifecycleClient, Protocol):
    pass


class ServeSessionClient(WorkspaceSyncClient, Protocol):
    def attach_to_container_events(
        self,
        container_id: str,
        *,
        poll_interval_seconds: float = DEFAULT_ATTACH_POLL_SECONDS,
    ) -> Iterator[AttachToContainerResponse]: ...


class ServeGatewayClient(ServeUrlClient, ServeSessionClient, Protocol):
    pass


@dataclass(frozen=True, slots=True)
class ServePreviewUrl:
    stub_id: str
    url: str


@dataclass(frozen=True, slots=True)
class ServePreviewRecord:
    kind: DeploymentKind
    name: str
    app: str
    workspace: str
    endpoint: str
    stub_id: str
    container_id: str
    url: str
    created_at: float
    expires_at: float

    def payload(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "name": self.name,
            "app": self.app,
            "workspace": self.workspace,
            "endpoint": self.endpoint,
            "stub_id": self.stub_id,
            "container_id": self.container_id,
            "url": self.url,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> ServePreviewRecord:
        return cls(
            kind=DeploymentKind(str(payload["kind"])),
            name=str(payload["name"]),
            app=str(payload["app"]),
            workspace=str(payload["workspace"]),
            endpoint=str(payload["endpoint"]),
            stub_id=str(payload["stub_id"]),
            container_id=str(payload["container_id"]),
            url=str(payload["url"]),
            created_at=_payload_float(payload, "created_at"),
            expires_at=_payload_float(payload, "expires_at"),
        )


def _payload_float(payload: Mapping[str, object], key: str) -> float:
    value = payload[key]
    if isinstance(value, int | float | str):
        return float(value)
    msg = f"preview record field {key!r} must be numeric"
    raise TypeError(msg)


@dataclass(slots=True)
class ServePreviewSession:
    stub_id: str
    container_id: str
    url: str
    gateway_client: ServeSessionClient
    resource_client: ContainerLifecycleClient
    terminal: Terminal | None = None
    sync_dir: str | None = None
    token: str | None = None
    authorized: bool = True
    attach_poll_seconds: float = DEFAULT_ATTACH_POLL_SECONDS
    sync_debounce_seconds: float = DEFAULT_SYNC_DEBOUNCE_SECONDS
    initial_sync_timeout_seconds: float = DEFAULT_INITIAL_SYNC_TIMEOUT_SECONDS
    preview_record: ServePreviewRecord | None = None
    _syncer: ContainerWorkspaceSyncer | None = field(default=None, init=False, repr=False)

    def run(self) -> None:
        terminal = self.terminal or Terminal()
        print_invocation_details(
            terminal,
            url=self.url,
            authorized=self.authorized,
            token_configured=bool(self.token),
        )
        if self.sync_dir:
            self._syncer = ContainerWorkspaceSyncer(
                container_id=self.container_id,
                local_dir=self.sync_dir,
                gateway_client=self.gateway_client,
                terminal=terminal,
                debounce_seconds=self.sync_debounce_seconds,
                initial_sync_timeout_seconds=self.initial_sync_timeout_seconds,
                full_initial_sync=False,
            )
            self._syncer.start()
        try:
            self._attach_foreground(terminal)
            if self._syncer is not None:
                self._syncer.stop()
                self._syncer.raise_if_failed()
        except KeyboardInterrupt:
            terminal.header("Stopping serve container")
            self._stop_with_warning(terminal)
        except Exception:
            terminal.header("Stopping serve container")
            self._stop_with_warning(terminal)
            raise
        finally:
            if self._syncer is not None:
                self._syncer.stop()
            if self.preview_record is not None:
                remove_serve_preview(self.preview_record)

    def stop(self) -> None:
        self.resource_client.stop_container(self.container_id)

    def _stop_with_warning(self, terminal: Terminal) -> None:
        try:
            self.stop()
        except Exception as exc:
            terminal.warn(f"failed to stop serve container: {exc}")

    def _attach_foreground(self, terminal: Terminal) -> None:
        self._attach_stream(terminal)

    def _attach_stream(self, terminal: Terminal) -> None:
        retry = TransientRetry()
        attach_timeout_reported = False
        reconnect_reported = False
        while True:
            if self._syncer is not None:
                self._syncer.raise_if_failed()
            try:
                for response in self.gateway_client.attach_to_container_events(
                    self.container_id,
                    poll_interval_seconds=self.attach_poll_seconds,
                ):
                    if self._syncer is not None:
                        self._syncer.raise_if_failed()
                    retry.reset()
                    attach_timeout_reported = False
                    reconnect_reported = False
                    if response.error_msg:
                        terminal.error(response.error_msg)
                    if response.output:
                        terminal.write(response.output)
                    if response.done:
                        if response.exit_code is None:
                            terminal.warn("serve container stopped; its exit code is not reported")
                        elif response.exit_code:
                            terminal.error(f"serve container exited with code {response.exit_code}")
                        return
            except TimeoutError:
                # An idle long-poll read timeout means the control plane is
                # reachable but quiet; reconnect without consuming the
                # transient-failure budget.
                retry.reset()
                if not attach_timeout_reported:
                    terminal.warn("serve attach timed out; retrying")
                    attach_timeout_reported = True
                time.sleep(self.attach_poll_seconds)
                continue
            except TRANSIENT_TRANSPORT_ERRORS as exc:
                if not is_transient_transport_error(exc):
                    raise
                if not reconnect_reported:
                    terminal.warn("serve attach connection lost; reconnecting")
                    reconnect_reported = True
                retry.backoff(exc)
                continue
            terminal.warn("serve attach stream ended; retrying")
            time.sleep(self.attach_poll_seconds)


@dataclass(slots=True)
class ContainerWorkspaceSyncer:
    container_id: str
    local_dir: str
    gateway_client: WorkspaceSyncClient
    terminal: Terminal | None = None
    debounce_seconds: float = DEFAULT_SYNC_DEBOUNCE_SECONDS
    initial_sync_timeout_seconds: float = DEFAULT_INITIAL_SYNC_TIMEOUT_SECONDS
    full_initial_sync: bool = True
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _snapshot: dict[str, FileState] = field(default_factory=dict, init=False, repr=False)
    _initialized: bool = field(default=False, init=False, repr=False)
    _failure: Exception | None = field(default=None, init=False, repr=False)

    _changes: _WorkspaceEvents = field(
        default_factory=lambda: _WorkspaceEvents(), init=False, repr=False
    )

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="serve-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._changes.ready.set()
        if self._thread is not None:
            self._thread.join()

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise RuntimeError(f"directory sync failed: {self._failure}") from self._failure

    def _run(self) -> None:
        observer = Observer()
        root = Path(self.local_dir).expanduser().resolve()
        try:
            self._changes.root = root
            self._changes.selection = SourceFileFilter.for_root(root)
            observer.schedule(self._changes, str(root), recursive=True)
            observer.start()
            self._sync_initial_with_retries()
            while not self._stop_event.is_set():
                if not self._changes.ready.wait(1):
                    if not observer.is_alive() or any(
                        not emitter.is_alive() for emitter in observer.emitters
                    ):
                        raise RuntimeError("source filesystem watcher stopped")
                    continue
                if self._stop_event.wait(self.debounce_seconds):
                    break
                paths, rescan = self._changes.take()
                if rescan:
                    self._changes.selection = SourceFileFilter.for_root(root)
                    next_snapshot = _snapshot(self.local_dir)
                    self._sync_delta(next_snapshot)
                    self._snapshot = next_snapshot
                else:
                    changed: list[str] = []
                    removed: list[str] = []
                    for relative in sorted(paths):
                        path = root / relative
                        if path.is_file() and self._changes.selection.includes(relative):
                            changed.append(relative)
                        elif relative in self._snapshot:
                            removed.append(relative)
                    self._send_changes(removed, changed)
                    for relative in removed:
                        self._snapshot.pop(relative, None)
                    for relative in changed:
                        try:
                            stat = (root / relative).stat()
                            self._snapshot[relative] = FileState(
                                stat.st_size, stat.st_mtime_ns, stat.st_mode & 0o777
                            )
                        except FileNotFoundError:
                            pass
        except Exception as exc:
            self._failure = exc
        finally:
            observer.stop()
            if observer.ident is not None:
                observer.join()

    def _sync_initial_with_retries(self) -> None:
        if self._initialized:
            return
        if not self.full_initial_sync:
            self.record_seed_snapshot()
            return
        deadline = time.monotonic() + max(self.initial_sync_timeout_seconds, 0.0)
        announced_wait = False
        while True:
            try:
                self.sync_once()
                return
            except Exception as exc:
                if (
                    self._stop_event.is_set()
                    or not _is_retryable_initial_sync_error(exc)
                    or time.monotonic() >= deadline
                ):
                    raise
                if not announced_wait:
                    self._detail(f"Waiting for serve sync endpoint: {exc}")
                    announced_wait = True
                if self._stop_event.wait(self.debounce_seconds):
                    return

    def sync_once(self) -> None:
        self._snapshot = _snapshot(self.local_dir)
        self._sync_initial(self._snapshot)
        self._initialized = True
        self._detail(f"Synced {len(self._snapshot)} files")

    def record_seed_snapshot(self) -> None:
        self._snapshot = _snapshot(self.local_dir)
        self._initialized = True
        self._detail(f"Watching {len(self._snapshot)} files")

    def _sync_initial(self, snapshot: dict[str, FileState]) -> None:
        self._send_changes([], sorted(snapshot))

    def _sync_delta(self, next_snapshot: dict[str, FileState]) -> None:
        removed = sorted(set(self._snapshot) - set(next_snapshot))
        changed = [
            relative
            for relative, state in sorted(next_snapshot.items())
            if self._snapshot.get(relative) != state
        ]
        self._send_changes(removed, changed)

    def _send_changes(self, removed: list[str], changed: list[str]) -> None:
        with TemporaryDirectory(prefix="lazycloud-sync-") as temporary:
            self._send_snapshots(removed, changed, Path(temporary))

    def _send_snapshots(self, removed: list[str], changed: list[str], temporary: Path) -> None:
        entries: list[WorkspaceSyncEntry] = []
        sources: list[Path] = []
        total_size = 0
        manifest_size = 1024
        root = Path(self.local_dir).expanduser().resolve()
        changed_paths = set(changed)
        for index, relative in enumerate(removed + changed):
            if self._stop_event.is_set():
                return
            size = 0
            checksum = ""
            mode = 0o644
            operation = WorkspaceSyncOperation.Delete
            snapshot: Path | None = None
            if relative in changed_paths:
                path = root / relative
                if not path.resolve().is_relative_to(root):
                    raise ValueError(f"source file escapes source root: {relative}")
                try:
                    with path.open("rb") as source:
                        mode = os.fstat(source.fileno()).st_mode & 0o777
                        snapshot = temporary / str(index)
                        digest = hashlib.sha256()
                        with snapshot.open("wb") as target:
                            while chunk := source.read(SYNC_CHUNK_BYTES):
                                if self._stop_event.is_set():
                                    return
                                target.write(chunk)
                                digest.update(chunk)
                                size += len(chunk)
                        checksum = digest.hexdigest()
                    operation = WorkspaceSyncOperation.Write
                except FileNotFoundError:
                    pass
            entry = WorkspaceSyncEntry(
                operation=operation,
                path=relative,
                mode=mode,
                size=min(size, MAX_SYNC_BATCH_BYTES),
                file_size=size,
                sha256=checksum,
                transfer_id=uuid4().hex if snapshot is not None else "",
            )
            entry_bytes = len(entry.model_dump_json().encode()) + 1
            if entries and (
                total_size + size > MAX_SYNC_BATCH_BYTES
                or len(entries) >= MAX_SYNC_CHANGES
                or manifest_size + entry_bytes > MAX_SYNC_MANIFEST_BYTES
            ):
                self._send_batch(entries, sources)
                for source_path in sources:
                    source_path.unlink()
                entries, sources = [], []
                total_size, manifest_size = 0, 1024
            if size > MAX_SYNC_BATCH_BYTES and snapshot is not None:
                try:
                    for offset in range(0, size, MAX_SYNC_BATCH_BYTES):
                        if self._stop_event.is_set():
                            raise InterruptedError("workspace sync stopped")
                        fragment = entry.model_copy(
                            update={
                                "offset": offset,
                                "size": min(MAX_SYNC_BATCH_BYTES, size - offset),
                            }
                        )
                        self._send_batch([fragment], [snapshot])
                except BaseException:
                    abort = WorkspaceSyncEntry(
                        operation=WorkspaceSyncOperation.Abort,
                        path=relative,
                        transfer_id=entry.transfer_id,
                    )
                    with suppress(Exception):
                        self._send_batch([abort], [])
                    raise
                snapshot.unlink()
                continue
            entries.append(entry)
            total_size += size
            manifest_size += entry_bytes
            if snapshot is not None:
                sources.append(snapshot)
        if entries:
            self._send_batch(entries, sources)
        if removed or changed:
            self._detail(f"Synced {len(changed)} changed, {len(removed)} removed")

    def _send_batch(self, entries: list[WorkspaceSyncEntry], sources: list[Path]) -> None:
        def content() -> Iterator[bytes]:
            writes = [entry for entry in entries if entry.operation is WorkspaceSyncOperation.Write]
            for path, entry in zip(sources, writes, strict=True):
                with path.open("rb") as source:
                    source.seek(entry.offset)
                    remaining = entry.size
                    while remaining:
                        chunk = source.read(min(remaining, SYNC_CHUNK_BYTES))
                        if not chunk:
                            raise ValueError("workspace snapshot is incomplete")
                        yield chunk
                        remaining -= len(chunk)

        def send() -> WorkspaceSyncResponse:
            return self.gateway_client.sync_container_workspace(
                WorkspaceSyncBatch(
                    manifest=WorkspaceSyncManifest(container_id=self.container_id, entries=entries),
                    data=content(),
                )
            )

        try:
            response = call_with_transient_retry(send)
        except BaseException:
            aborts = [
                WorkspaceSyncEntry(
                    operation=WorkspaceSyncOperation.Abort,
                    path=entry.path,
                    transfer_id=entry.transfer_id,
                )
                for entry in entries
                if entry.operation is WorkspaceSyncOperation.Write and entry.size == entry.file_size
            ]
            if aborts:
                with suppress(Exception):
                    self.gateway_client.sync_container_workspace(
                        WorkspaceSyncBatch(
                            manifest=WorkspaceSyncManifest(
                                container_id=self.container_id, entries=aborts
                            ),
                            data=(),
                        )
                    )
            raise
        if response.applied != len(entries):
            raise RuntimeError("worker did not acknowledge every source change")

    def _detail(self, message: str) -> None:
        if self.terminal is not None:
            self.terminal.detail(message)


class _WorkspaceEvents(FileSystemEventHandler):
    def __init__(self) -> None:
        self.root = Path(".")
        self.selection = SourceFileFilter(self.root, ())
        self.ready = threading.Event()
        self.lock = threading.Lock()
        self.paths: set[str] = set()
        self.rescan = False

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type not in {"created", "modified", "deleted", "moved"}:
            return
        if event.is_directory and event.event_type == "modified":
            return
        with self.lock:
            for raw in (event.src_path, event.dest_path):
                if not raw:
                    continue
                try:
                    relative = Path(os.fsdecode(raw)).relative_to(self.root).as_posix()
                except ValueError:
                    continue
                if relative == SOURCE_IGNORE_FILE or relative == ".":
                    self.rescan = True
                elif self.selection.includes(relative, directory=event.is_directory):
                    if event.is_directory or len(self.paths) >= 10000:
                        self.rescan = True
                    else:
                        self.paths.add(relative)
            if self.paths or self.rescan:
                self.ready.set()

    def take(self) -> tuple[set[str], bool]:
        with self.lock:
            paths, rescan = self.paths, self.rescan
            self.paths, self.rescan = set(), False
            self.ready.clear()
            return paths, rescan


@dataclass(frozen=True, slots=True)
class FileState:
    size: int
    mtime_ns: int
    mode: int


def resolve_serve_url(
    client: ServeUrlClient,
    *,
    stub_id: str,
    container_id: str,
    workspace: str | None = None,
    external_url: str,
) -> ServePreviewUrl:
    response = client.get_url(
        GetUrlRequest(
            stub_id=stub_id,
            container_id=container_id,
            url_type=GatewayUrlKind.Stub,
            workspace=workspace,
            external_url=external_url,
        )
    )
    if not response.url:
        raise RuntimeError("serve URL lookup failed")
    return ServePreviewUrl(stub_id=stub_id, url=response.url)


def write_serve_preview(
    *,
    kind: DeploymentKind,
    name: str,
    app: str,
    workspace: str,
    endpoint: str,
    stub_id: str,
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
        stub_id=stub_id,
        container_id=container_id,
        url=url,
        created_at=now,
        expires_at=now + ttl_seconds,
    )
    path = _preview_record_path(
        kind=kind,
        name=name,
        app=app,
        workspace=workspace,
        endpoint=endpoint,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(record.payload(), sort_keys=True), encoding="utf-8")
    temp_path.replace(path)
    return record


def read_serve_preview(
    *,
    kind: DeploymentKind,
    name: str,
    app: str,
    workspace: str,
    endpoint: str,
    client: PreviewContainerClient | None = None,
) -> ServePreviewRecord | None:
    path = _preview_record_path(
        kind=kind,
        name=name,
        app=app,
        workspace=workspace,
        endpoint=endpoint,
    )
    try:
        payload = parse_json_object(path.read_text(encoding="utf-8"))
        record = ServePreviewRecord.from_payload(payload)
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _remove_preview_path(path)
        return None
    if (
        record.kind is not kind
        or record.name != name
        or record.app != app
        or record.workspace != workspace
        or record.endpoint != endpoint
        or record.expires_at <= time.time()
    ):
        _remove_preview_path(path)
        return None
    if client is not None and not _preview_container_active(record, client):
        _remove_preview_path(path)
        return None
    return record


def remove_serve_preview(record: ServePreviewRecord) -> None:
    _remove_preview_path(
        _preview_record_path(
            kind=record.kind,
            name=record.name,
            app=record.app,
            workspace=record.workspace,
            endpoint=record.endpoint,
        )
    )


def print_invocation_details(
    terminal: Terminal,
    *,
    url: str,
    authorized: bool,
    token_configured: bool,
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


def sync_local_workspace(
    *,
    container_id: str,
    local_dir: str,
    gateway_client: WorkspaceSyncClient,
    terminal: Terminal | None = None,
) -> None:
    ContainerWorkspaceSyncer(
        container_id=container_id,
        local_dir=local_dir,
        gateway_client=gateway_client,
        terminal=terminal,
    ).sync_once()


def _snapshot(local_dir: str) -> dict[str, FileState]:
    from lazycloud.source_sync import collect_source_files

    root = Path(local_dir).expanduser().resolve()
    files: dict[str, FileState] = {}
    for path in collect_source_files(root):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        files[path.relative_to(root).as_posix()] = FileState(
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            mode=stat.st_mode & 0o777,
        )
    return files


def _is_retryable_initial_sync_error(exc: Exception) -> bool:
    message = str(exc)
    return "worker address not published" in message or "Container not found" in message


def _preview_record_path(
    *,
    kind: DeploymentKind,
    name: str,
    app: str,
    workspace: str,
    endpoint: str,
) -> Path:
    key = json.dumps(
        {
            "app": app,
            "endpoint": endpoint.rstrip("/"),
            "kind": kind.value,
            "name": name,
            "workspace": workspace,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return state_home() / "serve-previews" / f"{digest}.json"


def _preview_container_active(
    record: ServePreviewRecord,
    client: PreviewContainerClient,
) -> bool:
    cursor = ""
    seen_cursors: set[str] = set()
    while True:
        try:
            response = client.list_containers(cursor=cursor or None)
        except Exception:
            # Treated as active so a listing outage never ends a live preview.
            LOGGER.debug("preview container listing failed", exc_info=True)
            return True
        for item in response.data:
            container = item.container
            if container.id == record.container_id:
                return container.status in ACTIVE_PREVIEW_CONTAINER_STATUSES
        if not response.next or response.next in seen_cursors:
            return False
        seen_cursors.add(response.next)
        cursor = response.next


def _remove_preview_path(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


__all__ = [
    "ContainerWorkspaceSyncer",
    "PreviewContainerClient",
    "ServeGatewayClient",
    "ServeOptions",
    "ServePreviewRecord",
    "ServePreviewSession",
    "ServePreviewUrl",
    "ServeResourceClient",
    "ServeUrlClient",
    "WorkspaceSyncClient",
    "print_invocation_details",
    "read_serve_preview",
    "remove_serve_preview",
    "resolve_serve_url",
    "sync_local_workspace",
    "write_serve_preview",
]
