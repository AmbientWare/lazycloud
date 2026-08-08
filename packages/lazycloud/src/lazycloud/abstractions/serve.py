from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from shared.bytes_transport import encode_bytes
from shared.containers import ContainerStatus
from shared.deployments import DeploymentKind
from shared.http.compute import ContainerResponse, ContainerWithAppPageResponse
from shared.http.gateway import (
    AttachToContainerResponse,
    ContainerWorkspaceSyncOperation,
    GatewayUrlKind,
    GetUrlRequest,
    GetUrlResponse,
    SyncContainerWorkspaceBody,
    SyncContainerWorkspaceResponse,
)
from shared.paths import state_home

from lazycloud.json_contracts import parse_json_object
from lazycloud.session.source_sync import collect_source_files
from lazycloud.terminal import Terminal
from lazycloud.transport_retry import (
    TRANSIENT_TRANSPORT_ERRORS,
    TransientRetry,
    is_transient_transport_error,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_ATTACH_POLL_SECONDS = 0.5
DEFAULT_SYNC_POLL_SECONDS = 0.5
DEFAULT_INITIAL_SYNC_TIMEOUT_SECONDS = 30.0
DEFAULT_PREVIEW_RECORD_TTL_SECONDS = 24 * 60 * 60
ACTIVE_PREVIEW_CONTAINER_STATUSES = {ContainerStatus.Pending, ContainerStatus.Running}


class ServeUrlClient(Protocol):
    def get_url(self, request: GetUrlRequest) -> GetUrlResponse: ...


class WorkspaceSyncClient(Protocol):
    def sync_container_workspace(
        self,
        body: SyncContainerWorkspaceBody,
    ) -> SyncContainerWorkspaceResponse: ...


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
    sync_poll_seconds: float = DEFAULT_SYNC_POLL_SECONDS
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
                poll_seconds=self.sync_poll_seconds,
                initial_sync_timeout_seconds=self.initial_sync_timeout_seconds,
                full_initial_sync=False,
            )
            self._syncer.start()
        try:
            self._attach_foreground(terminal)
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
    full_initial_sync: bool = True
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _snapshot: dict[str, FileState] = field(default_factory=dict, init=False, repr=False)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="serve-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        try:
            self._sync_initial_with_retries()
        except Exception as exc:
            self._warn(f"serve sync disabled: {exc}")
            return
        while not self._stop_event.wait(self.poll_seconds):
            try:
                next_snapshot = _snapshot(self.local_dir)
                self._sync_delta(next_snapshot)
                self._snapshot = next_snapshot
            except Exception as exc:
                self._warn(f"serve sync failed: {exc}")

    def _sync_initial_with_retries(self) -> None:
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
                if self._stop_event.wait(self.poll_seconds):
                    return

    def sync_once(self) -> None:
        self._snapshot = _snapshot(self.local_dir)
        self._sync_initial(self._snapshot)
        self._detail(f"Synced {len(self._snapshot)} files")

    def record_seed_snapshot(self) -> None:
        self._snapshot = _snapshot(self.local_dir)
        self._detail(f"Watching {len(self._snapshot)} files")

    def _sync_initial(self, snapshot: dict[str, FileState]) -> None:
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
                path=relative,
            )
        for relative in changed:
            self._write_file(relative)
        if removed or changed:
            self._detail(f"Synced {len(changed)} changed, {len(removed)} removed")

    def _write_file(self, relative: str) -> None:
        self._sync(
            operation=ContainerWorkspaceSyncOperation.Write,
            path=relative,
            data=(Path(self.local_dir) / relative).read_bytes(),
            mode=(Path(self.local_dir) / relative).stat().st_mode & 0o777,
        )

    def _sync(
        self,
        *,
        operation: ContainerWorkspaceSyncOperation,
        path: str,
        data: bytes = b"",
        mode: int = 0o644,
        new_path: str = "",
    ) -> None:
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


def resolve_serve_url(
    client: ServeUrlClient,
    *,
    stub_id: str,
    workspace: str | None = None,
    external_url: str,
) -> ServePreviewUrl:
    response = client.get_url(
        GetUrlRequest(
            stub_id=stub_id,
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
