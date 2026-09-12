from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from shared.bytes_transport import encode_bytes
from shared.deployments import DeploymentKind
from shared.http.errors import HttpApiError
from shared.http.gateway import (
    AttachToContainerResponse,
    ContainerWorkspaceSyncOperation,
    SyncContainerWorkspaceBody,
    SyncContainerWorkspaceResponse,
)
from shared.http.previews import (
    PREVIEW_HEARTBEAT_SECONDS,
    PreviewSessionResponse,
    PreviewSessionStatus,
)
from shared.paths import state_home
from shared.transport_retry import (
    TRANSIENT_TRANSPORT_ERRORS,
    TransientRetry,
    is_transient_transport_error,
)

from lazycloud.json_contracts import parse_json_object
from lazycloud.session.source_sync import SourcePackageSyncResult
from lazycloud.terminal import Terminal

DEFAULT_ATTACH_POLL_SECONDS = 0.5
DEFAULT_SYNC_POLL_SECONDS = 0.5
DEFAULT_INITIAL_SYNC_TIMEOUT_SECONDS = 30.0


class PreviewSessionReader(Protocol):
    def get_preview(self, preview_id: str) -> PreviewSessionResponse: ...


class PreviewLifecycleClient(PreviewSessionReader, Protocol):
    def create(self, stub_id: str, *, timeout: int = 0) -> PreviewSessionResponse: ...

    def renew(self, preview_id: str) -> PreviewSessionResponse: ...

    def stop(self, preview_id: str) -> None: ...


class WorkspaceSyncClient(Protocol):
    def sync_container_workspace(
        self,
        body: SyncContainerWorkspaceBody,
    ) -> SyncContainerWorkspaceResponse: ...


class ServeSessionClient(WorkspaceSyncClient, Protocol):
    def attach_to_container_events(
        self,
        container_id: str,
        *,
        poll_interval_seconds: float = DEFAULT_ATTACH_POLL_SECONDS,
    ) -> Iterator[AttachToContainerResponse]: ...


class ServeGatewayClient(ServeSessionClient, Protocol):
    pass


@dataclass(frozen=True, slots=True)
class ServePreviewRecord:
    kind: DeploymentKind
    name: str
    app: str
    workspace: str
    endpoint: str
    preview_id: str
    stub_id: str
    container_id: str
    url: str
    created_at: float
    expires_at: float | None

    def payload(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "name": self.name,
            "app": self.app,
            "workspace": self.workspace,
            "endpoint": self.endpoint,
            "preview_id": self.preview_id,
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
            preview_id=str(payload["preview_id"]),
            stub_id=str(payload["stub_id"]),
            container_id=str(payload["container_id"]),
            url=str(payload["url"]),
            created_at=_payload_float(payload, "created_at"),
            expires_at=_payload_float(payload, "expires_at")
            if payload["expires_at"] is not None
            else None,
        )


def _payload_float(payload: Mapping[str, object], key: str) -> float:
    value = payload[key]
    if isinstance(value, int | float | str):
        return float(value)
    msg = f"preview record field {key!r} must be numeric"
    raise TypeError(msg)


@dataclass(slots=True)
class ServePreviewSession:
    preview_id: str
    stub_id: str
    container_id: str
    url: str
    gateway_client: ServeSessionClient
    preview_client: PreviewLifecycleClient
    terminal: Terminal | None = None
    source: SourcePackageSyncResult | None = None
    token: str | None = None
    authorized: bool = True
    attach_poll_seconds: float = DEFAULT_ATTACH_POLL_SECONDS
    sync_poll_seconds: float = DEFAULT_SYNC_POLL_SECONDS
    initial_sync_timeout_seconds: float = DEFAULT_INITIAL_SYNC_TIMEOUT_SECONDS
    preview_record: ServePreviewRecord | None = None
    _syncer: ContainerWorkspaceSyncer | None = field(default=None, init=False, repr=False)
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)

    def run(self) -> None:
        terminal = self.terminal or Terminal()
        heartbeat = threading.Thread(
            target=self._renew_lease, args=(terminal,), name="preview-heartbeat", daemon=True
        )
        try:
            heartbeat.start()
            print_invocation_details(
                terminal,
                url=self.url,
                authorized=self.authorized,
                token_configured=bool(self.token),
            )
            if self.source is not None:
                self._syncer = ContainerWorkspaceSyncer(
                    container_id=self.container_id,
                    local_dir=str(self.source.root),
                    archive_prefix=self.source.archive_prefix,
                    seed_files=self.source.files,
                    gateway_client=self.gateway_client,
                    terminal=terminal,
                    poll_seconds=self.sync_poll_seconds,
                    initial_sync_timeout_seconds=self.initial_sync_timeout_seconds,
                )
                self._syncer.start()
            self._attach_foreground(terminal)
        except KeyboardInterrupt:
            terminal.header("Stopping preview")
        finally:
            self._stop_event.set()
            if self._syncer is not None:
                self._syncer.stop()
            if heartbeat.ident is not None:
                heartbeat.join()
            self._stop_with_warning(terminal)
            if self.preview_record is not None:
                remove_serve_preview(self.preview_record)

    def stop(self) -> None:
        self.preview_client.stop(self.preview_id)

    def _renew_lease(self, terminal: Terminal) -> None:
        while not self._stop_event.wait(PREVIEW_HEARTBEAT_SECONDS):
            try:
                self.preview_client.renew(self.preview_id)
            except Exception as exc:
                terminal.warn(f"preview heartbeat failed: {exc}")

    def _stop_with_warning(self, terminal: Terminal) -> None:
        try:
            self.stop()
        except Exception as exc:
            terminal.warn(f"failed to stop preview: {exc}")

    def _attach_foreground(self, terminal: Terminal) -> None:
        self._attach_stream(terminal)

    def _attach_stream(self, terminal: Terminal) -> None:
        retry = TransientRetry()
        attach_timeout_reported = False
        reconnect_reported = False
        while True:
            try:
                for response in self.gateway_client.attach_to_container_events(
                    self.container_id,
                    poll_interval_seconds=self.attach_poll_seconds,
                ):
                    retry.reset()
                    attach_timeout_reported = False
                    reconnect_reported = False
                    if response.error_msg:
                        terminal.error(response.error_msg)
                    if response.output:
                        terminal.write(response.output)
                    if response.done:
                        if response.exit_code:
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
    poll_seconds: float = DEFAULT_SYNC_POLL_SECONDS
    initial_sync_timeout_seconds: float = DEFAULT_INITIAL_SYNC_TIMEOUT_SECONDS
    archive_prefix: tuple[str, ...] = ()
    seed_files: tuple[str, ...] = ()
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _snapshot: dict[str, FileState] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.local_dir = str(Path(self.local_dir).expanduser().resolve())

    def start(self) -> None:
        if self._thread is not None:
            return
        self._sync_initial_with_retries()
        self._thread = threading.Thread(target=self._run, name="serve-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join()

    def _run(self) -> None:
        while not self._stop_event.wait(self.poll_seconds):
            try:
                next_snapshot = _snapshot(self.local_dir)
                self._sync_delta(next_snapshot)
                self._snapshot = next_snapshot
            except Exception as exc:
                self._warn(f"serve sync failed: {exc}")

    def _sync_initial_with_retries(self) -> None:
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
                if self._stop_event.wait(self.poll_seconds):
                    return

    def sync_once(self) -> None:
        self._snapshot = _snapshot(self.local_dir)
        self._sync_initial(self._snapshot)
        self._detail(f"Synced {len(self._snapshot)} files")

    def _sync_initial(self, snapshot: dict[str, FileState]) -> None:
        removed = set(self.seed_files) - {self._remote_path(relative) for relative in snapshot}
        for path in sorted(removed):
            self._sync(operation=ContainerWorkspaceSyncOperation.Delete, path=path)
        for relative in sorted(snapshot):
            self._write_file(relative)

    def _sync_delta(self, next_snapshot: dict[str, FileState]) -> None:
        removed = sorted(set(self._snapshot) - set(next_snapshot))
        changed = [
            relative
            for relative, state in sorted(next_snapshot.items())
            if self._snapshot.get(relative) != state
        ]
        for relative in removed:
            self._sync(
                operation=ContainerWorkspaceSyncOperation.Delete,
                path=self._remote_path(relative),
            )
        for relative in changed:
            self._write_file(relative)
        if removed or changed:
            self._detail(f"Synced {len(changed)} changed, {len(removed)} removed")

    def _write_file(self, relative: str) -> None:
        if self._stop_event.is_set():
            return
        self._sync(
            operation=ContainerWorkspaceSyncOperation.Write,
            path=self._remote_path(relative),
            data=(Path(self.local_dir) / relative).read_bytes(),
            mode=(Path(self.local_dir) / relative).stat().st_mode & 0o777,
        )

    def _remote_path(self, relative: str) -> str:
        return "/".join((*self.archive_prefix, relative))

    def _sync(
        self,
        *,
        operation: ContainerWorkspaceSyncOperation,
        path: str,
        data: bytes = b"",
        mode: int = 0o644,
        new_path: str = "",
    ) -> None:
        if self._stop_event.is_set():
            return
        self.gateway_client.sync_container_workspace(
            SyncContainerWorkspaceBody(
                container_id=self.container_id,
                operation=operation,
                path=path,
                new_path=new_path,
                mode=mode,
                value_base64=encode_bytes(data),
            )
        )

    def _detail(self, message: str) -> None:
        if self.terminal is not None:
            self.terminal.detail(message)

    def _warn(self, message: str) -> None:
        if self.terminal is not None:
            self.terminal.warn(message)


@dataclass(frozen=True, slots=True)
class FileState:
    size: int
    mtime_ns: int


def write_serve_preview(
    *,
    kind: DeploymentKind,
    name: str,
    app: str,
    workspace: str,
    endpoint: str,
    preview_id: str,
    stub_id: str,
    container_id: str,
    url: str,
    expires_at: float | None = None,
) -> ServePreviewRecord:
    record = ServePreviewRecord(
        kind=kind,
        name=name,
        app=app,
        workspace=workspace,
        endpoint=endpoint,
        preview_id=preview_id,
        stub_id=stub_id,
        container_id=container_id,
        url=url,
        created_at=time.time(),
        expires_at=expires_at,
    )
    directory = _preview_record_path(
        kind=kind, name=name, app=app, workspace=workspace, endpoint=endpoint
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{preview_id}.json"
    temp_path = path.with_suffix(".tmp")
    try:
        temp_path.write_text(json.dumps(record.payload(), sort_keys=True), encoding="utf-8")
        temp_path.replace(path)
    finally:
        _remove_preview_path(temp_path)
    return record


def read_serve_preview(
    *,
    kind: DeploymentKind,
    name: str,
    app: str,
    workspace: str,
    endpoint: str,
    client: PreviewSessionReader,
) -> ServePreviewRecord | None:
    directory = _preview_record_path(
        kind=kind, name=name, app=app, workspace=workspace, endpoint=endpoint
    )
    records: list[ServePreviewRecord] = []
    for path in directory.glob("*.json"):
        try:
            record = ServePreviewRecord.from_payload(
                parse_json_object(path.read_text(encoding="utf-8"))
            )
        except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            _remove_preview_path(path)
            continue
        if (
            record.kind is not kind
            or record.name != name
            or record.app != app
            or record.workspace != workspace
            or record.endpoint != endpoint
            or path.name != f"{record.preview_id}.json"
        ):
            _remove_preview_path(path)
            continue
        records.append(record)
    for record in sorted(records, key=lambda item: item.created_at, reverse=True):
        try:
            session = client.get_preview(record.preview_id)
        except HttpApiError as exc:
            if exc.status_code != 404:
                raise
            remove_serve_preview(record)
            continue
        if (
            session.status is not PreviewSessionStatus.Active
            or session.container_id != record.container_id
            or session.execution_stub_id != record.stub_id
        ):
            remove_serve_preview(record)
            continue
        return record
    return None


def remove_serve_preview(record: ServePreviewRecord) -> None:
    directory = _preview_record_path(
        kind=record.kind,
        name=record.name,
        app=record.app,
        workspace=record.workspace,
        endpoint=record.endpoint,
    )
    _remove_preview_path(directory / f"{record.preview_id}.json")


def print_invocation_details(
    terminal: Terminal,
    *,
    url: str,
    authorized: bool,
    token_configured: bool,
) -> None:
    terminal.header("Invocation details")
    terminal.line("")
    terminal.line(f"curl -X POST '{url}' \\")
    terminal.line("-H 'Accept: */*' \\")
    if authorized:
        token = "[YOUR_AUTH_TOKEN]" if token_configured else "[TOKEN]"
        terminal.line(f"-H 'Authorization: Bearer {token}' \\")
    terminal.line("-H 'Content-Type: application/json' \\")
    terminal.line("-d '{}'")
    terminal.line("")
    terminal.header("Serving")


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
    from lazycloud.source_files import collect_source_files

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
    return state_home() / "serve-previews" / digest


def _remove_preview_path(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


__all__ = [
    "ContainerWorkspaceSyncer",
    "PreviewLifecycleClient",
    "PreviewSessionReader",
    "ServeGatewayClient",
    "ServePreviewRecord",
    "ServePreviewSession",
    "WorkspaceSyncClient",
    "print_invocation_details",
    "read_serve_preview",
    "remove_serve_preview",
    "sync_local_workspace",
    "write_serve_preview",
]
