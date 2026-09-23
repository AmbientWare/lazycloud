"""Durable disks on this worker: lease, attach, periodic publish, and release.

The disk engine (`lazycloud-disk`) owns the block device, its layer chain and the
object uploads. This module owns everything that involves the control plane:
the lease that fences writers, recording each published generation, and the
order that makes a publish safe to repeat. A generation is uploaded by the
engine, recorded with the control plane, and only then committed locally, so a
crash between any two steps is resumed rather than lost or doubled.

The lease survives this process. It is written beside the disk layers, which
live on a host directory that outlives the worker, so a restarted worker still
knows which leases it holds and gives them back.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from foundation.process import ProcessResult, run_command_with_timeout
from pydantic import Field
from shared.container_requests import RequestDisk
from shared.contracts import ContractModel
from shared.disks import DISK_FLATTEN_DEPTH, DISK_ROOT_MOUNT_PATH
from shared.http.errors import HttpApiError
from shared.identity import TokenKind

from worker.credential_payloads import WorkerCredentialPrincipal
from worker.durable_disk_records import (
    DiskAcquirePayload,
    DiskAcquireResult,
    DiskPublishPayload,
    DiskPublishResult,
    DiskReleasePayload,
)
from worker.events import ContainerRequestContext
from worker.execution import OciMount
from worker.tools import (
    ContainerCredentialRequest,
    ContainerCredentials,
    WorkspaceStorageCredentials,
)

LOGGER = logging.getLogger(__name__)

DISK_ENGINE_BINARY = "lazycloud-disk"
DEFAULT_DISK_ROOT = "/var/lib/lazycloud/disks"
DEFAULT_DISK_RUN_ROOT = "/run/lazycloud/disks"
DISK_LAYERS_DIR_NAME = "layers"
DISK_LEASES_DIR_NAME = "leases"
DISK_OVERLAY_DIR_NAME = "overlay"
DISK_PUBLISH_INTERVAL_SECONDS = 300.0
# The scheduler reclaims a container that has not started within its start
# deadline, and the restore that follows the wait counts against it too. The
# wait only has to outlast the previous holder's final publish.
DISK_ACQUIRE_DEADLINE_SECONDS = 180.0
DISK_ACQUIRE_INITIAL_BACKOFF_SECONDS = 2.0
DISK_ACQUIRE_MAX_BACKOFF_SECONDS = 30.0
_CONFLICT_STATUS = 409
_ATTACH_TIMEOUT_SECONDS = 3600.0
_PUBLISH_TIMEOUT_SECONDS = 3600.0
_SEAL_TIMEOUT_SECONDS = 300.0
_DETACH_TIMEOUT_SECONDS = 300.0
_RECOVER_TIMEOUT_SECONDS = 600.0
_COMMIT_TIMEOUT_SECONDS = 60.0


class DiskEngineError(RuntimeError):
    pass


class DiskLeaseClient(Protocol):
    def acquire_disk(self, payload: DiskAcquirePayload) -> DiskAcquireResult: ...

    def publish_disk(self, payload: DiskPublishPayload) -> DiskPublishResult: ...

    def release_disk(self, payload: DiskReleasePayload) -> None: ...


class DiskCredentialVendor(Protocol):
    def vend(
        self,
        request: ContainerCredentialRequest,
        *,
        principal: WorkerCredentialPrincipal,
    ) -> ContainerCredentials: ...


type DiskCommandRunner = Callable[[float, list[str]], ProcessResult]


class DiskEngineAttachResult(ContractModel):
    mountpoint: str
    generation: int
    restored_bytes: int = 0
    reused_local: bool = False


class DiskEngineSealResult(ContractModel):
    sealed: bool
    pending: int = Field(ge=0)


class DiskEnginePublishResult(ContractModel):
    manifest_key: str
    manifest_sha256: str
    stored_bytes_added: int = Field(ge=0)
    generation: int = Field(gt=0)
    parent_generation: int = Field(ge=0)


class DiskStoreFile(ContractModel):
    """STORE.json exactly as the engine reads it."""

    endpoint_url: str
    region: str
    bucket: str
    access_key: str = Field(repr=False)
    secret_key: str = Field(repr=False)
    session_token: str = Field(repr=False)
    force_path_style: bool


class DiskChainFileEntry(ContractModel):
    generation: int
    manifest_key: str
    manifest_sha256: str


@dataclass(slots=True)
class DiskEngine:
    """The `lazycloud-disk` command line; each call is one engine operation."""

    layers_root: Path
    run_root: Path
    binary: str = DISK_ENGINE_BINARY
    run_command: DiskCommandRunner = field(
        default=lambda timeout, argv: run_command_with_timeout(timeout, argv)
    )

    def attach(
        self,
        disk: RequestDisk,
        *,
        mountpoint: Path,
        chain: list[DiskChainFileEntry],
        store: DiskStoreFile,
    ) -> DiskEngineAttachResult:
        scratch = self._scratch_dir(disk.disk_id)
        chain_path = scratch / "chain.json"
        chain_path.write_text(
            json.dumps([entry.model_dump(mode="json") for entry in chain]), encoding="utf-8"
        )
        with self._store_file(scratch, store) as store_path:
            output = self._run(
                _ATTACH_TIMEOUT_SECONDS,
                "attach",
                *self._disk_args(disk.disk_id),
                "--size",
                str(disk.size_bytes),
                "--mountpoint",
                str(mountpoint),
                "--chain",
                str(chain_path),
                "--store",
                str(store_path),
            )
        chain_path.unlink(missing_ok=True)
        return DiskEngineAttachResult.model_validate_json(output)

    def seal(self, disk_id: str) -> DiskEngineSealResult:
        return DiskEngineSealResult.model_validate_json(
            self._run(_SEAL_TIMEOUT_SECONDS, "seal", *self._disk_args(disk_id))
        )

    def publish(
        self,
        disk_id: str,
        *,
        generation: int,
        parent: int,
        flatten: bool,
        store: DiskStoreFile,
    ) -> DiskEnginePublishResult:
        with self._store_file(self._scratch_dir(disk_id), store) as store_path:
            argv = [
                "publish",
                *self._disk_args(disk_id),
                "--store",
                str(store_path),
                "--generation",
                str(generation),
                "--parent",
                str(parent),
            ]
            if flatten:
                argv.append("--flatten")
            output = self._run(_PUBLISH_TIMEOUT_SECONDS, *argv)
        return DiskEnginePublishResult.model_validate_json(output)

    def commit_published(self, disk_id: str, *, generation: int) -> None:
        self._run(
            _COMMIT_TIMEOUT_SECONDS,
            "commit-published",
            *self._disk_args(disk_id),
            "--generation",
            str(generation),
        )

    def detach(self, disk_id: str) -> None:
        self._run(_DETACH_TIMEOUT_SECONDS, "detach", *self._disk_args(disk_id))

    def recover(self) -> None:
        self._run(_RECOVER_TIMEOUT_SECONDS, "recover", "--root", str(self.layers_root))

    def _disk_args(self, disk_id: str) -> list[str]:
        return ["--root", str(self.layers_root), "--disk", disk_id]

    def _scratch_dir(self, disk_id: str) -> Path:
        _require_path_segment(disk_id, field="disk id")
        path = self.run_root / disk_id
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def _store_file(self, directory: Path, store: DiskStoreFile) -> _StoreFile:
        return _StoreFile(directory / "store.json", store)

    def _run(self, timeout_seconds: float, *argv: str) -> str:
        command = [self.binary, *argv]
        result = self.run_command(timeout_seconds, command)
        if result.exit_code != 0:
            detail = (result.stderr or result.stdout).strip()
            raise DiskEngineError(f"{self.binary} {argv[0]} failed: {detail}")
        return result.stdout


class _StoreFile:
    """STORE.json for the length of one engine call, readable only by this user."""

    def __init__(self, path: Path, store: DiskStoreFile) -> None:
        self.path = path
        self.store = store

    def __enter__(self) -> Path:
        self.path.unlink(missing_ok=True)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(self.store.model_dump_json())
        return self.path

    def __exit__(self, *_: object) -> None:
        self.path.unlink(missing_ok=True)


class DiskLease(ContractModel):
    disk_id: str
    name: str
    mount_path: str
    size_bytes: int
    lease_token: str = Field(repr=False)
    generation: int = 0
    """Newest generation the control plane recorded for this lease."""

    chain_depth: int = 0
    """Published layers since the newest self-contained one."""

    mountpoint: str = ""
    released: bool = False


class ContainerDiskLeases(ContractModel):
    container_id: str
    workspace_id: str
    stub_id: str
    disks: list[DiskLease] = Field(default_factory=list)


class DurableDiskAttachment(ContractModel):
    root_upper: str = ""
    """Directory on the root disk holding the overlay's upper and work directories."""

    oci_mounts: list[OciMount] = Field(default_factory=list)


@dataclass(slots=True)
class _Attached:
    leases: ContainerDiskLeases
    lock: threading.Lock = field(default_factory=threading.Lock)
    stop: threading.Event = field(default_factory=threading.Event)
    publisher: threading.Thread | None = None
    store: DiskStoreFile | None = None
    store_expires_at: datetime | None = None


@dataclass(slots=True)
class WorkerDurableDiskService:
    """Owns every disk lease this worker holds, for as long as it holds them."""

    engine: DiskEngine
    leases: DiskLeaseClient
    credentials: DiskCredentialVendor
    lease_root: Path
    mount_root: Path
    publish_interval_seconds: float = DISK_PUBLISH_INTERVAL_SECONDS
    acquire_deadline_seconds: float = DISK_ACQUIRE_DEADLINE_SECONDS
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    _attached: dict[str, _Attached] = field(default_factory=dict)
    _attached_lock: threading.Lock = field(default_factory=threading.Lock)

    def recover(self) -> None:
        """Stop what a previous worker process left attached; run before any attach."""
        self.lease_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.engine.layers_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.engine.recover()

    def attach(self, request: ContainerRequestContext) -> DurableDiskAttachment:
        if not request.disks:
            return DurableDiskAttachment()
        _require_path_segment(request.container_id, field="container id")
        record = self._load(request.container_id) or ContainerDiskLeases(
            container_id=request.container_id,
            workspace_id=request.workspace_id,
            stub_id=request.stub_id,
        )
        attached = _Attached(leases=record)
        with self._attached_lock:
            self._attached[request.container_id] = attached
        attachment = DurableDiskAttachment()
        with attached.lock:
            for disk in request.disks:
                lease, chain = self._acquire(attached, disk)
                mountpoint = self.mount_root / request.container_id / disk.disk_id
                mountpoint.mkdir(parents=True, exist_ok=True)
                result = self.engine.attach(
                    disk,
                    mountpoint=mountpoint,
                    chain=chain,
                    store=self._store(attached),
                )
                lease.mountpoint = result.mountpoint
                self._save(record)
                LOGGER.info(
                    "disk %s attached for container %s at generation %d "
                    "(restored %d bytes, reused local layers: %s)",
                    disk.name,
                    request.container_id,
                    result.generation,
                    result.restored_bytes,
                    result.reused_local,
                )
                if disk.mount_path == DISK_ROOT_MOUNT_PATH:
                    upper_root = Path(result.mountpoint) / DISK_OVERLAY_DIR_NAME
                    upper_root.mkdir(parents=True, exist_ok=True)
                    attachment.root_upper = str(upper_root)
                else:
                    attachment.oci_mounts.append(
                        OciMount(
                            source=result.mountpoint,
                            destination=disk.mount_path,
                            options=["rbind", "rw", "rprivate", "nosuid", "nodev"],
                        )
                    )
        attached.publisher = threading.Thread(
            target=self._publish_periodically,
            args=(attached,),
            name=f"disk-publish-{request.container_id}",
            daemon=True,
        )
        attached.publisher.start()
        return attachment

    def release(self, container_id: str) -> None:
        """Publish what the container wrote and give every lease back.

        Raises on any failure so finalization retries it: the lease holds the
        disk for this container until the final publish lands or the release is
        recorded, which is what keeps the next container from starting on a
        generation this one was about to replace.
        """
        with self._attached_lock:
            attached = self._attached.get(container_id)
        live = attached is not None
        if attached is None:
            record = self._load(container_id)
            if record is None:
                return
            attached = _Attached(leases=record)
        attached.stop.set()
        publisher = attached.publisher
        if publisher is not None and publisher is not threading.current_thread():
            publisher.join()
        with attached.lock:
            failures: list[Exception] = []
            for lease in attached.leases.disks:
                if lease.released:
                    continue
                try:
                    if live and lease.mountpoint:
                        self._publish_pending(attached, lease, final=True)
                    else:
                        # A previous worker process attached this disk and its
                        # daemon is gone, so nothing can be sealed. Sealed layers
                        # and the head stay in the local cache for the next attach
                        # on this node.
                        self.leases.release_disk(
                            DiskReleasePayload(
                                container_id=container_id,
                                disk_id=lease.disk_id,
                                lease_token=lease.lease_token,
                            )
                        )
                    lease.released = True
                    self._save(attached.leases)
                    self.engine.detach(lease.disk_id)
                except Exception as exc:
                    LOGGER.exception(
                        "releasing disk %s for container %s failed; it will be retried",
                        lease.name,
                        container_id,
                    )
                    failures.append(exc)
            if failures:
                raise ExceptionGroup(f"disk release for container {container_id} failed", failures)
            for lease in attached.leases.disks:
                shutil.rmtree(self.mount_root / container_id / lease.disk_id, ignore_errors=True)
            shutil.rmtree(self.mount_root / container_id, ignore_errors=True)
            self._lease_path(container_id).unlink(missing_ok=True)
        with self._attached_lock:
            self._attached.pop(container_id, None)

    def _acquire(
        self, attached: _Attached, disk: RequestDisk
    ) -> tuple[DiskLease, list[DiskChainFileEntry]]:
        record = attached.leases
        deadline = self.monotonic() + self.acquire_deadline_seconds
        backoff = DISK_ACQUIRE_INITIAL_BACKOFF_SECONDS
        while True:
            try:
                result = self.leases.acquire_disk(
                    DiskAcquirePayload(container_id=record.container_id, disk_id=disk.disk_id)
                )
                break
            except HttpApiError as exc:
                remaining = deadline - self.monotonic()
                if exc.status_code != _CONFLICT_STATUS or remaining <= 0:
                    raise
                wait = min(backoff, remaining)
                LOGGER.warning(
                    "disk %s for container %s is still held (%s); retrying in %.0fs",
                    disk.name,
                    record.container_id,
                    exc,
                    wait,
                )
                self.sleep(wait)
                backoff = min(backoff * 2, DISK_ACQUIRE_MAX_BACKOFF_SECONDS)
        if result.size_bytes != disk.size_bytes:
            raise DiskEngineError(
                f"disk {disk.name} is {result.size_bytes} bytes but the request names "
                f"{disk.size_bytes}"
            )
        lease = DiskLease(
            disk_id=disk.disk_id,
            name=disk.name,
            mount_path=disk.mount_path,
            size_bytes=result.size_bytes,
            lease_token=result.lease_token,
            generation=result.generation,
            chain_depth=len(result.chain),
        )
        record.disks = [item for item in record.disks if item.disk_id != disk.disk_id]
        record.disks.append(lease)
        self._save(record)
        return lease, [
            DiskChainFileEntry(
                generation=layer.generation,
                manifest_key=layer.manifest_key,
                manifest_sha256=layer.manifest_sha256,
            )
            for layer in result.chain
        ]

    def _publish_periodically(self, attached: _Attached) -> None:
        while not attached.stop.wait(self.publish_interval_seconds):
            with attached.lock:
                if attached.stop.is_set():
                    return
                for lease in attached.leases.disks:
                    if lease.released or not lease.mountpoint:
                        continue
                    try:
                        self._publish_pending(attached, lease, final=False)
                    except Exception:
                        LOGGER.exception(
                            "periodic publish of disk %s for container %s failed; "
                            "the next pass retries it",
                            lease.name,
                            attached.leases.container_id,
                        )

    def _publish_pending(self, attached: _Attached, lease: DiskLease, *, final: bool) -> None:
        sealed = self.engine.seal(lease.disk_id)
        pending = sealed.pending
        if pending == 0:
            if final:
                self.leases.release_disk(
                    DiskReleasePayload(
                        container_id=attached.leases.container_id,
                        disk_id=lease.disk_id,
                        lease_token=lease.lease_token,
                    )
                )
            return
        for index in range(pending):
            generation = lease.generation + 1
            flatten = lease.chain_depth >= DISK_FLATTEN_DEPTH
            uploaded = self.engine.publish(
                lease.disk_id,
                generation=generation,
                parent=lease.generation,
                flatten=flatten,
                store=self._store(attached),
            )
            self.leases.publish_disk(
                DiskPublishPayload(
                    container_id=attached.leases.container_id,
                    disk_id=lease.disk_id,
                    lease_token=lease.lease_token,
                    generation=uploaded.generation,
                    parent_generation=uploaded.parent_generation,
                    manifest_key=uploaded.manifest_key,
                    manifest_sha256=uploaded.manifest_sha256,
                    stored_bytes_added=uploaded.stored_bytes_added,
                    final=final and index == pending - 1,
                )
            )
            self.engine.commit_published(lease.disk_id, generation=uploaded.generation)
            lease.generation = uploaded.generation
            lease.chain_depth = 1 if uploaded.parent_generation == 0 else lease.chain_depth + 1
            self._save(attached.leases)
            LOGGER.info(
                "disk %s published generation %d (%d new bytes%s)",
                lease.name,
                uploaded.generation,
                uploaded.stored_bytes_added,
                ", flattened" if uploaded.parent_generation == 0 else "",
            )

    def _store(self, attached: _Attached) -> DiskStoreFile:
        """Workspace bucket credentials, vended fresh and kept for when vending fails.

        The last vended credential is still a real one for the rest of its life,
        so a release whose container state has already expired can still publish.
        """
        record = attached.leases
        try:
            vended = self.credentials.vend(
                ContainerCredentialRequest(
                    workspace_id=record.workspace_id,
                    stub_id=record.stub_id,
                    container_id=record.container_id,
                    workspace_storage=True,
                ),
                principal=WorkerCredentialPrincipal(
                    workspace_id=record.workspace_id, token_kind=TokenKind.Worker
                ),
            )
        except Exception:
            if attached.store is not None and (
                attached.store_expires_at is None or attached.store_expires_at > datetime.now(UTC)
            ):
                LOGGER.warning(
                    "workspace storage credential for container %s could not be renewed; "
                    "using the unexpired one",
                    record.container_id,
                    exc_info=True,
                )
                return attached.store
            raise
        if vended.workspace_storage is None:
            raise DiskEngineError("workspace storage credentials were not vended for the disk")
        attached.store = disk_store_file(vended.workspace_storage)
        attached.store_expires_at = vended.workspace_storage.expires_at
        return attached.store

    def _lease_path(self, container_id: str) -> Path:
        _require_path_segment(container_id, field="container id")
        return self.lease_root / f"{container_id}.json"

    def _load(self, container_id: str) -> ContainerDiskLeases | None:
        path = self._lease_path(container_id)
        if not path.exists():
            return None
        return ContainerDiskLeases.model_validate_json(path.read_text(encoding="utf-8"))

    def _save(self, record: ContainerDiskLeases) -> None:
        self.lease_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._lease_path(record.container_id)
        staging = path.with_suffix(".tmp")
        staging.unlink(missing_ok=True)
        descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(record.model_dump_json())
            handle.flush()
            os.fsync(handle.fileno())
        staging.replace(path)


def disk_store_file(credentials: WorkspaceStorageCredentials) -> DiskStoreFile:
    if credentials.prefix:
        # Disk keys are the shared layout under the bucket root; a store scoped to
        # a prefix would put them somewhere the control plane never deletes.
        raise DiskEngineError("durable disks require workspace storage without a key prefix")
    return DiskStoreFile(
        endpoint_url=credentials.endpoint_url,
        region=credentials.region,
        bucket=credentials.bucket_name,
        access_key=credentials.access_key,
        secret_key=credentials.secret_key,
        session_token=credentials.session_token,
        force_path_style=credentials.force_path_style,
    )


def disk_layout(root: Path) -> tuple[Path, Path]:
    """Engine layer root and lease directory under the host disk directory."""
    return root / DISK_LAYERS_DIR_NAME, root / DISK_LEASES_DIR_NAME


def _require_path_segment(value: str, *, field: str) -> None:
    if not value or value in {".", ".."} or "/" in value or value.startswith("."):
        raise DiskEngineError(f"unsafe {field}: {value!r}")


__all__ = [
    "DEFAULT_DISK_ROOT",
    "DEFAULT_DISK_RUN_ROOT",
    "DISK_ENGINE_BINARY",
    "ContainerDiskLeases",
    "DiskEngine",
    "DiskEngineError",
    "DiskLease",
    "DiskLeaseClient",
    "DurableDiskAttachment",
    "WorkerDurableDiskService",
    "disk_layout",
    "disk_store_file",
]
