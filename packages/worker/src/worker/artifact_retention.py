from __future__ import annotations

import shutil
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from pydantic import Field, field_validator, model_validator
from shared.contracts import ContractModel
from shared.timestamps import utc_now

from worker.checkpoint_activity import CheckpointArtifactLeaseRegistry
from worker.container_service.models import WorkerContainerServiceInstance
from worker.execution import CHECKPOINT_ARCHIVE_EXTENSION
from worker.image_build_scratch import ImageBuildScratchManager
from worker.image_lifecycle import LOCAL_IMAGE_ARCHIVE_EXTENSION

DEFAULT_WORKER_ARTIFACT_RETENTION_INTERVAL_SECONDS = 5 * 60
DEFAULT_WORKER_IMAGE_CACHE_MAX_BYTES = 20 * 1024 * 1024 * 1024
DEFAULT_WORKER_IMAGE_MATERIALIZATION_MAX_BYTES = 40 * 1024 * 1024 * 1024
DEFAULT_WORKER_CHECKPOINT_CACHE_MAX_BYTES = 10 * 1024 * 1024 * 1024
DEFAULT_WORKER_ARTIFACT_LOW_WATERMARK_PCT = 0.75
DEFAULT_WORKER_ARTIFACT_RECENT_GUARD_SECONDS = 60 * 60
DEFAULT_WORKER_MATERIALIZATION_RETENTION_SECONDS = 24 * 60 * 60
DEFAULT_WORKER_CHECKPOINT_RETENTION_SECONDS = 7 * 24 * 60 * 60


class WorkerArtifactInstanceSource(Protocol):
    def list_container_instances(self) -> list[WorkerContainerServiceInstance]: ...


class WorkerArtifactRetentionConfig(ContractModel):
    image_cache_root: Path
    image_mount_root: Path
    checkpoint_root: Path
    image_archive_extension: str = "rclip"
    image_cache_max_bytes: int = DEFAULT_WORKER_IMAGE_CACHE_MAX_BYTES
    image_materialization_max_bytes: int = DEFAULT_WORKER_IMAGE_MATERIALIZATION_MAX_BYTES
    checkpoint_cache_max_bytes: int = DEFAULT_WORKER_CHECKPOINT_CACHE_MAX_BYTES
    low_watermark_pct: float = DEFAULT_WORKER_ARTIFACT_LOW_WATERMARK_PCT
    recent_guard_seconds: int = DEFAULT_WORKER_ARTIFACT_RECENT_GUARD_SECONDS
    materialization_retention_seconds: int = DEFAULT_WORKER_MATERIALIZATION_RETENTION_SECONDS
    checkpoint_retention_seconds: int = DEFAULT_WORKER_CHECKPOINT_RETENTION_SECONDS
    cache_pruning_enabled: bool = True

    @field_validator(
        "image_cache_max_bytes",
        "image_materialization_max_bytes",
        "checkpoint_cache_max_bytes",
        "recent_guard_seconds",
        "materialization_retention_seconds",
        "checkpoint_retention_seconds",
    )
    @classmethod
    def non_negative_values(cls, value: int) -> int:
        if value < 0:
            msg = "worker artifact retention values cannot be negative"
            raise ValueError(msg)
        return value

    @field_validator("low_watermark_pct")
    @classmethod
    def valid_low_watermark(cls, value: float) -> float:
        if not 0 < value <= 1:
            msg = "worker artifact low watermark must be in (0, 1]"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def distinct_roots(self) -> WorkerArtifactRetentionConfig:
        roots = {
            self.image_cache_root.expanduser().resolve(),
            self.image_mount_root.expanduser().resolve(),
            self.checkpoint_root.expanduser().resolve(),
        }
        if len(roots) != 3:
            raise ValueError("worker artifact retention roots must be distinct")
        return self


class WorkerArtifactRetentionResult(ContractModel):
    active_image_count: int = 0
    image_cache_scanned: int = 0
    image_cache_removed: int = 0
    image_cache_freed_bytes: int = 0
    materializations_scanned: int = 0
    materializations_removed: int = 0
    materializations_freed_bytes: int = 0
    checkpoints_scanned: int = 0
    checkpoints_removed: int = 0
    checkpoints_freed_bytes: int = 0
    image_build_scratch_scanned: int = 0
    image_build_scratch_active: int = 0
    image_build_scratch_removed: int = 0
    image_build_scratch_freed_bytes: int = 0
    image_build_scratch_cleanup_failures: list[str] = Field(default_factory=list)

    @property
    def removed(self) -> int:
        return (
            self.image_cache_removed
            + self.materializations_removed
            + self.checkpoints_removed
            + self.image_build_scratch_removed
        )

    @property
    def freed_bytes(self) -> int:
        return (
            self.image_cache_freed_bytes
            + self.materializations_freed_bytes
            + self.checkpoints_freed_bytes
            + self.image_build_scratch_freed_bytes
        )


@dataclass(frozen=True, slots=True)
class _ArtifactCandidate:
    path: Path
    size_bytes: int
    modified_at: datetime


@dataclass(slots=True)
class WorkerArtifactRetentionService:
    instances: WorkerArtifactInstanceSource
    config: WorkerArtifactRetentionConfig
    image_build_scratch: ImageBuildScratchManager | None = None
    checkpoint_activity: CheckpointArtifactLeaseRegistry = field(
        default_factory=CheckpointArtifactLeaseRegistry
    )

    def reconcile(self, *, now: datetime | None = None) -> WorkerArtifactRetentionResult:
        current = now or utc_now()
        active_image_ids = {
            instance.image_id
            for instance in self.instances.list_container_instances()
            if instance.image_id
        }
        if self.config.cache_pruning_enabled:
            image_cache = _prune_bounded_root(
                self.config.image_cache_root,
                max_bytes=self.config.image_cache_max_bytes,
                low_watermark_pct=self.config.low_watermark_pct,
                recent_guard_seconds=self.config.recent_guard_seconds,
                protected_names=_image_cache_protected_names(
                    active_image_ids,
                    extension=self.config.image_archive_extension,
                ),
                now=current,
            )
            materializations = _prune_bounded_root(
                self.config.image_mount_root,
                max_bytes=self.config.image_materialization_max_bytes,
                low_watermark_pct=self.config.low_watermark_pct,
                recent_guard_seconds=self.config.recent_guard_seconds,
                retention_seconds=self.config.materialization_retention_seconds,
                protected_names=active_image_ids,
                now=current,
            )
            checkpoints = _prune_bounded_root(
                self.config.checkpoint_root,
                max_bytes=self.config.checkpoint_cache_max_bytes,
                low_watermark_pct=self.config.low_watermark_pct,
                recent_guard_seconds=self.config.recent_guard_seconds,
                retention_seconds=self.config.checkpoint_retention_seconds,
                protected_names=_checkpoint_protected_names(
                    self.checkpoint_activity.protected_checkpoint_ids()
                ),
                removal_guard=lambda name: self.checkpoint_activity.retention_guard(
                    _checkpoint_id_from_artifact_name(name)
                ),
                now=current,
            )
        else:
            image_cache = _PruneResult()
            materializations = _PruneResult()
            checkpoints = _PruneResult()
        image_build_scratch = (
            self.image_build_scratch.reconcile(now=current)
            if self.image_build_scratch is not None
            else None
        )
        return WorkerArtifactRetentionResult(
            active_image_count=len(active_image_ids),
            image_cache_scanned=image_cache.scanned,
            image_cache_removed=image_cache.removed,
            image_cache_freed_bytes=image_cache.freed_bytes,
            materializations_scanned=materializations.scanned,
            materializations_removed=materializations.removed,
            materializations_freed_bytes=materializations.freed_bytes,
            checkpoints_scanned=checkpoints.scanned,
            checkpoints_removed=checkpoints.removed,
            checkpoints_freed_bytes=checkpoints.freed_bytes,
            image_build_scratch_scanned=(
                image_build_scratch.scanned if image_build_scratch is not None else 0
            ),
            image_build_scratch_active=(
                image_build_scratch.active if image_build_scratch is not None else 0
            ),
            image_build_scratch_removed=(
                image_build_scratch.removed if image_build_scratch is not None else 0
            ),
            image_build_scratch_freed_bytes=(
                image_build_scratch.freed_bytes if image_build_scratch is not None else 0
            ),
            image_build_scratch_cleanup_failures=(
                image_build_scratch.cleanup_failures if image_build_scratch is not None else []
            ),
        )


class _PruneResult(ContractModel):
    scanned: int = 0
    removed: int = 0
    freed_bytes: int = 0


def _prune_bounded_root(
    root: Path,
    *,
    max_bytes: int,
    low_watermark_pct: float,
    recent_guard_seconds: int,
    retention_seconds: int = 0,
    protected_names: set[str] | None = None,
    removal_guard: Callable[[str], AbstractContextManager[bool]] | None = None,
    now: datetime,
) -> _PruneResult:
    resolved_root = root.expanduser().resolve()
    if not resolved_root.exists():
        return _PruneResult()
    candidates = _artifact_candidates(resolved_root)
    total_bytes = sum(candidate.size_bytes for candidate in candidates)
    target_bytes = int(max_bytes * low_watermark_pct) if max_bytes > 0 else total_bytes
    pressured = max_bytes > 0 and total_bytes > max_bytes
    recent_cutoff = now - timedelta(seconds=recent_guard_seconds)
    retention_cutoff = now - timedelta(seconds=retention_seconds)
    protected = protected_names or set()
    removed = 0
    freed = 0
    for candidate in sorted(candidates, key=lambda item: (item.modified_at, item.path.name)):
        expired = retention_seconds > 0 and candidate.modified_at <= retention_cutoff
        if not pressured and not expired:
            continue
        if pressured and total_bytes - freed <= target_bytes and not expired:
            continue
        if candidate.path.name in protected or candidate.modified_at > recent_cutoff:
            continue
        guard = (
            removal_guard(candidate.path.name) if removal_guard is not None else nullcontext(True)
        )
        with guard as removable:
            if not removable:
                continue
            _remove_artifact(candidate.path, root=resolved_root)
        removed += 1
        freed += candidate.size_bytes
    return _PruneResult(scanned=len(candidates), removed=removed, freed_bytes=freed)


def _artifact_candidates(root: Path) -> list[_ArtifactCandidate]:
    candidates: list[_ArtifactCandidate] = []
    for path in root.iterdir():
        if path.name.startswith(".") and not _stale_temporary_path(path):
            continue
        stat = path.lstat()
        candidates.append(
            _ArtifactCandidate(
                path=path,
                size_bytes=_artifact_size(path),
                modified_at=datetime.fromtimestamp(stat.st_mtime, tz=utc_now().tzinfo),
            )
        )
    return candidates


def _artifact_size(path: Path) -> int:
    if path.is_symlink() or path.is_file():
        return path.lstat().st_size
    if not path.is_dir():
        return 0
    return sum(
        child.lstat().st_size for child in path.rglob("*") if child.is_file() or child.is_symlink()
    )


def _remove_artifact(path: Path, *, root: Path) -> None:
    if path.parent.resolve() != root:
        raise ValueError(f"artifact path is not an immediate child of retention root: {path}")
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return
    if path.is_dir():
        shutil.rmtree(path)


def _image_cache_protected_names(image_ids: set[str], *, extension: str) -> set[str]:
    suffixes = {
        extension.lstrip("."),
        LOCAL_IMAGE_ARCHIVE_EXTENSION,
        "cache",
    }
    return {f"{image_id}.{suffix}" for image_id in image_ids for suffix in suffixes if suffix}


def _checkpoint_protected_names(checkpoint_ids: set[str]) -> set[str]:
    return {
        name
        for checkpoint_id in checkpoint_ids
        for name in (
            checkpoint_id,
            checkpoint_id + CHECKPOINT_ARCHIVE_EXTENSION,
            f".{checkpoint_id}.extract",
            f".{checkpoint_id}.tmp",
        )
    }


def _checkpoint_id_from_artifact_name(name: str) -> str:
    if name.startswith(".") and name.endswith((".extract", ".tmp")):
        return name[1:].rsplit(".", maxsplit=1)[0]
    if name.endswith(CHECKPOINT_ARCHIVE_EXTENSION):
        return name.removesuffix(CHECKPOINT_ARCHIVE_EXTENSION)
    return name


def _stale_temporary_path(path: Path) -> bool:
    return path.name.endswith((".tmp", ".extract"))


__all__ = [
    "DEFAULT_WORKER_ARTIFACT_RETENTION_INTERVAL_SECONDS",
    "DEFAULT_WORKER_CHECKPOINT_CACHE_MAX_BYTES",
    "DEFAULT_WORKER_IMAGE_CACHE_MAX_BYTES",
    "DEFAULT_WORKER_IMAGE_MATERIALIZATION_MAX_BYTES",
    "WorkerArtifactRetentionConfig",
    "WorkerArtifactRetentionResult",
    "WorkerArtifactRetentionService",
]
