from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from database.mappers.images import (
    checkpoint_from_table,
    image_archive_from_table,
    image_build_from_table,
    image_from_table,
    write_checkpoint_row,
    write_image_build_row,
)
from database.repositories.cleanup import CleanupRepository
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
from shared.errors import ConflictError, NotFoundError
from shared.image_building.records import (
    BuildStatus,
    ImageArchiveRecord,
    ImageBuildRecord,
    ImageRecord,
)
from shared.runtime_paths import archive_path_digest
from shared.timestamps import utc_now
from sqlalchemy import delete, exists, func, or_, select, text, tuple_, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


@dataclass(slots=True)
class ImageRepository:
    session: Session

    def upsert(self, image: ImageRecord) -> ImageRecord:
        image = ImageRecord.model_validate(dict(image))
        WorkspaceRepository(self.session).lock_active_owner(image.workspace_id)
        CleanupRepository(self.session).assert_image_write_available(
            image.image_id, workspace_id=image.workspace_id
        )
        row = self.session.scalar(
            select(ImageTable).where(
                ImageTable.workspace_id == image.workspace_id,
                ImageTable.image_id == image.image_id,
            )
        )
        if row is None:
            row = ImageTable(workspace_id=image.workspace_id, image_id=image.image_id)
            self.session.add(row)
        row.clip_version = image.clip_version
        row.aliases = list(image.aliases)
        row.cleanup_claimed_at = image.cleanup_claimed_at
        row.cleanup_completed_at = image.cleanup_completed_at
        row.updated_at = utc_now()
        self.session.flush()
        return image_from_table(row)

    def get(
        self,
        image_id: str,
        *,
        workspace_id: str,
        include_cleaned: bool = False,
    ) -> ImageRecord | None:
        row = self.session.scalar(
            select(ImageTable).where(
                ImageTable.workspace_id == workspace_id,
                ImageTable.image_id == image_id,
            )
        )
        record = image_from_table(row) if row is not None else None
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
        return image_from_table(row) if row is not None else None

    def delete(self, image_id: str, *, workspace_id: str) -> bool:
        WorkspaceRepository(self.session).lock_active_owner(workspace_id)
        result = self.session.execute(
            delete(ImageTable).where(
                ImageTable.workspace_id == workspace_id,
                ImageTable.image_id == image_id,
            )
        )
        return _one_row_changed(result)

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
        row.cleanup_claimed_at = None
        row.cleanup_completed_at = completed_at
        self.session.flush()
        return True


@dataclass(slots=True)
class ImageArchiveRepository:
    """System-authority access to the one archive per image id.

    Tenant access goes through `get_authorized`, which requires the caller's
    own `images` row.
    """

    session: Session

    def get(self, image_id: str) -> ImageArchiveRecord | None:
        row = self.session.scalars(
            select(ImageArchiveTable).where(ImageArchiveTable.image_id == image_id)
        ).first()
        return image_archive_from_table(row) if row is not None else None

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
        return image_archive_from_table(row) if row is not None else None

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
            registry_ref=registry_ref,
            manifest_digest=manifest_digest,
            architecture=architecture,
            format_version=format_version,
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
        return image_archive_from_table(row), True

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
                registry_ref=registry_ref,
                manifest_digest=manifest_digest,
                architecture=architecture,
                format_version=format_version,
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
        return [image_archive_from_table(row) for row in rows]

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
        return [image_archive_from_table(row) for row in rows]

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


@dataclass(slots=True)
class ImageBuildRepository:
    session: Session

    def upsert(
        self,
        build: ImageBuildRecord,
        *,
        workspace_id: str | None = None,
    ) -> ImageBuildRecord:
        """System-authority write keyed by build id; workers update placed builds."""
        build = ImageBuildRecord.model_validate(dict(build))
        row = self.session.get(ImageBuildTable, build.id)
        owner_id = (
            workspace_id
            if workspace_id is not None
            else (row.workspace_id if row is not None else None)
        )
        if owner_id is None:
            raise ValueError("image build persistence requires workspace ownership")
        if row is not None and row.workspace_id != owner_id:
            raise ConflictError("image build ownership cannot change")
        WorkspaceRepository(self.session).lock_active_owner(owner_id)
        CleanupRepository(self.session).assert_build_available(build, workspace_id=owner_id)
        if row is None:
            row = ImageBuildTable(id=build.id, workspace_id=owner_id)
            self.session.add(row)
        write_image_build_row(row, build)
        row.updated_at = utc_now()
        if build.status not in {BuildStatus.Pending, BuildStatus.Running}:
            row.publication_claim_id = ""
            row.publication_claimed_at = None
            row.dispatch_payload = None
            row.dispatch_claim_id = None
        self.session.flush()
        return image_build_from_table(row)

    def get(self, build_id: str, *, workspace_id: str) -> ImageBuildRecord | None:
        row = self.session.scalar(
            select(ImageBuildTable).where(
                ImageBuildTable.id == build_id,
                ImageBuildTable.workspace_id == workspace_id,
            )
        )
        return image_build_from_table(row) if row is not None else None

    def lock_build(self, build_id: str, *, workspace_id: str) -> ImageBuildRecord:
        row = self.session.scalar(
            select(ImageBuildTable)
            .where(ImageBuildTable.id == build_id, ImageBuildTable.workspace_id == workspace_id)
            .with_for_update()
        )
        if row is None:
            raise NotFoundError("image build not found")
        return image_build_from_table(row)

    def get_across_workspaces(self, build_id: str) -> ImageBuildRecord | None:
        """System lookup for workers/reconcilers acting on placed builds."""
        row = self.session.get(ImageBuildTable, build_id)
        return image_build_from_table(row) if row is not None else None

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
        statement = select(ImageBuildTable).where(ImageBuildTable.workspace_id == workspace_id)
        if status is not None:
            statement = statement.where(ImageBuildTable.status == status)
        return [
            image_build_from_table(row)
            for row in self.session.scalars(
                statement.order_by(ImageBuildTable.created_at.desc(), ImageBuildTable.id)
            )
        ]

    def list_across_workspaces(self, *, status: str | None = None) -> list[ImageBuildRecord]:
        """System listing for build reconciliation and retention."""
        statement = select(ImageBuildTable)
        if status is not None:
            statement = statement.where(ImageBuildTable.status == status)
        return [
            image_build_from_table(row)
            for row in self.session.scalars(
                statement.order_by(ImageBuildTable.created_at.desc(), ImageBuildTable.id)
            )
        ]

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
        return image_build_from_table(row) if row is not None else None

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
        return image_build_from_table(row) if row is not None else None

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
        return [image_build_from_table(row) for row in self.session.scalars(statement)]

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
        return image_build_from_table(row) if row is not None else None

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
        return [image_build_from_table(row) for row in self.session.scalars(statement)]

    def claim_publication(
        self,
        build_id: str,
        *,
        workspace_id: str,
        claim_id: str,
        container_id: str | None,
        claimed_at: datetime | None = None,
    ) -> bool:
        now = claimed_at or utc_now()
        updated_id = self.session.scalar(
            update(ImageBuildTable)
            .where(
                ImageBuildTable.id == build_id,
                ImageBuildTable.workspace_id == workspace_id,
                ImageBuildTable.status.in_([BuildStatus.Pending.value, BuildStatus.Running.value]),
                ImageBuildTable.execution_container_id == container_id,
                or_(
                    ImageBuildTable.publication_claim_id == "",
                    ImageBuildTable.publication_claimed_at < now - timedelta(seconds=30),
                ),
            )
            .values(
                publication_claim_id=claim_id,
                publication_claimed_at=now,
                updated_at=now,
            )
            .returning(ImageBuildTable.id)
        )
        return updated_id is not None

    def release_publication(self, build_id: str, *, workspace_id: str, claim_id: str) -> None:
        self.session.execute(
            update(ImageBuildTable)
            .where(
                ImageBuildTable.id == build_id,
                ImageBuildTable.workspace_id == workspace_id,
                ImageBuildTable.publication_claim_id == claim_id,
            )
            .values(publication_claim_id="", publication_claimed_at=None)
        )

    def finalize_publication(
        self,
        build: ImageBuildRecord,
        *,
        workspace_id: str,
        claim_id: str,
        clip_version: int,
        archive_published: bool = False,
    ) -> ImageBuildRecord:
        build = ImageBuildRecord.model_validate(dict(build))
        WorkspaceRepository(self.session).lock_active_owner(workspace_id)
        row = self.session.scalars(
            select(ImageBuildTable)
            .where(
                ImageBuildTable.id == build.id,
                ImageBuildTable.workspace_id == workspace_id,
                ImageBuildTable.status.in_([BuildStatus.Pending.value, BuildStatus.Running.value]),
                ImageBuildTable.publication_claim_id == claim_id,
            )
            .with_for_update()
        ).first()
        if row is None:
            raise RuntimeError("image build publication ownership was lost before completion")
        write_image_build_row(row, build)
        row.status = build.status.value
        row.phase = build.phase.value
        row.finished_at = build.finished_at
        row.updated_at = utc_now()
        row.publication_claim_id = ""
        row.publication_claimed_at = None
        if archive_published:
            row.dispatch_payload = None
            row.dispatch_claim_id = None
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
                    clip_version=clip_version,
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
        return image_build_from_table(row) if row is not None else None

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
        return [image_build_from_table(row) for row in self.session.scalars(statement)]

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
        result = self.session.execute(delete(ImageBuildTable).where(ImageBuildTable.id == build_id))
        return _one_row_changed(result)

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


@dataclass(slots=True)
class CheckpointRepository:
    session: Session

    def upsert(self, checkpoint: CheckpointRecord) -> CheckpointRecord:
        """System-authority write keyed by checkpoint id; workers own checkpoint state."""
        checkpoint = CheckpointRecord.model_validate(dict(checkpoint))
        if checkpoint.workspace_id:
            WorkspaceRepository(self.session).lock_active_owner(checkpoint.workspace_id)
        CleanupRepository(self.session).assert_checkpoint_available(checkpoint.checkpoint_id)
        row = self.session.scalar(
            select(CheckpointTable).where(CheckpointTable.checkpoint_id == checkpoint.checkpoint_id)
        )
        if row is None:
            row = CheckpointTable(checkpoint_id=checkpoint.checkpoint_id)
            self.session.add(row)
        elif row.workspace_id != (checkpoint.workspace_id or None):
            raise ConflictError("checkpoint ownership cannot change")
        write_checkpoint_row(row, checkpoint)
        self.session.flush()
        return checkpoint_from_table(row)

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
        row = self.session.scalar(
            select(CheckpointTable).where(
                CheckpointTable.checkpoint_id == checkpoint_id,
                CheckpointTable.workspace_id == workspace_id,
            )
        )
        return _visible_checkpoint(
            checkpoint_from_table(row) if row is not None else None,
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
        row = self.session.scalar(
            select(CheckpointTable).where(CheckpointTable.checkpoint_id == checkpoint_id)
        )
        return _visible_checkpoint(
            checkpoint_from_table(row) if row is not None else None,
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
        statement = select(CheckpointTable).where(CheckpointTable.workspace_id == workspace_id)
        if not include_deleted:
            statement = statement.where(CheckpointTable.deleted_at.is_(None))
        if not include_claimed:
            statement = statement.where(CheckpointTable.cleanup_claimed_at.is_(None))
        statement = statement.order_by(
            CheckpointTable.created_at.desc(), CheckpointTable.checkpoint_id.desc()
        )
        return [checkpoint_from_table(row) for row in self.session.scalars(statement)]

    def list_across_workspaces(
        self,
        *,
        include_deleted: bool = False,
        include_claimed: bool = False,
    ) -> list[CheckpointRecord]:
        """System listing for retention and worker restore paths."""
        statement = select(CheckpointTable)
        if not include_deleted:
            statement = statement.where(CheckpointTable.deleted_at.is_(None))
        if not include_claimed:
            statement = statement.where(CheckpointTable.cleanup_claimed_at.is_(None))
        statement = statement.order_by(
            CheckpointTable.created_at.desc(), CheckpointTable.checkpoint_id.desc()
        )
        return [checkpoint_from_table(row) for row in self.session.scalars(statement)]

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
        return checkpoint_from_table(row) if row is not None else None

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
        return [checkpoint_from_table(row) for row in self.session.scalars(statement)]

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
        current = self.get_across_workspaces(
            checkpoint_id, include_deleted=True, include_claimed=True
        )
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
        current = self.get_across_workspaces(
            checkpoint_id, include_deleted=True, include_claimed=True
        )
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
            current = checkpoint_from_table(row)
            if current.deleted_at is not None:
                continue
            updated = current.model_copy(
                update={
                    "deleted_at": now,
                    "updated_at": now,
                    "cleanup_claimed_at": None,
                }
            )
            row.updated_at = now
            row.deleted_at = now
            row.cleanup_claimed_at = None
            self.session.flush()
            pruned.append(updated)
        return CheckpointPruneResult(pruned=pruned)
