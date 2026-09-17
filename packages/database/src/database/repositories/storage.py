from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import overload
from uuid import uuid4

from database.mappers.apps import stub_from_table
from database.mappers.containers import container_from_row
from database.mappers.images import image_build_from_table, image_from_table
from database.mappers.storage import object_from_table, volume_from_table, write_object_row
from database.repositories.cleanup import (
    CleanupRepository,
    object_location_lock_key,
)
from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import container_storage_release_pending
from database.tables.apps import AppTable, DeploymentTable, StubTable
from database.tables.execution import TaskTable
from database.tables.identity import WorkspaceTable
from database.tables.images import ImageBuildTable, ImageTable
from database.tables.orchestration import ContainerTable
from database.tables.storage import CacheEntryTable, ObjectTable, VolumeCleanupTable, VolumeTable
from shared.cache_records import CacheEntry
from shared.containers import ContainerRecord, ContainerStatus
from shared.errors import ConflictError, NotFoundError
from shared.identity import WorkspaceStatus
from shared.image_building.records import BuildStatus, ImageBuildRecord, ImageRecord
from shared.objects import ObjectRecord, ObjectWriteCommand
from shared.tasks import TaskStatus
from shared.timestamps import to_utc, utc_now
from shared.volumes import VolumeRecord
from sqlalchemy import (
    CompoundSelect,
    String,
    any_,
    case,
    cast,
    delete,
    exists,
    func,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import InstrumentedAttribute, Session
from sqlalchemy.sql.elements import ColumnElement


@dataclass(frozen=True, slots=True)
class VolumeMeteringTarget:
    id: str
    workspace_id: str
    workspace_name: str
    name: str
    size_bytes: int
    metered_at: datetime


@dataclass(frozen=True, slots=True)
class VolumeMeteringCheckpoint:
    id: str
    workspace_id: str
    name: str
    size_bytes: int
    metered_at: datetime
    deletion_requested_at: datetime | None


@dataclass(frozen=True, slots=True)
class OwnedObjectRecord:
    workspace_id: str
    record: ObjectRecord


@dataclass(frozen=True, slots=True)
class ObjectWriteClaim:
    record: ObjectRecord
    claim_id: str
    created: bool
    write_required: bool = True


@dataclass(frozen=True, slots=True)
class ObjectUploadLease:
    bucket: str
    key: str
    size: int
    upload_id: str


@dataclass(slots=True)
class ObjectRepository:
    session: Session

    def upsert(self, record: ObjectRecord, *, workspace_id: str) -> ObjectRecord:
        WorkspaceRepository(self.session).lock_active_owner(workspace_id)
        CleanupRepository(self.session).assert_object_write_available(
            record, workspace_id=workspace_id
        )
        return self._save(record, workspace_id=workspace_id)

    def _save(self, record: ObjectRecord, *, workspace_id: str) -> ObjectRecord:
        record = ObjectRecord.model_validate(dict(record))
        row = self.session.get(ObjectTable, record.id)
        if row is None:
            row = ObjectTable(id=record.id, workspace_id=workspace_id)
            self.session.add(row)
        elif row.workspace_id != workspace_id:
            raise ConflictError("object ownership cannot change")
        write_object_row(row, record)
        self.session.flush()
        return object_from_table(row)

    def begin_write(
        self,
        command: ObjectWriteCommand,
        *,
        workspace_id: str,
        object_id: str | None = None,
        overwrite: bool,
        reuse_existing: bool = False,
    ) -> ObjectWriteClaim:
        claims = CleanupRepository(self.session)
        claims.assert_object_location_available(
            workspace_id=workspace_id,
            bucket=command.bucket,
            key=command.key,
        )
        WorkspaceRepository(self.session).lock_active_owner(workspace_id)
        existing = self.get_by_bucket_key(
            command.bucket,
            command.key,
            workspace_id=workspace_id,
            include_operations=True,
        )
        if existing is not None and object_id is not None and existing.id != object_id:
            raise ConflictError(
                f"object location already has a different id: {command.bucket}/{command.key}"
            )
        if existing is not None and existing.artifact_task_id is not None:
            raise ConflictError("saved artifacts are immutable; save a new artifact")
        if existing is not None and not overwrite:
            if not _object_content_matches_write_command(existing, command):
                raise ConflictError(f"object already exists: {command.bucket}/{command.key}")
            if reuse_existing:
                return ObjectWriteClaim(
                    record=existing,
                    claim_id="",
                    created=False,
                    write_required=False,
                )
        claim_id = str(uuid4())
        claimed_at = utc_now()
        write_target = command.model_copy(deep=True)
        fields = command.model_dump(mode="json")
        if existing is None:
            record = self._save(
                ObjectRecord.model_validate(
                    {
                        **fields,
                        "id": object_id or str(uuid4()),
                        "write_claim_id": claim_id,
                        "write_claimed_at": claimed_at,
                        "write_created": True,
                        "write_target": write_target,
                    }
                ),
                workspace_id=workspace_id,
            )
            return ObjectWriteClaim(record=record, claim_id=claim_id, created=True)
        claimed = existing.model_copy(
            update={
                "write_claim_id": claim_id,
                "write_claimed_at": claimed_at,
                "write_created": False,
                "write_target": write_target,
            }
        )
        self._save(claimed, workspace_id=workspace_id)
        target = ObjectRecord.model_validate(
            {
                **existing.model_dump(),
                **fields,
                "write_claim_id": claim_id,
                "write_claimed_at": claimed_at,
                "write_created": False,
                "write_target": write_target,
            }
        )
        return ObjectWriteClaim(record=target, claim_id=claim_id, created=False)

    def complete_write(
        self, claim: ObjectWriteClaim, *, workspace_id: str, stored_at: datetime | None = None
    ) -> ObjectRecord:
        if not claim.write_required:
            return claim.record
        WorkspaceRepository(self.session).lock_object_write_completion_owner(workspace_id)
        claims = CleanupRepository(self.session)
        claims.lock_keys(
            {
                f"object:{claim.record.id}",
                object_location_lock_key(
                    workspace_id,
                    claim.record.bucket,
                    claim.record.key,
                ),
            }
        )
        row = self.session.get(ObjectTable, claim.record.id)
        if row is None or str(row.workspace_id) != workspace_id:
            raise ConflictError(f"object write claim was lost: {claim.record.id}")
        if row.write_claim_id != claim.claim_id:
            raise ConflictError(f"object write claim was replaced: {claim.record.id}")
        completed = claim.record.model_copy(
            update={
                "write_claim_id": "",
                "write_upload_id": "",
                "write_claimed_at": None,
                "write_created": False,
                "write_target": None,
            }
        )
        if completed.artifact_task_id is not None and completed.artifact_stored_at is None:
            stored = stored_at or utc_now()
            completed.artifact_stored_at = stored
            completed.artifact_metered_at = (
                max(to_utc(completed.artifact_metered_at), to_utc(stored))
                if completed.artifact_metered_at is not None
                else stored
            )
            if completed.artifact_retention_seconds is not None:
                completed.artifact_expires_at = stored + timedelta(
                    seconds=completed.artifact_retention_seconds
                )
        write_object_row(row, completed)
        self.session.flush()
        return object_from_table(row)

    def abort_write(self, claim: ObjectWriteClaim, *, workspace_id: str) -> None:
        if not claim.write_required:
            return
        WorkspaceRepository(self.session).lock_object_write_completion_owner(workspace_id)
        claims = CleanupRepository(self.session)
        claims.lock_keys(
            {
                f"object:{claim.record.id}",
                object_location_lock_key(
                    workspace_id,
                    claim.record.bucket,
                    claim.record.key,
                ),
            }
        )
        row = self.session.get(ObjectTable, claim.record.id)
        if row is None or str(row.workspace_id) != workspace_id:
            return
        if row.write_claim_id != claim.claim_id:
            return
        if claim.created:
            self.session.delete(row)
            self.session.flush()
            return
        row.write_claim_id = ""
        row.write_upload_id = ""
        row.write_claimed_at = None
        row.write_created = False
        current = object_from_table(row)
        write_object_row(row, current)
        self.session.flush()

    def bind_upload(self, claim: ObjectWriteClaim, upload_id: str, *, workspace_id: str) -> None:
        row = self.session.scalar(
            select(ObjectTable)
            .where(
                ObjectTable.id == claim.record.id,
                ObjectTable.workspace_id == workspace_id,
            )
            .with_for_update()
        )
        if row is None or row.write_claim_id != claim.claim_id:
            raise ConflictError("object write claim was replaced")
        row.write_upload_id = upload_id
        self.session.flush()

    def renew_upload(
        self, object_id: str, claim_id: str, *, workspace_id: str
    ) -> ObjectUploadLease:
        WorkspaceRepository(self.session).lock_active_owner(workspace_id)
        CleanupRepository(self.session).lock_keys({f"object:{object_id}"})
        row = self.session.execute(
            update(ObjectTable)
            .where(
                ObjectTable.id == object_id,
                ObjectTable.workspace_id == workspace_id,
                ObjectTable.write_claim_id == claim_id,
                ObjectTable.write_upload_id != "",
                ObjectTable.write_target_size.is_not(None),
            )
            .values(write_claimed_at=utc_now())
            .returning(
                ObjectTable.bucket,
                ObjectTable.key,
                ObjectTable.write_target_size,
                ObjectTable.write_upload_id,
            )
        ).one_or_none()
        if row is None or row.write_target_size is None:
            raise NotFoundError("active object upload not found")
        return ObjectUploadLease(
            bucket=row.bucket,
            key=row.key,
            size=row.write_target_size,
            upload_id=row.write_upload_id,
        )

    def list_stale_write_claims(
        self,
        *,
        claimed_before: datetime,
        limit: int,
    ) -> list[OwnedObjectRecord]:
        statement = (
            select(ObjectTable)
            .where(
                ObjectTable.write_claimed_at.is_not(None),
                ObjectTable.write_claimed_at <= claimed_before,
            )
            .order_by(ObjectTable.write_claimed_at.asc(), ObjectTable.id.asc())
            .limit(limit)
        )
        return [
            OwnedObjectRecord(
                workspace_id=str(row.workspace_id),
                record=object_from_table(row),
            )
            for row in self.session.scalars(statement)
        ]

    def list_stale_delete_claims(
        self,
        *,
        claimed_before: datetime,
        cleanup_kind: str,
        limit: int,
    ) -> list[OwnedObjectRecord]:
        statement = (
            select(ObjectTable)
            .where(
                ObjectTable.cleanup_claimed_at.is_not(None),
                ObjectTable.cleanup_claimed_at <= claimed_before,
                ObjectTable.cleanup_kind == cleanup_kind,
            )
            .order_by(ObjectTable.cleanup_claimed_at.asc(), ObjectTable.id.asc())
            .limit(limit)
        )
        return [
            OwnedObjectRecord(
                workspace_id=str(row.workspace_id),
                record=object_from_table(row),
            )
            for row in self.session.scalars(statement)
        ]

    def reserve(
        self,
        command: ObjectWriteCommand,
        *,
        workspace_id: str,
        overwrite: bool,
    ) -> ObjectRecord:
        CleanupRepository(self.session).assert_object_location_available(
            workspace_id=workspace_id,
            bucket=command.bucket,
            key=command.key,
        )
        existing = self.get_by_bucket_key(
            command.bucket,
            command.key,
            workspace_id=workspace_id,
        )
        if existing is not None and existing.artifact_task_id is not None:
            raise ConflictError("saved artifacts are immutable; save a new artifact")
        if existing is not None and not overwrite:
            return existing
        fields = command.model_dump(mode="json")
        WorkspaceRepository(self.session).lock_active_owner(workspace_id)
        if existing is None:
            record = ObjectRecord.model_validate({**fields, "id": str(uuid4())})
        else:
            record = ObjectRecord.model_validate({**existing.model_dump(), **fields})
        return self._save(record, workspace_id=workspace_id)

    def claim_delete(
        self,
        object_id: str,
        *,
        cleanup_kind: str,
        claimed_at: datetime,
    ) -> ObjectRecord:
        owned = self.get_owned(object_id, include_operations=True)
        if owned is None:
            raise KeyError(object_id)
        WorkspaceRepository(self.session).lock_active_owner(owned.workspace_id)
        return self._claim_delete(
            owned,
            cleanup_kind=cleanup_kind,
            claimed_at=claimed_at,
        )

    def claim_delete_for_workspace_deletion(
        self,
        object_id: str,
        *,
        workspace_id: str,
        cleanup_kind: str,
        claimed_at: datetime,
    ) -> ObjectRecord:
        owned = self.get_owned(object_id, include_operations=True)
        if owned is None or owned.workspace_id != workspace_id:
            raise KeyError(object_id)
        workspace = WorkspaceRepository(self.session).lock_for_deletion(workspace_id)
        if workspace.status is not WorkspaceStatus.Deleting:
            raise ConflictError(f"workspace cleanup requires deleting state: {workspace_id}")
        return self._claim_delete(
            owned,
            cleanup_kind=cleanup_kind,
            claimed_at=claimed_at,
        )

    def _claim_delete(
        self,
        owned: OwnedObjectRecord,
        *,
        cleanup_kind: str,
        claimed_at: datetime,
    ) -> ObjectRecord:
        claims = CleanupRepository(self.session)
        claims.lock_keys(
            {
                f"object:{owned.record.id}",
                object_location_lock_key(
                    owned.workspace_id,
                    owned.record.bucket,
                    owned.record.key,
                ),
            }
        )
        return claims.mark_object_claimed(
            owned.record.id,
            claimed_at=claimed_at,
            cleanup_kind=cleanup_kind,
        )

    def release_delete_claim(self, object_id: str, *, cleanup_kind: str) -> bool:
        owned = self.get_owned(object_id, include_operations=True)
        if owned is None:
            return False
        claims = CleanupRepository(self.session)
        claims.lock_keys(
            {
                f"object:{object_id}",
                object_location_lock_key(
                    owned.workspace_id,
                    owned.record.bucket,
                    owned.record.key,
                ),
            }
        )
        row = self.session.get(ObjectTable, object_id)
        if row is None:
            return False
        current = object_from_table(row)
        if current.cleanup_kind != cleanup_kind:
            raise ConflictError(f"object delete claim was replaced: {object_id}")
        row.cleanup_kind = ""
        row.cleanup_claimed_at = None
        self.session.flush()
        return True

    def get_location(self, object_id: str, *, workspace_id: str) -> tuple[str, str] | None:
        row = self.session.execute(
            select(ObjectTable.bucket, ObjectTable.key).where(
                ObjectTable.id == object_id, ObjectTable.workspace_id == workspace_id
            )
        ).one_or_none()
        return (row.bucket, row.key) if row is not None else None

    def get(
        self,
        object_id: str,
        *,
        workspace_id: str,
        include_operations: bool = False,
    ) -> ObjectRecord | None:
        row = self.session.scalar(
            select(ObjectTable).where(
                ObjectTable.id == object_id,
                ObjectTable.workspace_id == workspace_id,
            )
        )
        record = object_from_table(row) if row is not None else None
        if record is not None and not include_operations and _object_operation_active(record):
            return None
        return record

    def get_owned(
        self,
        object_id: str,
        *,
        include_operations: bool = False,
    ) -> OwnedObjectRecord | None:
        row = self.session.get(ObjectTable, object_id)
        if row is None:
            return None
        owned = OwnedObjectRecord(
            workspace_id=str(row.workspace_id),
            record=object_from_table(row),
        )
        if not include_operations and _object_operation_active(owned.record):
            return None
        return owned

    def get_by_bucket_key(
        self,
        bucket: str,
        key: str,
        *,
        workspace_id: str,
        include_operations: bool = False,
    ) -> ObjectRecord | None:
        row = self.session.scalars(
            select(ObjectTable).where(
                ObjectTable.workspace_id == workspace_id,
                ObjectTable.bucket == bucket,
                ObjectTable.key == key,
            )
        ).first()
        record = object_from_table(row) if row is not None else None
        if record is not None and not include_operations and _object_operation_active(record):
            return None
        return record

    def find_by_sha256(
        self,
        sha256: str,
        *,
        workspace_id: str,
        bucket: str | None = None,
    ) -> ObjectRecord | None:
        statement = select(ObjectTable).where(
            ObjectTable.workspace_id == workspace_id,
            ObjectTable.sha256 == sha256,
        )
        if bucket is not None:
            statement = statement.where(ObjectTable.bucket == bucket)
        row = self.session.scalars(
            statement.order_by(ObjectTable.created_at.desc(), ObjectTable.id.asc()).limit(1)
        ).first()
        return object_from_table(row) if row is not None else None

    def list(self, *, workspace_id: str) -> list[ObjectRecord]:
        statement = (
            select(ObjectTable)
            .where(
                ObjectTable.workspace_id == workspace_id,
                ObjectTable.write_claimed_at.is_(None),
                ObjectTable.cleanup_claimed_at.is_(None),
            )
            .order_by(ObjectTable.created_at.desc(), ObjectTable.id)
        )
        return [object_from_table(row) for row in self.session.scalars(statement)]

    def list_for_workspace_deletion(self, workspace_id: str) -> list[ObjectRecord]:
        """System cleanup listing that retains active operation records."""
        statement = (
            select(ObjectTable)
            .where(ObjectTable.workspace_id == workspace_id)
            .order_by(ObjectTable.created_at.desc(), ObjectTable.id)
        )
        return [object_from_table(row) for row in self.session.scalars(statement)]

    def list_source_object_ids_for_deletion(
        self,
        *,
        workspace_id: str,
        source_bucket: str,
    ) -> list[str]:
        """Snapshot all source rows, including writes admitted before the fence."""
        return [
            str(value)
            for value in self.session.scalars(
                select(ObjectTable.id)
                .where(
                    ObjectTable.workspace_id == workspace_id,
                    ObjectTable.bucket == source_bucket,
                )
                .order_by(ObjectTable.id)
            )
        ]

    def workspace_has_write_claims(self, workspace_id: str) -> bool:
        return bool(
            self.session.scalar(
                select(
                    exists().where(
                        ObjectTable.workspace_id == workspace_id,
                        ObjectTable.write_claimed_at.is_not(None),
                    )
                )
            )
        )

    def workspace_has_objects(self, workspace_id: str) -> bool:
        return bool(
            self.session.scalar(select(exists().where(ObjectTable.workspace_id == workspace_id)))
        )

    def delete(self, object_id: str, *, workspace_id: str) -> bool:
        WorkspaceRepository(self.session).lock_active_owner(workspace_id)
        return (
            self.session.scalar(
                delete(ObjectTable)
                .where(
                    ObjectTable.id == object_id,
                    ObjectTable.workspace_id == workspace_id,
                )
                .returning(ObjectTable.id)
            )
            is not None
        )

    def delete_across_workspaces(self, object_id: str) -> bool:
        """System retention/cleanup deletion regardless of owning workspace."""
        return (
            self.session.scalar(
                delete(ObjectTable).where(ObjectTable.id == object_id).returning(ObjectTable.id)
            )
            is not None
        )

    def delete_for_workspace_deletion(
        self,
        object_id: str,
        *,
        workspace_id: str,
        cleanup_kind: str,
    ) -> bool:
        """Delete one claimed row under its workspace's deletion authority."""
        workspace = WorkspaceRepository(self.session).lock_for_deletion(workspace_id)
        if workspace.status is not WorkspaceStatus.Deleting:
            raise ConflictError(f"workspace cleanup requires deleting state: {workspace_id}")
        row = self.session.get(ObjectTable, object_id)
        if row is None:
            return False
        if str(row.workspace_id) != workspace_id:
            raise ConflictError(f"object is not owned by deleting workspace: {object_id}")
        record = object_from_table(row)
        if record.cleanup_kind != cleanup_kind:
            raise ConflictError(f"object delete claim was replaced: {object_id}")
        self.session.delete(row)
        self.session.flush()
        return True


def _object_operation_active(record: ObjectRecord) -> bool:
    return record.write_claimed_at is not None or record.cleanup_claimed_at is not None


def _object_content_matches_write_command(
    record: ObjectRecord,
    command: ObjectWriteCommand,
) -> bool:
    """Whether a stored object holds the same bytes this command is writing.

    Conflict means different content at one key, so only content identity
    participates. Content type and metadata are deliberately excluded: a caller
    that re-sends identical bytes with different metadata already dedupes to the
    existing record when the stored object is intact, and a repair of the same
    bytes must not be rejected for describing them differently.
    """
    return (
        record.bucket == command.bucket
        and record.key == command.key
        and record.path == command.path
        and record.size == command.size
        and record.sha256 == command.sha256
    )


@dataclass(slots=True)
class ObjectReferenceRepository:
    session: Session

    def list_source_cleanup_candidates(
        self,
        *,
        source_bucket: str,
        created_before: datetime,
        recent_build_after: datetime,
        excluded_object_ids: frozenset[str],
        limit: int,
    ) -> list[OwnedObjectRecord]:
        statement = select(ObjectTable).where(
            ObjectTable.created_at < created_before,
            ObjectTable.cleanup_claimed_at.is_(None),
            ObjectTable.write_claimed_at.is_(None),
            (ObjectTable.bucket == source_bucket) & ObjectTable.key.startswith("sources/"),
            ~_source_object_reference_exists(
                self.session,
                recent_build_after=recent_build_after,
            ),
        )
        if excluded_object_ids:
            statement = statement.where(ObjectTable.id.not_in(excluded_object_ids))
        statement = statement.order_by(ObjectTable.created_at.asc(), ObjectTable.id.asc()).limit(
            limit
        )
        return [
            OwnedObjectRecord(
                workspace_id=str(row.workspace_id),
                record=object_from_table(row),
            )
            for row in self.session.scalars(statement)
        ]

    def list_image_cleanup_candidates(
        self,
        *,
        updated_before: datetime,
        recent_build_after: datetime,
        excluded_image_ids: frozenset[str],
        limit: int,
    ) -> list[ImageRecord]:
        statement = select(ImageTable).where(
            ImageTable.updated_at < updated_before,
            ImageTable.cleanup_claimed_at.is_(None),
            ImageTable.cleanup_completed_at.is_(None),
            ~_image_reference_exists(
                self.session,
                ImageTable.image_id,
                ImageTable.workspace_id,
                recent_build_after=recent_build_after,
            ),
        )
        if excluded_image_ids:
            statement = statement.where(ImageTable.image_id.not_in(excluded_image_ids))
        statement = statement.order_by(ImageTable.updated_at.asc(), ImageTable.id.asc()).limit(
            limit
        )
        return [image_from_table(row) for row in self.session.scalars(statement)]

    def list_build_cleanup_candidates(
        self,
        *,
        finished_before: datetime,
        recent_build_after: datetime,
        excluded_build_ids: frozenset[str],
        limit: int,
    ) -> list[ImageBuildRecord]:
        live_stub_ids = _live_stub_ids()
        image_is_unreferenced = or_(
            ImageBuildTable.image_id.is_(None),
            ~_direct_image_reference_clause(
                self.session,
                live_stub_ids,
                ImageBuildTable.image_id,
                ImageBuildTable.workspace_id,
            ),
        )
        statement = select(ImageBuildTable).where(
            ImageBuildTable.status.not_in([BuildStatus.Pending.value, BuildStatus.Running.value]),
            ImageBuildTable.finished_at.is_not(None),
            ImageBuildTable.finished_at < finished_before,
            ImageBuildTable.finished_at < recent_build_after,
            ImageBuildTable.created_at < finished_before,
            ImageBuildTable.cleanup_claimed_at.is_(None),
            image_is_unreferenced,
        )
        if excluded_build_ids:
            statement = statement.where(ImageBuildTable.id.not_in(excluded_build_ids))
        statement = statement.order_by(
            ImageBuildTable.finished_at.asc(),
            ImageBuildTable.created_at.asc(),
            ImageBuildTable.id.asc(),
        )
        return [image_build_from_table(row) for row in self.session.scalars(statement.limit(limit))]

    def object_is_referenced(
        self,
        object_id: str,
        *,
        workspace_id: str,
        recent_build_after: datetime,
    ) -> bool:
        live_stub_ids = _live_stub_ids()
        stub_reference = self.session.scalar(
            select(
                exists().where(
                    StubTable.id.in_(live_stub_ids),
                    StubTable.workspace_id == workspace_id,
                    or_(
                        StubTable.object_id == object_id,
                        StubTable.image_context_object_id == object_id,
                        any_(StubTable.copied_object_ids) == object_id,
                        any_(StubTable.config_copied_object_ids) == object_id,
                    ),
                )
            )
        )
        if stub_reference:
            return True
        return bool(
            self.session.scalar(
                select(
                    exists().where(
                        ImageBuildTable.workspace_id == workspace_id,
                        ImageBuildTable.context_object_id == object_id,
                        or_(
                            ImageBuildTable.status.in_(
                                [BuildStatus.Pending.value, BuildStatus.Running.value]
                            ),
                            ImageBuildTable.finished_at.is_(None),
                            ImageBuildTable.finished_at >= recent_build_after,
                            _direct_image_reference_clause(
                                self.session,
                                live_stub_ids,
                                ImageBuildTable.image_id,
                                workspace_id,
                            ),
                        ),
                    )
                )
            )
        )

    def image_is_referenced(
        self,
        image_id: str,
        *,
        workspace_id: str,
        recent_build_after: datetime,
    ) -> bool:
        live_stub_ids = _live_stub_ids()
        if self.session.scalar(
            select(literal(True))
            .where(
                _direct_image_reference_clause(
                    self.session,
                    live_stub_ids,
                    image_id,
                    workspace_id,
                )
            )
            .limit(1)
        ):
            return True
        return bool(
            self.session.scalar(
                select(
                    exists().where(
                        ImageBuildTable.workspace_id == workspace_id,
                        ImageBuildTable.image_id == image_id,
                        or_(
                            ImageBuildTable.status.in_(
                                [BuildStatus.Pending.value, BuildStatus.Running.value]
                            ),
                            ImageBuildTable.finished_at.is_(None),
                            ImageBuildTable.finished_at >= recent_build_after,
                        ),
                    )
                )
            )
        )

    def build_is_retained(
        self,
        build: ImageBuildRecord,
        *,
        workspace_id: str,
        recent_build_after: datetime,
    ) -> bool:
        if build.status in {BuildStatus.Pending, BuildStatus.Running}:
            return True
        if build.finished_at is None or build.finished_at >= recent_build_after:
            return True
        if not build.image_id:
            return False
        live_stub_ids = _live_stub_ids()
        return bool(
            self.session.scalar(
                select(literal(True))
                .where(
                    _direct_image_reference_clause(
                        self.session,
                        live_stub_ids,
                        build.image_id,
                        workspace_id,
                    )
                )
                .limit(1)
            )
        )


@dataclass(slots=True)
class VolumeRepository:
    session: Session

    def create(self, name: str, *, workspace_id: str) -> tuple[VolumeRecord, bool]:
        """Return the named volume and whether this transaction created it."""
        WorkspaceRepository(self.session).lock_active_owner(workspace_id)
        created = self.session.scalar(
            postgresql_insert(VolumeTable)
            .values(name=name, workspace_id=workspace_id)
            .on_conflict_do_nothing(constraint="uq_volumes_workspace_name")
            .returning(VolumeTable)
        )
        if created is not None:
            return volume_from_table(created), True
        existing = self.get(name, workspace_id=workspace_id)
        if existing is None:
            raise ConflictError("volume changed during creation; retry the request")
        return existing, False

    def get(self, name: str, *, workspace_id: str) -> VolumeRecord | None:
        row = self.session.scalar(
            select(VolumeTable).where(
                VolumeTable.workspace_id == workspace_id,
                VolumeTable.name == name,
            )
        )
        return volume_from_table(row) if row is not None else None

    def list(self, *, workspace_id: str) -> list[VolumeRecord]:
        return [
            volume_from_table(row)
            for row in self.session.scalars(
                select(VolumeTable).where(VolumeTable.workspace_id == workspace_id)
            )
        ]

    def lock(self, name: str, *, workspace_id: str, allow_deleting: bool = False) -> VolumeTable:
        row = self.session.scalar(
            select(VolumeTable)
            .where(
                VolumeTable.workspace_id == workspace_id,
                VolumeTable.name == name,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise NotFoundError(f"volume not found: {name}")
        if row.deletion_requested_at is not None and not allow_deleting:
            raise ConflictError(f"volume {name} is deleting")
        return row

    def lock_mounts(self, names: set[str], *, workspace_id: str) -> None:
        expected_ids: set[str] = set()
        for name in sorted(names):
            record, _ = self.create(name, workspace_id=workspace_id)
            if record.deletion_requested_at is not None:
                raise ConflictError(f"volume {name} is deleting")
            expected_ids.add(record.id)
        rows = self.session.scalars(
            select(VolumeTable)
            .where(
                VolumeTable.workspace_id == workspace_id,
                VolumeTable.name.in_(names),
            )
            .order_by(VolumeTable.id)
            .with_for_update()
        )
        locked_ids: set[str] = set()
        for row in rows:
            if row.deletion_requested_at is not None:
                raise ConflictError(f"volume {row.name} is deleting")
            locked_ids.add(str(row.id))
        if locked_ids != expected_ids:
            raise ConflictError("volumes changed during container admission; retry the request")

    def list_deletions(self, *, limit: int) -> tuple[tuple[str, str], ...]:
        return tuple(
            self.session.execute(
                select(VolumeTable.workspace_id, VolumeTable.name)
                .where(VolumeTable.deletion_requested_at.is_not(None))
                .order_by(VolumeTable.updated_at, VolumeTable.id)
                .limit(limit)
            ).tuples()
        )

    def unreleased_mounts(self, name: str, *, workspace_id: str) -> tuple[ContainerRecord, ...]:
        rows = self.session.execute(
            select(ContainerTable, StubTable)
            .join(StubTable, StubTable.id == ContainerTable.stub_id)
            .where(
                ContainerTable.workspace_id == workspace_id,
                container_storage_release_pending(),
            )
        )
        records: list[ContainerRecord] = []
        for container_row, stub_row in rows:
            stub = stub_from_table(stub_row)
            if any((volume.name or volume.id) == name for volume in stub.config.volumes):
                records.append(container_from_row(container_row))
        return tuple(records)

    def retain_cleanup(self, row: VolumeTable) -> None:
        if row.unfenced_writes_possible and self.session.get(VolumeCleanupTable, row.id) is None:
            self.session.add(VolumeCleanupTable(volume_id=row.id, workspace_id=row.workspace_id))

    def list_cleanup(self, *, swept_before: datetime, limit: int) -> tuple[tuple[str, str], ...]:
        return tuple(
            self.session.execute(
                select(VolumeCleanupTable.workspace_id, VolumeCleanupTable.volume_id)
                .where(VolumeCleanupTable.swept_at <= swept_before)
                .order_by(VolumeCleanupTable.swept_at, VolumeCleanupTable.volume_id)
                .limit(limit)
            ).tuples()
        )

    def lock_cleanup(self, volume_id: str, *, workspace_id: str) -> VolumeCleanupTable | None:
        return self.session.scalar(
            select(VolumeCleanupTable)
            .where(
                VolumeCleanupTable.volume_id == volume_id,
                VolumeCleanupTable.workspace_id == workspace_id,
            )
            .with_for_update(skip_locked=True)
        )

    def has_cleanup(self, workspace_id: str) -> bool:
        return bool(
            self.session.scalar(
                select(exists().where(VolumeCleanupTable.workspace_id == workspace_id))
            )
        )

    def retire_cleanup(self, workspace_id: str) -> None:
        self.session.execute(
            delete(VolumeCleanupTable).where(VolumeCleanupTable.workspace_id == workspace_id)
        )

    def list_metering_targets(
        self,
        *,
        metered_before: datetime,
        limit: int,
    ) -> tuple[VolumeMeteringTarget, ...]:
        statement = (
            select(VolumeTable, WorkspaceTable.name)
            .join(WorkspaceTable, WorkspaceTable.id == VolumeTable.workspace_id)
            .where(
                VolumeTable.metered_at <= metered_before,
                VolumeTable.deletion_requested_at.is_(None),
            )
            .order_by(VolumeTable.metered_at.asc(), VolumeTable.id.asc())
            .limit(limit)
        )
        return tuple(
            self._metering_target(row, workspace_name)
            for row, workspace_name in self.session.execute(statement).tuples()
        )

    def get_metering_target(
        self,
        name: str,
        *,
        workspace_id: str,
    ) -> VolumeMeteringTarget | None:
        statement = (
            select(VolumeTable, WorkspaceTable.name)
            .join(WorkspaceTable, WorkspaceTable.id == VolumeTable.workspace_id)
            .where(
                VolumeTable.workspace_id == workspace_id,
                VolumeTable.name == name,
            )
        )
        result = self.session.execute(statement).tuples().first()
        if result is None:
            return None
        row, workspace_name = result
        return self._metering_target(row, workspace_name)

    def lock_metering_checkpoint(self, volume_id: str) -> VolumeMeteringCheckpoint | None:
        row = self.session.scalars(
            select(VolumeTable).where(VolumeTable.id == volume_id).with_for_update()
        ).first()
        if row is None:
            return None
        return VolumeMeteringCheckpoint(
            id=str(row.id),
            workspace_id=str(row.workspace_id),
            name=row.name,
            size_bytes=row.size_bytes,
            metered_at=row.metered_at,
            deletion_requested_at=row.deletion_requested_at,
        )

    def advance_metering_checkpoint(
        self,
        volume_id: str,
        *,
        size_bytes: int,
        metered_at: datetime,
    ) -> None:
        row = self.session.get(VolumeTable, volume_id)
        if row is None:
            return
        row.size_bytes = size_bytes
        row.metered_at = metered_at
        self.session.flush()

    @staticmethod
    def _metering_target(
        row: VolumeTable,
        workspace_name: str,
    ) -> VolumeMeteringTarget:
        return VolumeMeteringTarget(
            id=str(row.id),
            workspace_id=str(row.workspace_id),
            workspace_name=workspace_name,
            name=row.name,
            size_bytes=row.size_bytes,
            metered_at=row.metered_at,
        )


@dataclass(slots=True)
class CacheEntryRepository:
    session: Session

    def lock(self, key: str) -> None:
        CleanupRepository(self.session).lock_keys({f"cache:{key}"})

    def upsert(self, record: CacheEntry) -> CacheEntry:
        statement = (
            postgresql_insert(CacheEntryTable)
            .values(
                key=record.key,
                path=record.path,
                size=record.size,
                sha256=record.sha256,
                hits=record.hits,
                expires_at=record.expires_at,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
            .on_conflict_do_update(
                index_elements=[CacheEntryTable.key],
                set_={
                    "path": record.path,
                    "size": record.size,
                    "sha256": record.sha256,
                    "hits": record.hits,
                    "expires_at": record.expires_at,
                    "updated_at": case(
                        (CacheEntryTable.updated_at < record.updated_at, record.updated_at),
                        else_=CacheEntryTable.updated_at,
                    ),
                },
            )
            .returning(CacheEntryTable)
            .execution_options(populate_existing=True)
        )
        return _cache_entry_from_row(self.session.scalars(statement).one())

    def get(self, key: str) -> CacheEntry | None:
        row = self.session.scalars(
            select(CacheEntryTable).where(CacheEntryTable.key == key)
        ).first()
        return _cache_entry_from_row(row) if row is not None else None

    def increment_hits(self, key: str, *, updated_at: datetime) -> CacheEntry | None:
        statement = (
            update(CacheEntryTable)
            .where(CacheEntryTable.key == key)
            .values(
                hits=CacheEntryTable.hits + 1,
                updated_at=case(
                    (CacheEntryTable.updated_at < updated_at, updated_at),
                    else_=CacheEntryTable.updated_at,
                ),
            )
            .returning(CacheEntryTable)
            .execution_options(populate_existing=True)
        )
        row = self.session.scalars(statement).first()
        return _cache_entry_from_row(row) if row is not None else None

    def list(self, *, limit: int | None = None) -> list[CacheEntry]:
        statement = select(CacheEntryTable).order_by(
            CacheEntryTable.created_at.desc(),
            CacheEntryTable.id.asc(),
        )
        if limit is not None:
            statement = statement.limit(limit)
        return [_cache_entry_from_row(row) for row in self.session.scalars(statement)]

    def path_is_referenced(self, path: str) -> bool:
        return bool(self.session.scalar(select(exists().where(CacheEntryTable.path == path))))

    def list_expired(self, *, now: datetime, limit: int) -> list[CacheEntry]:
        statement = (
            select(CacheEntryTable)
            .where(
                CacheEntryTable.expires_at.is_not(None),
                CacheEntryTable.expires_at <= now,
            )
            .order_by(CacheEntryTable.expires_at.asc(), CacheEntryTable.id.asc())
            .limit(limit)
        )
        return [_cache_entry_from_row(row) for row in self.session.scalars(statement)]

    def delete(self, key: str) -> bool:
        row = self.session.scalars(
            select(CacheEntryTable).where(CacheEntryTable.key == key)
        ).first()
        if row is None:
            return False
        self.session.delete(row)
        self.session.flush()
        return True


def _cache_entry_from_row(row: CacheEntryTable) -> CacheEntry:
    return CacheEntry(
        key=row.key,
        path=row.path,
        size=row.size,
        sha256=row.sha256,
        hits=row.hits,
        expires_at=_utc_datetime(row.expires_at),
        created_at=_utc_datetime(row.created_at),
        updated_at=_utc_datetime(row.updated_at),
    )


@overload
def _utc_datetime(value: datetime) -> datetime: ...


@overload
def _utc_datetime(value: None) -> None: ...


def _utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _live_stub_ids() -> CompoundSelect[tuple[str | None]]:
    return (
        select(AppTable.stub_id)
        .where(AppTable.deleted_at.is_(None), AppTable.stub_id.is_not(None))
        .union(
            select(DeploymentTable.stub_id).where(
                DeploymentTable.deleted_at.is_(None),
                DeploymentTable.stub_id.is_not(None),
            ),
            select(ContainerTable.stub_id).where(
                ContainerTable.status.in_(
                    [ContainerStatus.Pending.value, ContainerStatus.Running.value]
                ),
                ContainerTable.stub_id.is_not(None),
            ),
            select(TaskTable.stub_id).where(
                TaskTable.status.in_(
                    [TaskStatus.Pending.value, TaskStatus.Running.value, TaskStatus.Retry.value]
                ),
                TaskTable.stub_id.is_not(None),
            ),
        )
    )


def _build_active_or_recent(
    recent_build_after: datetime,
) -> ColumnElement[bool]:
    return or_(
        ImageBuildTable.status.in_([BuildStatus.Pending.value, BuildStatus.Running.value]),
        ImageBuildTable.finished_at.is_(None),
        ImageBuildTable.finished_at >= recent_build_after,
    )


def _source_object_reference_exists(
    session: Session,
    *,
    recent_build_after: datetime,
) -> ColumnElement[bool]:
    object_id = _uuid_text_without_hyphens(ObjectTable.id)
    live_stub_ids = _live_stub_ids()
    stub_reference = (
        exists()
        .where(
            StubTable.id.in_(live_stub_ids),
            StubTable.workspace_id == ObjectTable.workspace_id,
            or_(
                StubTable.object_id == ObjectTable.id,
                StubTable.image_context_object_id == ObjectTable.id,
                ObjectTable.id == any_(StubTable.copied_object_ids),
                ObjectTable.id == any_(StubTable.config_copied_object_ids),
            ),
        )
        .correlate(ObjectTable)
    )
    build_reference = (
        exists()
        .where(
            ImageBuildTable.workspace_id == ObjectTable.workspace_id,
            _uuid_text_without_hyphens(ImageBuildTable.context_object_id) == object_id,
            or_(
                _build_active_or_recent(recent_build_after),
                _direct_image_reference_clause(
                    session,
                    live_stub_ids,
                    ImageBuildTable.image_id,
                    ObjectTable.workspace_id,
                ),
            ),
        )
        .correlate(ObjectTable)
    )
    # No archive arm: image archives are not objects. They live in `image_archives`
    # with their own reference predicate, because an object reference correlated on
    # workspace cannot answer a question about bytes no workspace owns.
    return or_(stub_reference, build_reference)


def _image_reference_exists(
    session: Session,
    image_id: ColumnElement[str] | InstrumentedAttribute[str],
    workspace_id: ColumnElement[str] | InstrumentedAttribute[str],
    *,
    recent_build_after: datetime,
) -> ColumnElement[bool]:
    live_stub_ids = _live_stub_ids()
    direct_reference = _direct_image_reference_clause(
        session,
        live_stub_ids,
        image_id,
        workspace_id,
    )
    build_reference = (
        exists()
        .where(
            ImageBuildTable.workspace_id == workspace_id,
            ImageBuildTable.image_id == image_id,
            _build_active_or_recent(recent_build_after),
        )
        .correlate(ImageTable)
    )
    return or_(direct_reference, build_reference)


def _direct_image_reference_clause(
    session: Session,
    live_stub_ids: CompoundSelect[tuple[str | None]],
    image_id: (
        str
        | ColumnElement[str]
        | ColumnElement[str | None]
        | InstrumentedAttribute[str]
        | InstrumentedAttribute[str | None]
    ),
    workspace_id: (
        str
        | ColumnElement[str]
        | ColumnElement[str | None]
        | InstrumentedAttribute[str]
        | InstrumentedAttribute[str | None]
    ),
) -> ColumnElement[bool]:
    stub_reference = exists().where(
        StubTable.id.in_(live_stub_ids),
        StubTable.workspace_id == workspace_id,
        or_(StubTable.image_id == image_id, StubTable.runtime_image_id == image_id),
    )
    container_reference = exists().where(
        ContainerTable.workspace_id == workspace_id,
        ContainerTable.status.in_([ContainerStatus.Pending.value, ContainerStatus.Running.value]),
        ContainerTable.image == image_id,
        ContainerTable.image != "",
    )
    return or_(stub_reference, container_reference)


def _uuid_text_without_hyphens(
    value: ColumnElement[str | None] | InstrumentedAttribute[str | None],
) -> ColumnElement[str]:
    return func.replace(cast(value, String), "-", "", type_=String)
