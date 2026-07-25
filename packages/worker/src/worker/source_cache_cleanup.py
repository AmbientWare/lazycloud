from __future__ import annotations

import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import Field
from shared.contracts import ContractModel
from shared.source_cache_cleanup import (
    SourceCacheCleanupTargetRecord,
    WorkerCacheStorageOwnerRecord,
)

from worker.source_code import SourceCodePackageMaterializer

SOURCE_CACHE_GENERATION_MARKER = ".lazycloud-cache-generation"
SOURCE_CACHE_SESSION_MARKER = ".lazycloud-cache-session"
DEFAULT_SOURCE_CACHE_CLAIM_LIMIT = 128


class WorkerSourceCacheIdentity(ContractModel):
    generation_id: str
    storage_id: str

    @classmethod
    def open(
        cls,
        cache_root: Path,
        *,
        storage_id: str = "",
    ) -> WorkerSourceCacheIdentity:
        try:
            storage_owner = WorkerCacheStorageOwnerRecord.from_storage_id(storage_id)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        root = cache_root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        marker = root / SOURCE_CACHE_GENERATION_MARKER
        generation_id = _open_generation_marker(root, marker)
        try:
            generation_id = str(UUID(generation_id))
        except ValueError as exc:
            raise RuntimeError(f"invalid source cache generation marker: {marker}") from exc
        return cls(
            generation_id=generation_id,
            storage_id=storage_owner.storage_id,
        )


class WorkerSourceCacheSessionMarker(ContractModel):
    generation_id: str
    storage_id: str
    session_fence: int = Field(ge=1)


class WorkerSourceCacheDestructionReceipt(ContractModel):
    generation_id: str
    storage_id: str
    session_fence: int = Field(ge=1)


def record_source_cache_session(
    cache_root: Path,
    identity: WorkerSourceCacheIdentity,
    *,
    session_fence: int,
) -> None:
    if session_fence < 1:
        raise ValueError("source cache session fence must be positive")
    root = _safe_cache_root(cache_root)
    generation_id = _read_generation_marker(root / SOURCE_CACHE_GENERATION_MARKER)
    if generation_id != identity.generation_id:
        raise RuntimeError("source cache generation changed before session binding")
    marker = root / SOURCE_CACHE_SESSION_MARKER
    payload = WorkerSourceCacheSessionMarker(
        generation_id=identity.generation_id,
        storage_id=identity.storage_id,
        session_fence=session_fence,
    ).model_dump_json()
    _write_marker_atomic(root, marker, payload)


def source_cache_destruction_receipt(
    cache_root: Path,
    *,
    storage_id: str,
) -> WorkerSourceCacheDestructionReceipt | None:
    try:
        cache_root.lstat()
    except FileNotFoundError:
        return None
    root = _safe_cache_root(cache_root)
    generation_id = _read_generation_marker(root / SOURCE_CACHE_GENERATION_MARKER)
    if generation_id is None:
        raise RuntimeError("source cache generation marker is missing")
    try:
        generation_id = str(UUID(generation_id))
    except ValueError as exc:
        raise RuntimeError("source cache generation marker is invalid") from exc
    session_marker = _read_session_marker(root / SOURCE_CACHE_SESSION_MARKER)
    if session_marker.generation_id != generation_id or session_marker.storage_id != storage_id:
        raise RuntimeError("source cache session does not match the machine-owned cache")
    return WorkerSourceCacheDestructionReceipt(
        generation_id=generation_id,
        storage_id=storage_id,
        session_fence=session_marker.session_fence,
    )


def destroy_source_cache_storage(
    cache_root: Path,
    receipt: WorkerSourceCacheDestructionReceipt,
) -> None:
    current = source_cache_destruction_receipt(
        cache_root,
        storage_id=receipt.storage_id,
    )
    if current is None:
        return
    if current != receipt:
        raise RuntimeError("source cache session changed before storage destruction")
    root = _safe_cache_root(cache_root)
    shutil.rmtree(root)
    if root.exists():
        raise RuntimeError(f"source cache storage still exists after removal: {root}")
    parent_descriptor = os.open(root.parent, os.O_RDONLY)
    try:
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


class WorkerSourceCacheClaimSource(ContractModel):
    targets: list[SourceCacheCleanupTargetRecord]


class WorkerSourceCacheReconcileResult(ContractModel):
    claimed_count: int = 0
    completed_count: int = 0
    failed_count: int = 0


class WorkerSourceCacheRepository(Protocol):
    def claim_source_cache_cleanup(
        self,
        *,
        limit: int,
    ) -> WorkerSourceCacheClaimSource: ...

    def complete_source_cache_cleanup(
        self,
        target: SourceCacheCleanupTargetRecord,
    ) -> None: ...

    def fail_source_cache_cleanup(
        self,
        target: SourceCacheCleanupTargetRecord,
    ) -> None: ...

    def activate_source_cache(self) -> None: ...


@dataclass(slots=True)
class WorkerSourceCacheReconciler:
    repository: WorkerSourceCacheRepository
    materializer: SourceCodePackageMaterializer
    claim_limit: int = DEFAULT_SOURCE_CACHE_CLAIM_LIMIT

    def reconcile(self) -> WorkerSourceCacheReconcileResult:
        claimed_count = 0
        completed_count = 0
        failed_count = 0
        while True:
            claimed = self.repository.claim_source_cache_cleanup(limit=self.claim_limit).targets
            if not claimed:
                break
            claimed_count += len(claimed)
            for target in claimed:
                try:
                    self.materializer.purge(
                        target.workspace_id,
                        [target.source_object_id],
                    )
                    self.repository.complete_source_cache_cleanup(target)
                    completed_count += 1
                except Exception:
                    self.repository.fail_source_cache_cleanup(target)
                    failed_count += 1
        if failed_count == 0:
            self.repository.activate_source_cache()
        return WorkerSourceCacheReconcileResult(
            claimed_count=claimed_count,
            completed_count=completed_count,
            failed_count=failed_count,
        )


def _open_generation_marker(root: Path, marker: Path) -> str:
    existing = _read_generation_marker(marker)
    if existing is not None:
        return existing
    unexpected_entries = [
        entry
        for entry in root.iterdir()
        if entry.name != SOURCE_CACHE_GENERATION_MARKER
        and not entry.name.startswith(f"{SOURCE_CACHE_GENERATION_MARKER}.")
    ]
    if unexpected_entries:
        raise RuntimeError(
            f"source cache generation marker is missing from non-empty cache root: {root}"
        )
    generation_id = str(uuid4())
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f"{SOURCE_CACHE_GENERATION_MARKER}.",
        dir=root,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(generation_id)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, marker)
            directory_descriptor = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
            return generation_id
        except FileExistsError:
            installed = _read_generation_marker(marker)
            if installed is None:
                raise RuntimeError(
                    f"source cache generation marker disappeared: {marker}"
                ) from None
            return installed
    finally:
        temporary.unlink(missing_ok=True)


def _safe_cache_root(cache_root: Path) -> Path:
    root = cache_root.expanduser().absolute()
    try:
        status = root.lstat()
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise RuntimeError(f"invalid source cache root: {root}")
    return root


def _read_session_marker(marker: Path) -> WorkerSourceCacheSessionMarker:
    raw = _read_secure_marker(marker)
    if raw is None:
        raise RuntimeError(f"source cache session marker is missing: {marker}")
    try:
        return WorkerSourceCacheSessionMarker.model_validate_json(raw)
    except ValueError as exc:
        raise RuntimeError(f"invalid source cache session marker: {marker}") from exc


def _write_marker_atomic(root: Path, marker: Path, contents: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f"{marker.name}.",
        dir=root,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, marker)
        directory_descriptor = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _read_generation_marker(marker: Path) -> str | None:
    return _read_secure_marker(marker)


def _read_secure_marker(marker: Path) -> str | None:
    try:
        marker_status = marker.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(marker_status.st_mode) or not stat.S_ISREG(marker_status.st_mode):
        raise RuntimeError(f"invalid source cache generation marker: {marker}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(marker, flags)
    except OSError as exc:
        raise RuntimeError(f"invalid source cache generation marker: {marker}") from exc
    try:
        opened_status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_status.st_mode)
            or opened_status.st_dev != marker_status.st_dev
            or opened_status.st_ino != marker_status.st_ino
        ):
            raise RuntimeError(f"invalid source cache generation marker: {marker}")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            return stream.read().strip()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


__all__ = [
    "DEFAULT_SOURCE_CACHE_CLAIM_LIMIT",
    "SOURCE_CACHE_GENERATION_MARKER",
    "SOURCE_CACHE_SESSION_MARKER",
    "WorkerSourceCacheClaimSource",
    "WorkerSourceCacheDestructionReceipt",
    "WorkerSourceCacheIdentity",
    "WorkerSourceCacheReconcileResult",
    "WorkerSourceCacheReconciler",
    "WorkerSourceCacheRepository",
    "WorkerSourceCacheSessionMarker",
    "destroy_source_cache_storage",
    "record_source_cache_session",
    "source_cache_destruction_receipt",
]
