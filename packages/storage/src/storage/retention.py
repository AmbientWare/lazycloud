from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from database.repositories.cleanup import (
    OBJECT_CLEANUP_SOURCE,
    CleanupRepository,
    object_location_lock_key,
)
from database.repositories.identity import WorkspaceRepository
from database.repositories.images import ImageBuildRepository, ImageRepository
from database.repositories.source_cache import SourceCacheCleanupRepository
from database.repositories.storage import (
    CacheEntryRepository,
    ObjectReferenceRepository,
    ObjectRepository,
    OwnedObjectRecord,
)
from pydantic import field_validator
from shared.app_identity import SOURCE_PACKAGE_BUCKET
from shared.contracts import ContractModel
from shared.identity import WorkspaceStatus
from shared.image_building.records import BuildStatus, ImageBuildRecord, ImageRecord
from shared.objects import ObjectRecord
from shared.runtime_paths import normalize_runtime_path
from shared.timestamps import utc_now

from storage.checkpoint_retention import DurableCheckpointRetentionService
from storage.context import StorageContext
from storage.service import CacheStorage, ObjectByteClient, ObjectStorage

DEFAULT_RETENTION_INTERVAL_SECONDS = 60 * 60
DEFAULT_RETENTION_SOURCE_GRACE_SECONDS = 24 * 60 * 60
DEFAULT_RETENTION_BUILD_SECONDS = 7 * 24 * 60 * 60
DEFAULT_RETENTION_IMAGE_SECONDS = 7 * 24 * 60 * 60
DEFAULT_RETENTION_MAX_ITEMS_PER_CYCLE = 100
OBJECT_CLEANUP_IMAGE_ARCHIVE = "image-archive-retention"


class RetentionConfig(ContractModel):
    image_archive_bucket: str
    checkpoint_bucket: str
    image_archive_prefix: str = ""
    source_grace_seconds: int = DEFAULT_RETENTION_SOURCE_GRACE_SECONDS
    build_retention_seconds: int = DEFAULT_RETENTION_BUILD_SECONDS
    image_retention_seconds: int = DEFAULT_RETENTION_IMAGE_SECONDS
    max_items_per_cycle: int = DEFAULT_RETENTION_MAX_ITEMS_PER_CYCLE
    object_operation_lease_seconds: int = 2 * 60 * 60

    @field_validator(
        "source_grace_seconds",
        "build_retention_seconds",
        "image_retention_seconds",
        "max_items_per_cycle",
        "object_operation_lease_seconds",
    )
    @classmethod
    def positive_values(cls, value: int) -> int:
        if value <= 0:
            msg = "artifact retention values must be positive"
            raise ValueError(msg)
        return value


class RetentionResult(ContractModel):
    source_objects_removed: int = 0
    image_archives_removed: int = 0
    image_records_removed: int = 0
    build_records_removed: int = 0
    build_paths_removed: int = 0
    cache_entries_removed: int = 0
    cache_objects_removed: int = 0
    checkpoints_removed: int = 0

    @property
    def removed(self) -> int:
        return (
            self.source_objects_removed
            + self.image_archives_removed
            + self.image_records_removed
            + self.build_records_removed
            + self.build_paths_removed
            + self.cache_entries_removed
            + self.cache_objects_removed
            + self.checkpoints_removed
        )


@dataclass(slots=True)
class RetentionService:
    context: StorageContext
    object_storage: ObjectStorage
    cache_storage: CacheStorage
    config: RetentionConfig
    image_archive_client: ObjectByteClient | None = None

    def reconcile(
        self,
        *,
        active_recent_stub_keys: list[str],
        now: datetime | None = None,
    ) -> RetentionResult:
        current = now or utc_now()
        self.object_storage.reconcile_operations(
            now=current,
            lease_seconds=self.config.object_operation_lease_seconds,
            limit=self.config.max_items_per_cycle,
        )
        build_cutoff = current - timedelta(seconds=self.config.build_retention_seconds)
        source_removed = self._prune_source_objects(
            frozenset(),
            created_before=current - timedelta(seconds=self.config.source_grace_seconds),
            recent_build_after=build_cutoff,
        )
        checkpoint_removed = self._prune_checkpoints(active_recent_stub_keys, now=current)
        cache_removed = self._prune_expired_cache(current)
        (
            image_archives_removed,
            image_records_removed,
            image_builds_removed,
            image_build_paths_removed,
            image_build_cache_removed,
        ) = self._prune_images(
            frozenset(),
            updated_before=current - timedelta(seconds=self.config.image_retention_seconds),
            recent_build_after=build_cutoff,
        )
        build_records_removed, build_paths_removed, build_cache_removed = self._prune_builds(
            frozenset(),
            finished_before=build_cutoff,
            recent_build_after=build_cutoff,
        )
        cache_reconciliation = self.cache_storage.reconcile(limit=self.config.max_items_per_cycle)
        return RetentionResult(
            source_objects_removed=source_removed,
            image_archives_removed=image_archives_removed,
            image_records_removed=image_records_removed,
            build_records_removed=build_records_removed + image_builds_removed,
            build_paths_removed=build_paths_removed + image_build_paths_removed,
            cache_entries_removed=(
                cache_removed
                + build_cache_removed
                + image_build_cache_removed
                + cache_reconciliation.records_removed
            ),
            cache_objects_removed=cache_reconciliation.objects_removed,
            checkpoints_removed=checkpoint_removed,
        )

    def _prune_source_objects(
        self,
        referenced_object_ids: frozenset[str],
        *,
        created_before: datetime,
        recent_build_after: datetime,
    ) -> int:
        removed = self._resume_claimed_source_objects()
        with self.context.database.session() as session:
            candidates = ObjectReferenceRepository(session).list_source_cleanup_candidates(
                source_bucket=SOURCE_PACKAGE_BUCKET,
                created_before=created_before,
                recent_build_after=recent_build_after,
                excluded_object_ids=referenced_object_ids,
                limit=max(self.config.max_items_per_cycle - removed, 0),
            )
        for candidate in candidates:
            if self._prune_source_candidate(
                candidate,
                created_before=created_before,
                recent_build_after=recent_build_after,
            ):
                removed += 1
        return removed

    def _prune_source_candidate(
        self,
        candidate: OwnedObjectRecord,
        *,
        created_before: datetime,
        recent_build_after: datetime,
    ) -> bool:
        claimed = self._claim_source_candidate(
            candidate,
            created_before=created_before,
            recent_build_after=recent_build_after,
        )
        return claimed is not None and self._delete_claimed_source(claimed)

    def _claim_source_candidate(
        self,
        candidate: OwnedObjectRecord,
        *,
        created_before: datetime,
        recent_build_after: datetime,
    ) -> ObjectRecord | None:
        with self.context.database.session() as session:
            references = ObjectReferenceRepository(session)
            objects = ObjectRepository(session)
            current = objects.get_owned(candidate.record.id)
            if current is None:
                return None
            claims = CleanupRepository(session)
            claims.lock_keys(
                {
                    f"object:{current.record.id}",
                    object_location_lock_key(
                        current.workspace_id,
                        current.record.bucket,
                        current.record.key,
                    ),
                }
            )
            current = objects.get_owned(candidate.record.id)
            if (
                current is None
                or current.record.cleanup_claimed_at is not None
                or current.record.created_at >= created_before
                or references.object_is_referenced(
                    current.record.id,
                    workspace_id=current.workspace_id,
                    recent_build_after=recent_build_after,
                )
                or ImageRepository(session).archive_object_is_referenced(
                    current.record.id,
                    workspace_id=current.workspace_id,
                )
                or not _generated_source_object(current.record.bucket, current.record.key)
            ):
                return None
            return claims.mark_object_claimed(
                current.record.id,
                claimed_at=utc_now(),
                cleanup_kind=OBJECT_CLEANUP_SOURCE,
            )

    def _resume_claimed_source_objects(self) -> int:
        with self.context.database.session() as session:
            claimed = CleanupRepository(session).list_claimed_objects(
                limit=self.config.max_items_per_cycle,
                cleanup_kind=OBJECT_CLEANUP_SOURCE,
            )
        return sum(int(self._delete_claimed_source(record)) for record in claimed)

    def _delete_claimed_source(self, record: ObjectRecord) -> bool:
        physical_key = self.object_storage.physical_key_for_record(record)
        physical_bucket = self.object_storage.physical_bucket(record.bucket)
        self.object_storage.object_client.delete(physical_key, bucket=physical_bucket)
        if self.object_storage.object_client.exists(physical_key, bucket=physical_bucket):
            raise RuntimeError(
                f"source object deletion was not confirmed: {record.bucket}/{record.key}"
            )
        with self.context.database.session() as session:
            claims = CleanupRepository(session)
            current = ObjectRepository(session).get_owned(record.id, include_operations=True)
            if current is None:
                return False
            claims.lock_keys(
                {
                    f"object:{record.id}",
                    object_location_lock_key(
                        current.workspace_id,
                        record.bucket,
                        record.key,
                    ),
                }
            )
            current = ObjectRepository(session).get_owned(record.id, include_operations=True)
            if current is None:
                return False
            if current.record.cleanup_claimed_at is None:
                raise RuntimeError(f"source cleanup claim was lost: {record.id}")
            workspace = WorkspaceRepository(session).lock_for_deletion(current.workspace_id)
            SourceCacheCleanupRepository(session).add_targets(
                workspace_id=current.workspace_id,
                source_object_ids=[record.id],
                now=utc_now(),
            )
            objects = ObjectRepository(session)
            if workspace.status is WorkspaceStatus.Active:
                return objects.delete_across_workspaces(record.id)
            if workspace.status is WorkspaceStatus.Deleting:
                return objects.delete_for_workspace_deletion(
                    record.id,
                    workspace_id=current.workspace_id,
                    cleanup_kind=OBJECT_CLEANUP_SOURCE,
                )
            raise RuntimeError(f"source cleanup owner is no longer mutable: {current.workspace_id}")

    def _prune_checkpoints(
        self,
        active_recent_stub_keys: list[str],
        *,
        now: datetime,
    ) -> int:
        result = DurableCheckpointRetentionService(
            context=self.context,
            object_storage=self.object_storage,
            checkpoint_bucket=self.config.checkpoint_bucket,
            max_items_per_cycle=self.config.max_items_per_cycle,
        ).prune(active_recent_stub_keys, now=now)
        return result.count

    def _prune_images(
        self,
        referenced_image_ids: frozenset[str],
        *,
        updated_before: datetime,
        recent_build_after: datetime,
    ) -> tuple[int, int, int, int, int]:
        resumed = self._resume_claimed_images()
        with self.context.database.session() as session:
            candidates = [
                image
                for image in ObjectReferenceRepository(session).list_image_cleanup_candidates(
                    excluded_image_ids=referenced_image_ids,
                    updated_before=updated_before,
                    recent_build_after=recent_build_after,
                    limit=self.config.max_items_per_cycle,
                )
                if image.cleanup_claimed_at is None
            ][: max(self.config.max_items_per_cycle - resumed[1], 0)]
        archives_removed, records_removed, builds_removed, paths_removed, cache_removed = resumed
        remaining_build_budget = max(self.config.max_items_per_cycle - builds_removed, 0)
        for image in candidates:
            if remaining_build_budget <= 0:
                break
            removed = self._prune_image_candidate(
                image,
                updated_before=updated_before,
                recent_build_after=recent_build_after,
                build_limit=remaining_build_budget,
            )
            archives_removed += removed[0]
            records_removed += removed[1]
            builds_removed += removed[2]
            paths_removed += removed[3]
            cache_removed += removed[4]
            remaining_build_budget -= removed[2]
        return (
            archives_removed,
            records_removed,
            builds_removed,
            paths_removed,
            cache_removed,
        )

    def _prune_builds(
        self,
        retained_build_ids: frozenset[str],
        *,
        finished_before: datetime,
        recent_build_after: datetime,
    ) -> tuple[int, int, int]:
        resumed = self._resume_claimed_builds()
        with self.context.database.session() as session:
            candidates = [
                build
                for build in ObjectReferenceRepository(session).list_build_cleanup_candidates(
                    excluded_build_ids=retained_build_ids,
                    finished_before=finished_before,
                    recent_build_after=recent_build_after,
                    limit=self.config.max_items_per_cycle,
                )
                if build.cleanup_claimed_at is None
            ][: max(self.config.max_items_per_cycle - resumed[0], 0)]
        records_removed, paths_removed, cache_removed = resumed
        for build in candidates:
            removed = self._prune_build_candidate(
                build,
                finished_before=finished_before,
                recent_build_after=recent_build_after,
            )
            records_removed += removed[0]
            paths_removed += removed[1]
            cache_removed += removed[2]
        return records_removed, paths_removed, cache_removed

    def _prune_image_candidate(
        self,
        candidate: ImageRecord,
        *,
        updated_before: datetime,
        recent_build_after: datetime,
        build_limit: int | None = None,
    ) -> tuple[int, int, int, int, int]:
        claimed = self._claim_image_candidate(
            candidate,
            updated_before=updated_before,
            recent_build_after=recent_build_after,
        )
        return (
            self._delete_claimed_image(claimed, build_limit=build_limit)
            if claimed is not None
            else (0, 0, 0, 0, 0)
        )

    def _claim_image_candidate(
        self,
        candidate: ImageRecord,
        *,
        updated_before: datetime,
        recent_build_after: datetime,
    ) -> ImageRecord | None:
        with self.context.database.session() as session:
            references = ObjectReferenceRepository(session)
            claims = CleanupRepository(session)
            claims.lock_keys({f"image:{candidate.workspace_id}:{candidate.image_id}"})
            images = ImageRepository(session)
            current = images.get_updated_before(
                candidate.image_id,
                workspace_id=candidate.workspace_id,
                updated_before=updated_before,
            )
            if (
                current is None
                or current.cleanup_claimed_at is not None
                or references.image_is_referenced(
                    current.image_id,
                    workspace_id=current.workspace_id,
                    recent_build_after=recent_build_after,
                )
            ):
                return None

            return claims.mark_image_claimed(
                current.image_id,
                workspace_id=current.workspace_id,
                claimed_at=utc_now(),
            )

    def _claim_image_build_batch(
        self,
        image_id: str,
        *,
        workspace_id: str,
        limit: int,
    ) -> list[ImageBuildRecord]:
        if limit <= 0:
            return []
        with self.context.database.session() as session:
            claims = CleanupRepository(session)
            claims.lock_keys({f"image:{workspace_id}:{image_id}"})
            image = ImageRepository(session).get(image_id, workspace_id=workspace_id)
            if image is None or image.cleanup_claimed_at is None:
                return []
            builds = ImageBuildRepository(session)
            claimed = builds.list_for_image_cleanup(
                image_id,
                workspace_id=workspace_id,
                claimed=True,
                limit=limit,
            )
            if claimed:
                return claimed
            candidates = builds.list_for_image_cleanup(
                image_id,
                workspace_id=workspace_id,
                claimed=False,
                limit=limit,
            )
            claim_keys: set[str] = set()
            for build in candidates:
                claim_keys.update(_build_claim_keys(build))
            claim_keys.add(f"image:{workspace_id}:{image_id}")
            claims.lock_keys(claim_keys)
            current = ImageRepository(session).get(image_id, workspace_id=workspace_id)
            if current is None or current.cleanup_claimed_at is None:
                raise RuntimeError(f"image cleanup claim was lost: {image_id}")
            return claims.mark_builds_claimed(
                [build.id for build in candidates],
                claimed_at=utc_now(),
            )

    def _resume_claimed_images(self) -> tuple[int, int, int, int, int]:
        with self.context.database.session() as session:
            claimed = CleanupRepository(session).list_claimed_images(
                limit=self.config.max_items_per_cycle
            )
        total = [0, 0, 0, 0, 0]
        remaining_build_budget = self.config.max_items_per_cycle
        for image in claimed:
            if remaining_build_budget <= 0:
                break
            removed = self._delete_claimed_image(
                image,
                build_limit=remaining_build_budget,
            )
            total = [current + item for current, item in zip(total, removed, strict=True)]
            remaining_build_budget -= removed[2]
        return (total[0], total[1], total[2], total[3], total[4])

    def _delete_claimed_image(
        self,
        image: ImageRecord,
        *,
        build_limit: int | None = None,
    ) -> tuple[int, int, int, int, int]:
        image_builds = self._claim_image_build_batch(
            image.image_id,
            workspace_id=image.workspace_id,
            limit=(self.config.max_items_per_cycle if build_limit is None else build_limit),
        )
        with self.context.database.session() as session:
            builds = ImageBuildRepository(session)
            protected = _protected_build_resources(
                builds,
                deleting_builds=image_builds,
            )
        archives_removed = self._delete_claimed_image_archive(image)

        paths_removed = 0
        cache_removed = 0
        for build in image_builds:
            paths_removed += self._remove_build_paths(
                build,
                protected_paths=protected.paths,
            )
            cache_key = _build_cache_key(build)
            if (
                cache_key
                and cache_key not in protected.cache_keys
                and self.cache_storage.delete_key(cache_key)
            ):
                cache_removed += 1
        with self.context.database.session() as session:
            claims = CleanupRepository(session)
            claims.lock_keys(
                {f"image:{image.workspace_id}:{image.image_id}"}
                | set().union(*(_build_claim_keys(build) for build in image_builds))
                if image_builds
                else {f"image:{image.workspace_id}:{image.image_id}"}
            )
            images = ImageRepository(session)
            current = images.get(image.image_id, workspace_id=image.workspace_id)
            if current is None:
                return (archives_removed, 0, 0, paths_removed, cache_removed)
            if current.cleanup_claimed_at is None:
                raise RuntimeError(f"image cleanup claim was lost: {image.image_id}")
            builds = ImageBuildRepository(session)
            builds_removed = builds.delete_claimed_for_image(
                [build.id for build in image_builds],
                image_id=current.image_id,
                workspace_id=current.workspace_id,
            )
            if builds.has_for_image(
                current.image_id,
                workspace_id=current.workspace_id,
            ):
                return (
                    archives_removed,
                    0,
                    builds_removed,
                    paths_removed,
                    cache_removed,
                )
            return (
                archives_removed,
                int(
                    images.finalize_cleanup(
                        current.image_id,
                        workspace_id=current.workspace_id,
                        completed_at=utc_now(),
                    )
                ),
                builds_removed,
                paths_removed,
                cache_removed,
            )

    def _delete_claimed_image_archive(self, image: ImageRecord) -> int:
        if not image.has_archive:
            return 0
        with self.context.database.session() as session:
            current_image = ImageRepository(session).get(
                image.image_id,
                workspace_id=image.workspace_id,
            )
            if current_image is None or current_image.cleanup_claimed_at is None:
                raise RuntimeError(f"image cleanup claim was lost: {image.image_id}")
            _assert_same_archive_identity(image, current_image)
            objects = ObjectRepository(session)
            owned = objects.get_owned(image.archive_object_id, include_operations=True)
            if owned is None:
                return 0
            _assert_archive_object_identity(
                image,
                owned,
                expected_bucket=self.config.image_archive_bucket,
            )
            archive = objects.claim_delete(
                image.archive_object_id,
                cleanup_kind=OBJECT_CLEANUP_IMAGE_ARCHIVE,
                claimed_at=utc_now(),
            )

        physical_bucket = self.object_storage.physical_bucket(archive.bucket)
        physical_key = self.object_storage.physical_key_for_workspace(
            image.workspace_id,
            bucket=archive.bucket,
            key=archive.key,
        )
        archive_client = self.image_archive_client or self.object_storage.object_client
        existed = archive_client.exists(physical_key, bucket=physical_bucket)
        archive_client.delete(physical_key, bucket=physical_bucket)
        if archive_client.exists(physical_key, bucket=physical_bucket):
            raise RuntimeError(
                f"image archive deletion was not confirmed: {archive.bucket}/{archive.key}"
            )

        with self.context.database.session() as session:
            claims = CleanupRepository(session)
            claims.lock_keys(
                {
                    f"image:{image.workspace_id}:{image.image_id}",
                    f"object:{archive.id}",
                    object_location_lock_key(
                        image.workspace_id,
                        archive.bucket,
                        archive.key,
                    ),
                }
            )
            current_image = ImageRepository(session).get(
                image.image_id,
                workspace_id=image.workspace_id,
            )
            if current_image is None or current_image.cleanup_claimed_at is None:
                raise RuntimeError(f"image cleanup claim was lost: {image.image_id}")
            _assert_same_archive_identity(image, current_image)
            objects = ObjectRepository(session)
            owned = objects.get_owned(archive.id, include_operations=True)
            if owned is None:
                return int(existed)
            _assert_archive_object_identity(
                image,
                owned,
                expected_bucket=self.config.image_archive_bucket,
            )
            if owned.record.cleanup_kind != OBJECT_CLEANUP_IMAGE_ARCHIVE:
                raise RuntimeError(f"image archive cleanup claim was lost: {archive.id}")
            ImageRepository(session).detach_archive_for_cleanup(
                image.image_id,
                workspace_id=image.workspace_id,
                archive_object_id=archive.id,
            )
            objects.delete_across_workspaces(archive.id)
        return int(existed)

    def _prune_build_candidate(
        self,
        candidate: ImageBuildRecord,
        *,
        finished_before: datetime,
        recent_build_after: datetime,
    ) -> tuple[int, int, int]:
        claimed = self._claim_build_candidate(
            candidate,
            finished_before=finished_before,
            recent_build_after=recent_build_after,
        )
        return self._delete_claimed_build(claimed) if claimed is not None else (0, 0, 0)

    def _claim_build_candidate(
        self,
        candidate: ImageBuildRecord,
        *,
        finished_before: datetime,
        recent_build_after: datetime,
    ) -> ImageBuildRecord | None:
        with self.context.database.session() as session:
            references = ObjectReferenceRepository(session)
            builds = ImageBuildRepository(session)
            current = builds.get_across_workspaces(candidate.id)
            workspace_id = builds.workspace_id(candidate.id)
            claims = CleanupRepository(session)
            claim_keys = _build_claim_keys(candidate)
            if workspace_id is not None and candidate.image_id:
                claim_keys.add(f"image:{workspace_id}:{candidate.image_id}")
            claims.lock_keys(claim_keys)
            if (
                current is None
                or workspace_id is None
                or current.cleanup_claimed_at is not None
                or current.status in {BuildStatus.Pending, BuildStatus.Running}
                or current.finished_at is None
                or current.finished_at >= finished_before
                or references.build_is_retained(
                    current,
                    workspace_id=workspace_id,
                    recent_build_after=recent_build_after,
                )
            ):
                return None
            image = (
                ImageRepository(session).get(
                    current.image_id,
                    workspace_id=workspace_id,
                )
                if current.image_id
                else None
            )
            if image is not None and image.cleanup_claimed_at is not None:
                return None
            return claims.mark_build_claimed(current.id, claimed_at=utc_now())

    def _resume_claimed_builds(self) -> tuple[int, int, int]:
        with self.context.database.session() as session:
            images_claimed = {
                (image.workspace_id, image.image_id)
                for image in CleanupRepository(session).list_claimed_images(
                    limit=self.config.max_items_per_cycle
                )
            }
            builds = ImageBuildRepository(session)
            claimed = [
                build
                for build in CleanupRepository(session).list_claimed_builds(
                    limit=self.config.max_items_per_cycle
                )
                if not build.image_id
                or (builds.workspace_id(build.id), build.image_id) not in images_claimed
            ][: self.config.max_items_per_cycle]
        total = [0, 0, 0]
        for build in claimed:
            removed = self._delete_claimed_build(build)
            total = [current + item for current, item in zip(total, removed, strict=True)]
        return (total[0], total[1], total[2])

    def _delete_claimed_build(self, build: ImageBuildRecord) -> tuple[int, int, int]:
        with self.context.database.session() as session:
            builds = ImageBuildRepository(session)
            protected = _protected_build_resources(
                builds,
                deleting_builds=[build],
            )
        paths_removed = self._remove_build_paths(build, protected_paths=protected.paths)
        cache_key = _build_cache_key(build)
        cache_removed = int(
            bool(cache_key)
            and cache_key not in protected.cache_keys
            and self.cache_storage.delete_key(cache_key)
        )
        with self.context.database.session() as session:
            claims = CleanupRepository(session)
            claims.lock_keys(_build_claim_keys(build))
            current = ImageBuildRepository(session).get_across_workspaces(build.id)
            if current is None:
                return (0, paths_removed, cache_removed)
            if current.cleanup_claimed_at is None:
                raise RuntimeError(f"build cleanup claim was lost: {build.id}")
            return (
                int(ImageBuildRepository(session).delete_across_workspaces(build.id)),
                paths_removed,
                cache_removed,
            )

    def _prune_expired_cache(self, now: datetime) -> int:
        with self.context.database.session() as session:
            expired = CacheEntryRepository(session).list_expired(
                now=now,
                limit=self.config.max_items_per_cycle,
            )
        return sum(1 for entry in expired if self.cache_storage.delete_key(entry.key))

    def _remove_build_paths(
        self,
        build: ImageBuildRecord,
        *,
        protected_paths: frozenset[Path],
    ) -> int:
        removed = 0
        seen: set[Path] = set()
        for path in _build_paths(build):
            if (
                path in seen
                or path in protected_paths
                or not _owned_build_path(path, root=self.context.paths.root)
            ):
                continue
            seen.add(path)
            if path.is_symlink() or path.is_file():
                path.unlink(missing_ok=True)
                removed += 1
            elif path.is_dir():
                try:
                    shutil.rmtree(path)
                except FileNotFoundError:
                    continue
                removed += 1
        return removed


def _generated_source_object(bucket: str, key: str) -> bool:
    return (bucket == SOURCE_PACKAGE_BUCKET and key.startswith("sources/")) or key.startswith(
        "image-builds/"
    )


def _assert_same_archive_identity(expected: ImageRecord, current: ImageRecord) -> None:
    if (
        current.workspace_id != expected.workspace_id
        or current.image_id != expected.image_id
        or current.archive_object_id != expected.archive_object_id
        or current.archive_object_key != expected.archive_object_key
        or current.archive_size_bytes != expected.archive_size_bytes
        or current.archive_sha256 != expected.archive_sha256
    ):
        raise RuntimeError(f"image archive identity changed during cleanup: {expected.image_id}")


def _assert_archive_object_identity(
    image: ImageRecord,
    owned: OwnedObjectRecord,
    *,
    expected_bucket: str,
) -> None:
    record = owned.record
    if (
        owned.workspace_id != image.workspace_id
        or record.id != image.archive_object_id
        or record.bucket != expected_bucket
        or record.key != image.archive_object_key
        or record.size != image.archive_size_bytes
        or record.sha256 != image.archive_sha256
    ):
        raise RuntimeError(f"image archive object identity is invalid: {image.image_id}")


@dataclass(frozen=True, slots=True)
class _BuildResourceProtection:
    paths: frozenset[Path]
    cache_keys: frozenset[str]


def _protected_build_resources(
    repository: ImageBuildRepository,
    *,
    deleting_builds: list[ImageBuildRecord],
) -> _BuildResourceProtection:
    deleting_build_ids = {build.id for build in deleting_builds}
    candidate_paths = {str(path) for build in deleting_builds for path in _build_paths(build)}
    candidate_cache_keys = {
        cache_key for build in deleting_builds if (cache_key := _build_cache_key(build))
    }
    protected_paths, protected_cache_keys = repository.protected_artifact_resources(
        deleting_build_ids=deleting_build_ids,
        paths=candidate_paths,
        cache_keys=candidate_cache_keys,
    )
    return _BuildResourceProtection(
        paths=frozenset(Path(path) for path in protected_paths),
        cache_keys=protected_cache_keys,
    )


def _build_paths(build: ImageBuildRecord) -> tuple[Path, ...]:
    raw_paths = (
        build.artifact_path,
        build.manifest_path,
        build.cache_metadata.get("dockerfile_path"),
        build.cache_metadata.get("manifest_path"),
    )
    return tuple(Path(normalize_runtime_path(raw_path)) for raw_path in raw_paths if raw_path)


def _build_cache_key(build: ImageBuildRecord) -> str:
    return build.cache_metadata.get("cache_publish_key", "")


def _build_claim_keys(build: ImageBuildRecord) -> set[str]:
    keys = {f"build:{build.id}"}
    keys.update(f"path:{path}" for path in _build_paths(build))
    cache_key = _build_cache_key(build)
    if cache_key:
        keys.add(f"cache:{cache_key}")
    if build.cache_key:
        keys.add(f"build-cache-key:{build.cache_key}")
    if build.image.context_object_id:
        keys.add(f"object:{build.image.context_object_id}")
    if build.image_id:
        keys.add(f"image:{build.image_id}")
    return keys


def _owned_build_path(path: Path, *, root: Path) -> bool:
    resolved_root = root.expanduser().resolve()
    return path != resolved_root and resolved_root in path.parents


__all__ = [
    "DEFAULT_RETENTION_INTERVAL_SECONDS",
    "RetentionConfig",
    "RetentionResult",
    "RetentionService",
]
