from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from database.repositories.cleanup import CleanupRepository
from database.repositories.common import (
    TableRepositoryConfig,
    WorkspaceTableRepository,
)
from database.repositories.identity import WorkspaceRepository
from database.tables.images import (
    CheckpointTable,
    ImageArchiveTable,
    ImageBuildTable,
    ImageTable,
)
from shared.checkpoints import (
    CHECKPOINT_RETENTION_ELIGIBLE_STATUSES,
    CheckpointPruneResult,
    CheckpointRecord,
    CheckpointStatus,
)
from shared.image_building.records import (
    BuildStatus,
    ImageArchiveRecord,
    ImageBuildPhase,
    ImageBuildRecord,
    ImageRecord,
)
from shared.runtime_paths import archive_path_digest, normalize_runtime_path
from shared.timestamps import utc_now
from sqlalchemy import case, delete, exists, func, or_, select, text, tuple_, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


@dataclass(slots=True)
class ImageRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[ImageRecord]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(ImageTable, ImageRecord, key_field="image_id"),
        )

    def upsert(self, image: ImageRecord) -> ImageRecord:
        CleanupRepository(self.session).assert_image_write_available(
            image.image_id,
            workspace_id=image.workspace_id,
        )
        existing = self.get(
            image.image_id,
            workspace_id=image.workspace_id,
            include_cleaned=True,
        )
        if existing is None:
            saved = self.records.create(
                image.model_dump(mode="json", exclude={"id"}, exclude_none=True),
                workspace_id=image.workspace_id,
                name=image.image_id,
            )
        else:
            saved = self.records.upsert(
                image.model_copy(update={"id": existing.id}),
                workspace_id=image.workspace_id,
                key=image.image_id,
                name=image.image_id,
            )
        return saved

    def get(
        self,
        image_id: str,
        *,
        workspace_id: str,
        include_cleaned: bool = False,
    ) -> ImageRecord | None:
        record = self.records.get(image_id, workspace_id=workspace_id)
        if record is not None and record.cleanup_completed_at is not None and not include_cleaned:
            return None
        return record

    def get_updated_before(
        self,
        image_id: str,
        *,
        workspace_id: str,
        updated_before: datetime,
    ) -> ImageRecord | None:
        row = self.session.scalars(
            select(ImageTable).where(
                ImageTable.workspace_id == workspace_id,
                ImageTable.image_id == image_id,
                ImageTable.updated_at < updated_before,
            )
        ).first()
        return ImageRecord.model_validate(row.payload) if row is not None else None

    def delete(self, image_id: str, *, workspace_id: str) -> bool:
        return self.records.delete(image_id, workspace_id=workspace_id)

    def finalize_cleanup(
        self,
        image_id: str,
        *,
        workspace_id: str,
        completed_at: datetime,
    ) -> bool:
        row = self.session.scalars(
            select(ImageTable).where(
                ImageTable.workspace_id == workspace_id,
                ImageTable.image_id == image_id,
            )
        ).first()
        if row is None:
            return False
        current = ImageRecord.model_validate(row.payload)
        completed = current.model_copy(
            update={
                "cleanup_claimed_at": None,
                "cleanup_completed_at": completed_at,
            }
        )
        row.payload = completed.model_dump(mode="json")
        row.cleanup_claimed_at = None
        row.cleanup_completed_at = completed_at
        self.session.flush()
        return True


@dataclass(slots=True)
class ImageArchiveRepository:
    """System-authority access to the one archive per image id.

    Not a `WorkspaceTableRepository`: the archive has no workspace. Tenant access
    goes through `get_authorized`, which requires the caller's own `images` row.
    """

    session: Session

    def get(self, image_id: str) -> ImageArchiveRecord | None:
        row = self.session.scalars(
            select(ImageArchiveTable).where(ImageArchiveTable.image_id == image_id)
        ).first()
        return _image_archive_record(row) if row is not None else None

    def get_authorized(self, image_id: str, *, workspace_id: str) -> ImageArchiveRecord | None:
        """The archive, only if this workspace holds a live authorization for it.

        The physical key carries no tenant component, so this join is the whole of
        the download boundary. Resolving the archive without it would hand any
        workspace any image.
        """

        row = self.session.scalars(
            select(ImageArchiveTable)
            .join(ImageTable, ImageTable.image_id == ImageArchiveTable.image_id)
            .where(
                ImageArchiveTable.image_id == image_id,
                ImageTable.workspace_id == workspace_id,
                ImageTable.cleanup_completed_at.is_(None),
            )
        ).first()
        return _image_archive_record(row) if row is not None else None

    def reserve(
        self,
        image_id: str,
        *,
        bucket: str,
        object_key: str,
        size_bytes: int,
        sha256: str,
        registry_ref: str,
        manifest_digest: str,
        architecture: str,
        format_version: int,
    ) -> tuple[ImageArchiveRecord, bool]:
        """Claim the archive for this image id, or return the one already there.

        Returns ``(record, reserved)``. ``reserved`` is False when another build got
        there first, which is the ordinary dedup path rather than an error.
        """

        existing = self.get(image_id)
        if existing is not None:
            return existing, False
        row = ImageArchiveTable(
            image_id=image_id,
            bucket=bucket,
            object_key=object_key,
            size_bytes=size_bytes,
            sha256=sha256,
            payload={
                "registry_ref": registry_ref,
                "manifest_digest": manifest_digest,
                "architecture": architecture,
                "format_version": format_version,
            },
        )
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError:
            # Another build inserted the same image id between the read and the
            # flush; its bytes are as good as ours.
            #
            # Rolled back to a savepoint rather than outright: a plain rollback
            # discards the caller's whole transaction, and the caller here has an
            # image build in flight that would be silently thrown away while this
            # returned as though it had succeeded.
            conflicting = self.get(image_id)
            if conflicting is None:
                raise
            return conflicting, False
        return _image_archive_record(row), True

    def take_over(
        self,
        image_id: str,
        *,
        expected_sha256: str,
        bucket: str,
        object_key: str,
        size_bytes: int,
        sha256: str,
        registry_ref: str,
        manifest_digest: str,
        architecture: str,
        format_version: int,
    ) -> ImageArchiveRecord | None:
        """Repoint a broken archive, only if it still holds the digest we saw.

        The compare-and-set is what keeps the row and the bytes moving together: the
        recorded digest only changes in the same statement that claims the right to
        replace the bytes, so a valid archive can never be silently overwritten with
        different content. A row retention has already claimed is equally off limits:
        repointing it would leave the cleanup that is mid-delete pointed at bytes a
        build is writing.
        """

        result = self.session.execute(
            update(ImageArchiveTable)
            .where(
                ImageArchiveTable.image_id == image_id,
                ImageArchiveTable.sha256 == expected_sha256,
                ImageArchiveTable.cleanup_claimed_at.is_(None),
            )
            .values(
                bucket=bucket,
                object_key=object_key,
                size_bytes=size_bytes,
                sha256=sha256,
                payload={
                    "registry_ref": registry_ref,
                    "manifest_digest": manifest_digest,
                    "architecture": architecture,
                    "format_version": format_version,
                },
                updated_at=utc_now(),
            )
        )
        if not _one_row_changed(result):
            return None
        self.session.flush()
        return self.get(image_id)

    def list_cleanup_candidates(
        self,
        *,
        updated_before: datetime,
        recent_build_after: datetime,
        limit: int,
    ) -> list[ImageArchiveRecord]:
        """Archives no live authorization or recent build still needs.

        Both clauses are unscoped by workspace, which is the point: the archive
        belongs to no tenant, so one tenant's cleanup must not free bytes another is
        still authorized for. The build clause covers the window between reserving
        an archive and writing the authorization row.
        """

        authorized = (
            select(ImageTable.id)
            .where(
                ImageTable.image_id == ImageArchiveTable.image_id,
                ImageTable.cleanup_completed_at.is_(None),
            )
            .exists()
        )
        building = (
            select(ImageBuildTable.id)
            .where(
                ImageBuildTable.image_id == ImageArchiveTable.image_id,
                or_(
                    ImageBuildTable.status.in_(
                        (BuildStatus.Pending.value, BuildStatus.Running.value)
                    ),
                    ImageBuildTable.finished_at.is_(None),
                    ImageBuildTable.finished_at >= recent_build_after,
                ),
            )
            .exists()
        )
        rows = self.session.scalars(
            select(ImageArchiveTable)
            .where(
                ImageArchiveTable.updated_at < updated_before,
                ImageArchiveTable.cleanup_claimed_at.is_(None),
                ~authorized,
                ~building,
            )
            .order_by(ImageArchiveTable.updated_at.asc())
            .limit(limit)
        )
        return [_image_archive_record(row) for row in rows]

    def claim_cleanup(self, image_id: str, *, claimed_at: datetime) -> ImageArchiveRecord | None:
        result = self.session.execute(
            update(ImageArchiveTable)
            .where(
                ImageArchiveTable.image_id == image_id,
                ImageArchiveTable.cleanup_claimed_at.is_(None),
            )
            .values(cleanup_claimed_at=claimed_at)
        )
        if not _one_row_changed(result):
            return None
        self.session.flush()
        return self.get(image_id)

    def list_claimed(self) -> list[ImageArchiveRecord]:
        rows = self.session.scalars(
            select(ImageArchiveTable).where(ImageArchiveTable.cleanup_claimed_at.is_not(None))
        )
        return [_image_archive_record(row) for row in rows]

    def release_claim(self, image_id: str) -> None:
        self.session.execute(
            update(ImageArchiveTable)
            .where(ImageArchiveTable.image_id == image_id)
            .values(cleanup_claimed_at=None)
        )
        self.session.flush()

    def delete(self, image_id: str, *, expected_sha256: str) -> bool:
        result = self.session.execute(
            delete(ImageArchiveTable).where(
                ImageArchiveTable.image_id == image_id,
                ImageArchiveTable.sha256 == expected_sha256,
            )
        )
        self.session.flush()
        return _one_row_changed(result)


def _one_row_changed(result: object) -> bool:
    return int(result.rowcount) == 1 if isinstance(result, CursorResult) else False


def _image_archive_record(row: ImageArchiveTable) -> ImageArchiveRecord:
    format_version = row.payload.get("format_version")
    return ImageArchiveRecord(
        id=str(row.id),
        image_id=row.image_id,
        bucket=row.bucket,
        object_key=row.object_key,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        registry_ref=str(row.payload.get("registry_ref") or ""),
        manifest_digest=str(row.payload.get("manifest_digest") or ""),
        architecture=str(row.payload.get("architecture") or ""),
        format_version=(
            format_version
            if isinstance(format_version, int) and not isinstance(format_version, bool)
            else 1
        ),
        cleanup_claimed_at=row.cleanup_claimed_at,
    )


@dataclass(slots=True)
class ImageBuildRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[ImageBuildRecord]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(ImageBuildTable, ImageBuildRecord),
        )

    def upsert(
        self,
        build: ImageBuildRecord,
        *,
        workspace_id: str | None = None,
    ) -> ImageBuildRecord:
        """System-authority write keyed by build id; workers update placed builds."""
        resolved_workspace_id = workspace_id
        if resolved_workspace_id is None:
            row = self.session.get(ImageBuildTable, build.id)
            resolved_workspace_id = str(row.workspace_id) if row is not None else None
        if resolved_workspace_id is None:
            raise ValueError("image build persistence requires workspace ownership")
        CleanupRepository(self.session).assert_build_available(
            build,
            workspace_id=resolved_workspace_id,
        )
        saved = self.records.upsert_across_workspaces(
            build,
            workspace_id=resolved_workspace_id,
            status=build.status.value,
        )
        row = self.session.get(ImageBuildTable, build.id)
        if row is None:
            raise RuntimeError(f"image build persistence failed: {build.id}")
        _sync_image_build_row(row, build)
        if build.status not in {BuildStatus.Pending, BuildStatus.Running}:
            row.publication_claim_id = ""
            row.publication_claimed_at = None
        self.session.flush()
        return saved

    def get(self, build_id: str, *, workspace_id: str) -> ImageBuildRecord | None:
        return self.records.get(build_id, workspace_id=workspace_id)

    def get_across_workspaces(self, build_id: str) -> ImageBuildRecord | None:
        """System lookup for workers/reconcilers acting on placed builds."""
        return self.records.get_across_workspaces(build_id)

    def workspace_id(self, build_id: str) -> str | None:
        value = self.session.scalar(
            select(ImageBuildTable.workspace_id).where(ImageBuildTable.id == build_id)
        )
        return str(value) if value is not None else None

    def list(
        self,
        *,
        workspace_id: str,
        status: str | None = None,
    ) -> list[ImageBuildRecord]:
        return self.records.list(status=status, workspace_id=workspace_id)

    def list_across_workspaces(self, *, status: str | None = None) -> list[ImageBuildRecord]:
        """System listing for build reconciliation and retention."""
        return self.records.list_across_workspaces(status=status)

    def protected_artifact_resources(
        self,
        *,
        deleting_build_ids: set[str],
        paths: set[str],
        cache_keys: set[str],
    ) -> tuple[frozenset[str], frozenset[str]]:
        protected_paths: set[str] = set()
        if paths:
            path_digests = {archive_path_digest(path) for path in paths}
            statement = select(
                ImageBuildTable.archive_path_value,
                ImageBuildTable.manifest_path_value,
                ImageBuildTable.dockerfile_path_value,
                ImageBuildTable.cache_manifest_path_value,
            ).where(
                ImageBuildTable.id.not_in(deleting_build_ids),
                or_(
                    ImageBuildTable.archive_path_digest.in_(path_digests),
                    ImageBuildTable.manifest_path_digest.in_(path_digests),
                    ImageBuildTable.dockerfile_path_digest.in_(path_digests),
                    ImageBuildTable.cache_manifest_path_digest.in_(path_digests),
                ),
            )
            protected_paths = {
                value
                for row in self.session.execute(statement).tuples()
                for value in row
                if value in paths
            }
        protected_cache_keys: set[str] = set()
        if cache_keys:
            protected_cache_keys = set(
                self.session.scalars(
                    select(ImageBuildTable.cache_publish_key)
                    .where(
                        ImageBuildTable.id.not_in(deleting_build_ids),
                        ImageBuildTable.cache_publish_key.in_(cache_keys),
                    )
                    .distinct()
                )
            )
        return frozenset(protected_paths), frozenset(protected_cache_keys)

    def lock_fingerprint(self, fingerprint: str, *, workspace_id: str) -> None:
        if self.session.bind is None or self.session.bind.dialect.name != "postgresql":
            return
        lock_key = f"image-build:{workspace_id}:{fingerprint}"
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )

    def get_latest_by_fingerprint(
        self,
        fingerprint: str,
        *,
        workspace_id: str,
    ) -> ImageBuildRecord | None:
        statement = (
            select(ImageBuildTable)
            .where(
                ImageBuildTable.workspace_id == workspace_id,
                ImageBuildTable.fingerprint == fingerprint,
            )
            .order_by(ImageBuildTable.created_at.desc(), ImageBuildTable.id.asc())
            .limit(1)
        )
        row = self.session.scalars(statement).first()
        return ImageBuildRecord.model_validate(row.payload) if row is not None else None

    def get_active_by_fingerprint(
        self,
        fingerprint: str,
        *,
        workspace_id: str,
    ) -> ImageBuildRecord | None:
        row = self.session.scalars(
            select(ImageBuildTable)
            .where(
                ImageBuildTable.workspace_id == workspace_id,
                ImageBuildTable.fingerprint == fingerprint,
                ImageBuildTable.status.in_([BuildStatus.Pending.value, BuildStatus.Running.value]),
            )
            .order_by(ImageBuildTable.created_at.desc(), ImageBuildTable.id.asc())
            .limit(1)
        ).first()
        return ImageBuildRecord.model_validate(row.payload) if row is not None else None

    def list_completed_by_fingerprint(
        self,
        fingerprint: str,
        *,
        workspace_id: str,
        limit: int,
    ) -> list[ImageBuildRecord]:
        statement = (
            select(ImageBuildTable)
            .where(
                ImageBuildTable.workspace_id == workspace_id,
                ImageBuildTable.fingerprint == fingerprint,
                ImageBuildTable.status == BuildStatus.Complete.value,
            )
            .order_by(ImageBuildTable.created_at.desc(), ImageBuildTable.id.asc())
            .limit(max(limit, 0))
        )
        return [
            ImageBuildRecord.model_validate(row.payload) for row in self.session.scalars(statement)
        ]

    def get_latest_by_image_id(
        self,
        image_id: str,
        *,
        workspace_id: str,
    ) -> ImageBuildRecord | None:
        row = self.session.scalars(
            select(ImageBuildTable)
            .where(
                ImageBuildTable.workspace_id == workspace_id,
                ImageBuildTable.image_id == image_id,
            )
            .order_by(ImageBuildTable.created_at.desc(), ImageBuildTable.id.asc())
            .limit(1)
        ).first()
        return ImageBuildRecord.model_validate(row.payload) if row is not None else None

    def list_completed_by_image_id(
        self,
        image_id: str,
        *,
        workspace_id: str,
        limit: int,
    ) -> list[ImageBuildRecord]:
        statement = (
            select(ImageBuildTable)
            .where(
                ImageBuildTable.workspace_id == workspace_id,
                ImageBuildTable.image_id == image_id,
                ImageBuildTable.status == BuildStatus.Complete.value,
            )
            .order_by(ImageBuildTable.created_at.desc(), ImageBuildTable.id.asc())
            .limit(max(limit, 0))
        )
        return [
            ImageBuildRecord.model_validate(row.payload) for row in self.session.scalars(statement)
        ]

    def heartbeat_active(
        self,
        build_id: str,
        *,
        workspace_id: str,
        now: datetime | None = None,
    ) -> bool:
        heartbeat_at = now or utc_now()
        updated_id = self.session.scalar(
            update(ImageBuildTable)
            .where(
                ImageBuildTable.id == build_id,
                ImageBuildTable.workspace_id == workspace_id,
                ImageBuildTable.status.in_([BuildStatus.Pending.value, BuildStatus.Running.value]),
            )
            .values(
                updated_at=heartbeat_at,
                publication_claimed_at=case(
                    (ImageBuildTable.publication_claim_id != "", heartbeat_at),
                    else_=ImageBuildTable.publication_claimed_at,
                ),
            )
            .returning(ImageBuildTable.id)
        )
        return updated_id is not None

    def claim_publication(
        self,
        build_id: str,
        *,
        workspace_id: str,
        claim_id: str,
        claimed_at: datetime | None = None,
    ) -> bool:
        now = claimed_at or utc_now()
        updated_id = self.session.scalar(
            update(ImageBuildTable)
            .where(
                ImageBuildTable.id == build_id,
                ImageBuildTable.workspace_id == workspace_id,
                ImageBuildTable.status.in_([BuildStatus.Pending.value, BuildStatus.Running.value]),
                ImageBuildTable.publication_claim_id == "",
            )
            .values(
                publication_claim_id=claim_id,
                publication_claimed_at=now,
                updated_at=now,
            )
            .returning(ImageBuildTable.id)
        )
        return updated_id is not None

    def finalize_publication(
        self,
        build: ImageBuildRecord,
        *,
        workspace_id: str,
        claim_id: str,
        archive_published: bool = False,
    ) -> ImageBuildRecord:
        row = self.session.scalars(
            select(ImageBuildTable).where(
                ImageBuildTable.id == build.id,
                ImageBuildTable.workspace_id == workspace_id,
                ImageBuildTable.status.in_([BuildStatus.Pending.value, BuildStatus.Running.value]),
                ImageBuildTable.publication_claim_id == claim_id,
            )
        ).first()
        if row is None:
            raise RuntimeError("image build publication ownership was lost before completion")
        _sync_image_build_row(row, build)
        row.status = build.status.value
        row.phase = build.phase.value
        row.finished_at = build.finished_at
        row.updated_at = utc_now()
        row.payload = build.model_dump(mode="json")
        row.publication_claim_id = ""
        row.publication_claimed_at = None
        if archive_published:
            # The archive itself is global and was written when the upload was
            # reserved. What publication establishes here is this workspace's
            # authorization to resolve it.
            images = ImageRepository(self.session)
            existing = images.get(
                build.image_id or "",
                workspace_id=workspace_id,
            )
            images.upsert(
                ImageRecord(
                    id=existing.id if existing is not None else "",
                    workspace_id=workspace_id,
                    image_id=build.image_id or "",
                    clip_version=existing.clip_version if existing is not None else 1,
                    aliases=existing.aliases if existing is not None else [],
                )
            )
        self.session.flush()
        return build

    def get_claimed_publication(
        self,
        build_id: str,
        *,
        workspace_id: str,
        claim_id: str,
    ) -> ImageBuildRecord | None:
        row = self.session.scalars(
            select(ImageBuildTable).where(
                ImageBuildTable.id == build_id,
                ImageBuildTable.workspace_id == workspace_id,
                ImageBuildTable.status.in_([BuildStatus.Pending.value, BuildStatus.Running.value]),
                ImageBuildTable.publication_claim_id == claim_id,
            )
        ).first()
        return ImageBuildRecord.model_validate(row.payload) if row is not None else None

    def fail_stale_active(
        self,
        fingerprint: str,
        *,
        workspace_id: str,
        stale_before: datetime,
        publication_stale_before: datetime,
        now: datetime | None = None,
    ) -> list[ImageBuildRecord]:
        rows = list(
            self.session.scalars(
                select(ImageBuildTable).where(
                    ImageBuildTable.workspace_id == workspace_id,
                    ImageBuildTable.fingerprint == fingerprint,
                    ImageBuildTable.status.in_(
                        [BuildStatus.Pending.value, BuildStatus.Running.value]
                    ),
                    ImageBuildTable.updated_at <= stale_before,
                    or_(
                        ImageBuildTable.publication_claim_id == "",
                        ImageBuildTable.publication_claimed_at <= publication_stale_before,
                    ),
                )
            )
        )
        if not rows:
            return []
        finished_at = now or utc_now()
        reason = "image build ownership lease expired before completion"
        failed: list[ImageBuildRecord] = []
        for row in rows:
            record = ImageBuildRecord.model_validate(row.payload)
            if not record.logs or record.logs[-1] != reason:
                record.logs.append(reason)
            record.status = BuildStatus.Failed
            record.phase = ImageBuildPhase.Failed
            record.finished_at = finished_at
            record.error = reason
            row.status = record.status.value
            row.phase = record.phase.value
            row.finished_at = finished_at
            row.updated_at = finished_at
            row.payload = record.model_dump(mode="json")
            row.publication_claim_id = ""
            row.publication_claimed_at = None
            failed.append(record)
        self.session.flush()
        return failed

    def list_for_image_cleanup(
        self,
        image_id: str,
        *,
        workspace_id: str,
        claimed: bool,
        limit: int,
    ) -> list[ImageBuildRecord]:
        claim_clause = (
            ImageBuildTable.cleanup_claimed_at.is_not(None)
            if claimed
            else ImageBuildTable.cleanup_claimed_at.is_(None)
        )
        statement = (
            select(ImageBuildTable)
            .where(
                ImageBuildTable.workspace_id == workspace_id,
                ImageBuildTable.image_id == image_id,
                claim_clause,
            )
            .order_by(ImageBuildTable.created_at.asc(), ImageBuildTable.id.asc())
            .limit(limit)
        )
        return [
            ImageBuildRecord.model_validate(row.payload) for row in self.session.scalars(statement)
        ]

    def has_for_image(self, image_id: str, *, workspace_id: str) -> bool:
        return bool(
            self.session.scalar(
                select(
                    exists().where(
                        ImageBuildTable.workspace_id == workspace_id,
                        ImageBuildTable.image_id == image_id,
                    )
                )
            )
        )

    def count_for_image(self, image_id: str, *, workspace_id: str) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(ImageBuildTable)
                .where(
                    ImageBuildTable.workspace_id == workspace_id,
                    ImageBuildTable.image_id == image_id,
                )
            )
            or 0
        )

    def delete_across_workspaces(self, build_id: str) -> bool:
        """System retention deletion regardless of owning workspace."""
        return self.records.delete_across_workspaces(build_id)

    def delete_claimed_for_image(
        self,
        build_ids: list[str],
        *,
        image_id: str,
        workspace_id: str,
    ) -> int:
        if not build_ids:
            return 0
        WorkspaceRepository(self.session).lock_active_owner(workspace_id)
        result = self.session.execute(
            delete(ImageBuildTable).where(
                ImageBuildTable.workspace_id == workspace_id,
                ImageBuildTable.image_id == image_id,
                ImageBuildTable.id.in_(build_ids),
                ImageBuildTable.cleanup_claimed_at.is_not(None),
            )
        )
        return int(result.rowcount) if isinstance(result, CursorResult) else 0


def _sync_image_build_row(row: ImageBuildTable, build: ImageBuildRecord) -> None:
    row.created_at = build.created_at
    row.archive_path_value = normalize_runtime_path(build.artifact_path)
    row.archive_path_digest = archive_path_digest(row.archive_path_value)
    row.manifest_path_value = normalize_runtime_path(build.manifest_path)
    row.manifest_path_digest = archive_path_digest(row.manifest_path_value)
    row.dockerfile_path_value = normalize_runtime_path(
        build.cache_metadata.get("dockerfile_path", "")
    )
    row.dockerfile_path_digest = archive_path_digest(row.dockerfile_path_value)
    row.cache_manifest_path_value = normalize_runtime_path(
        build.cache_metadata.get("manifest_path", "")
    )
    row.cache_manifest_path_digest = archive_path_digest(row.cache_manifest_path_value)
    row.cache_publish_key = build.cache_metadata.get("cache_publish_key", "")


def _visible_checkpoint(
    record: CheckpointRecord | None,
    *,
    include_deleted: bool,
    include_claimed: bool,
) -> CheckpointRecord | None:
    if record is None:
        return None
    if record.deleted_at is not None and not include_deleted:
        return None
    if record.cleanup_claimed_at is not None and not include_claimed:
        return None
    return record


def _visible_checkpoints(
    records: list[CheckpointRecord],
    *,
    include_deleted: bool,
    include_claimed: bool,
) -> list[CheckpointRecord]:
    if not include_deleted:
        records = [record for record in records if record.deleted_at is None]
    if not include_claimed:
        records = [record for record in records if record.cleanup_claimed_at is None]
    records.sort(key=lambda item: (item.created_at, item.checkpoint_id), reverse=True)
    return records


@dataclass(slots=True)
class CheckpointRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[CheckpointRecord]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(CheckpointTable, CheckpointRecord, key_field="checkpoint_id"),
        )

    def upsert(self, checkpoint: CheckpointRecord) -> CheckpointRecord:
        """System-authority write keyed by checkpoint id; workers own checkpoint state."""
        CleanupRepository(self.session).assert_checkpoint_available(checkpoint.checkpoint_id)
        saved = self.records.upsert_across_workspaces(
            checkpoint,
            key=checkpoint.checkpoint_id,
            workspace_id=checkpoint.workspace_id or None,
            status=checkpoint.status.value,
        )
        row = self.session.scalars(
            select(CheckpointTable).where(CheckpointTable.checkpoint_id == checkpoint.checkpoint_id)
        ).one()
        row.stub_id = saved.stub_id or None
        row.retention_expires_at = checkpoint.retention_expires_at
        self.session.flush()
        return saved

    def create(self, checkpoint: CheckpointRecord) -> CheckpointRecord:
        if not checkpoint.checkpoint_id:
            msg = "checkpoint id is required"
            raise ValueError(msg)
        return self.upsert(checkpoint.model_copy(update={"updated_at": utc_now()}))

    def get(
        self,
        checkpoint_id: str,
        *,
        workspace_id: str,
        include_deleted: bool = False,
        include_claimed: bool = False,
    ) -> CheckpointRecord | None:
        return _visible_checkpoint(
            self.records.get(checkpoint_id, workspace_id=workspace_id),
            include_deleted=include_deleted,
            include_claimed=include_claimed,
        )

    def get_across_workspaces(
        self,
        checkpoint_id: str,
        *,
        include_deleted: bool = False,
        include_claimed: bool = False,
    ) -> CheckpointRecord | None:
        """System lookup for worker restore/cleanup paths."""
        return _visible_checkpoint(
            self.records.get_across_workspaces(checkpoint_id),
            include_deleted=include_deleted,
            include_claimed=include_claimed,
        )

    def list(
        self,
        *,
        workspace_id: str,
        include_deleted: bool = False,
        include_claimed: bool = False,
    ) -> list[CheckpointRecord]:
        return _visible_checkpoints(
            self.records.list(workspace_id=workspace_id),
            include_deleted=include_deleted,
            include_claimed=include_claimed,
        )

    def list_across_workspaces(
        self,
        *,
        include_deleted: bool = False,
        include_claimed: bool = False,
    ) -> list[CheckpointRecord]:
        """System listing for retention and worker restore paths."""
        return _visible_checkpoints(
            self.records.list_across_workspaces(),
            include_deleted=include_deleted,
            include_claimed=include_claimed,
        )

    def latest_for_stub(
        self,
        stub_id: str,
        *,
        include_deleted: bool = False,
    ) -> CheckpointRecord:
        """System lookup keyed by an already-authorized stub id."""
        candidates = [
            item
            for item in self.list_across_workspaces(include_deleted=include_deleted)
            if item.stub_id == stub_id
        ]
        if not candidates:
            msg = f"checkpoint not found for stub: {stub_id}"
            raise KeyError(msg)
        candidates.sort(key=lambda item: (item.created_at, item.checkpoint_id), reverse=True)
        return candidates[0]

    def latest_available_for_stub(
        self,
        *,
        workspace_id: str,
        stub_id: str,
    ) -> CheckpointRecord | None:
        row = self.session.scalars(
            select(CheckpointTable)
            .where(
                CheckpointTable.workspace_id == workspace_id,
                CheckpointTable.stub_id == stub_id,
                CheckpointTable.status == CheckpointStatus.Available.value,
                CheckpointTable.deleted_at.is_(None),
                CheckpointTable.cleanup_claimed_at.is_(None),
            )
            .order_by(CheckpointTable.created_at.desc(), CheckpointTable.id.desc())
            .limit(1)
        ).first()
        return CheckpointRecord.model_validate(row.payload) if row is not None else None

    def list_expired_for_retention(
        self,
        *,
        active_recent_stub_keys: list[str],
        now: datetime,
        limit: int,
    ) -> list[CheckpointRecord]:
        statement = (
            select(CheckpointTable)
            .where(
                CheckpointTable.deleted_at.is_(None),
                CheckpointTable.cleanup_claimed_at.is_(None),
                CheckpointTable.retention_expires_at.is_not(None),
                CheckpointTable.retention_expires_at <= now,
                CheckpointTable.status.in_(
                    status.value for status in CHECKPOINT_RETENTION_ELIGIBLE_STATUSES
                ),
            )
            .order_by(
                CheckpointTable.retention_expires_at.asc(),
                CheckpointTable.created_at.asc(),
                CheckpointTable.id.asc(),
            )
        )
        active_pairs = [
            tuple(key.split("|", maxsplit=1)) for key in active_recent_stub_keys if "|" in key
        ]
        if active_pairs:
            statement = statement.where(
                or_(
                    CheckpointTable.workspace_id.is_(None),
                    CheckpointTable.stub_id.is_(None),
                    tuple_(CheckpointTable.workspace_id, CheckpointTable.stub_id).not_in(
                        active_pairs
                    ),
                )
            )
        statement = statement.limit(limit)
        return [
            CheckpointRecord.model_validate(row.payload) for row in self.session.scalars(statement)
        ]

    def origin_is_referenced_elsewhere(
        self,
        *,
        workspace_id: str,
        origin_key: str,
        checkpoint_id: str,
    ) -> bool:
        statement = select(
            exists().where(
                CheckpointTable.workspace_id == workspace_id,
                CheckpointTable.origin_key == origin_key,
                CheckpointTable.checkpoint_id != checkpoint_id,
                CheckpointTable.deleted_at.is_(None),
            )
        )
        return bool(self.session.scalar(statement))

    def update(
        self,
        checkpoint_id: str,
        *,
        status: CheckpointStatus | None = None,
        last_restored_at: datetime | None = None,
    ) -> CheckpointRecord:
        current = self.records.get_across_workspaces(checkpoint_id)
        if current is None:
            msg = f"checkpoint not found: {checkpoint_id}"
            raise KeyError(msg)
        update: dict[str, datetime | CheckpointStatus] = {"updated_at": utc_now()}
        if status is not None:
            update["status"] = status
        if last_restored_at is not None:
            update["last_restored_at"] = last_restored_at
        return self.upsert(current.model_copy(update=update))

    def set_status(self, checkpoint_id: str, status: CheckpointStatus) -> CheckpointRecord:
        current = self.records.get_across_workspaces(checkpoint_id)
        if current is None:
            msg = f"checkpoint not found: {checkpoint_id}"
            raise KeyError(msg)
        updated = current.model_copy(update={"status": status})
        return self.upsert(updated)

    def prune(self, checkpoint_ids: list[str]) -> CheckpointPruneResult:
        if not checkpoint_ids:
            return CheckpointPruneResult(pruned=[])
        now = utc_now()
        pruned: list[CheckpointRecord] = []
        for checkpoint_id in sorted(set(checkpoint_ids)):
            row = self.session.scalars(
                select(CheckpointTable).where(CheckpointTable.checkpoint_id == checkpoint_id)
            ).first()
            if row is None:
                continue
            current = CheckpointRecord.model_validate(row.payload)
            if current.deleted_at is not None:
                continue
            updated = current.model_copy(
                update={
                    "deleted_at": now,
                    "updated_at": now,
                    "cleanup_claimed_at": None,
                }
            )
            row.payload = updated.model_dump(mode="json")
            row.deleted_at = now
            row.cleanup_claimed_at = None
            self.session.flush()
            pruned.append(updated)
        return CheckpointPruneResult(pruned=pruned)
