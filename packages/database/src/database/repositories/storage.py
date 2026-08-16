from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import overload
from uuid import uuid4

from database.repositories.cleanup import (
    CleanupRepository,
    object_location_lock_key,
)
from database.repositories.common import (
    TableRepositoryConfig,
    WorkspaceTableRepository,
)
from database.repositories.identity import WorkspaceRepository
from database.tables.apps import AppTable, DeploymentTable, StubTable
from database.tables.execution import TaskTable
from database.tables.identity import WorkspaceTable
from database.tables.images import ImageBuildTable, ImageTable
from database.tables.orchestration import ContainerTable
from database.tables.storage import CacheEntryTable, ObjectTable, VolumeTable
from pydantic import JsonValue
from shared.cache_records import CacheEntry
from shared.containers import ContainerStatus
from shared.errors import ConflictError
from shared.identity import WorkspaceRecord, WorkspaceStatus
from shared.image_building.records import BuildStatus, ImageBuildRecord, ImageRecord
from shared.objects import ObjectRecord, ObjectWriteCommand
from shared.tasks import TaskStatus
from shared.timestamps import utc_now
from shared.volumes import VolumeRecord
from sqlalchemy import (
    CompoundSelect,
    String,
    case,
    cast,
    delete,
    exists,
    func,
    literal,
    or_,
    select,
    type_coerce,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import InstrumentedAttribute, Session
from sqlalchemy.orm.attributes import flag_modified
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


@dataclass(slots=True)
class ObjectRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[ObjectRecord]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(ObjectTable, ObjectRecord),
        )

    def upsert(self, record: ObjectRecord, *, workspace_id: str) -> ObjectRecord:
        CleanupRepository(self.session).assert_object_write_available(
            record,
            workspace_id=workspace_id,
        )
        return self.records.upsert(record, workspace_id=workspace_id)

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
        payload = _object_write_payload(command)
        if existing is None:
            record = self.records.create(
                {
                    **payload,
                    **({"id": object_id} if object_id is not None else {}),
                    "write_claim_id": claim_id,
                    "write_claimed_at": claimed_at,
                    "write_created": True,
                    "write_target": write_target,
                },
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
        self.records.upsert(claimed, workspace_id=workspace_id)
        target = existing.model_copy(
            update={
                **payload,
                "write_claim_id": claim_id,
                "write_claimed_at": claimed_at,
                "write_created": False,
                "write_target": write_target,
            }
        )
        return ObjectWriteClaim(record=target, claim_id=claim_id, created=False)

    def complete_write(self, claim: ObjectWriteClaim, *, workspace_id: str) -> ObjectRecord:
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
                "write_claimed_at": None,
                "write_created": False,
                "write_target": None,
            }
        )
        _write_object_row(row, completed)
        self.session.flush()
        return completed

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
        current = ObjectRecord.model_validate(row.payload).model_copy(
            update={
                "write_claim_id": "",
                "write_claimed_at": None,
                "write_created": False,
                "write_target": None,
            }
        )
        row.payload = current.model_dump(mode="json")
        row.write_claim_id = ""
        row.write_claimed_at = None
        flag_modified(row, "payload")
        self.session.flush()

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
                record=ObjectRecord.model_validate(row.payload),
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
                record=ObjectRecord.model_validate(row.payload),
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
        if existing is not None and not overwrite:
            return existing
        payload = _object_write_payload(command)
        if existing is None:
            return self.records.create(payload, workspace_id=workspace_id)
        return self.records.upsert(existing.model_copy(update=payload), workspace_id=workspace_id)

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
        current = ObjectRecord.model_validate(row.payload)
        if current.cleanup_kind != cleanup_kind:
            raise ConflictError(f"object delete claim was replaced: {object_id}")
        released = current.model_copy(update={"cleanup_kind": "", "cleanup_claimed_at": None})
        row.payload = released.model_dump(mode="json")
        row.cleanup_kind = ""
        row.cleanup_claimed_at = None
        flag_modified(row, "payload")
        self.session.flush()
        return True

    def get(
        self,
        object_id: str,
        *,
        workspace_id: str,
        include_operations: bool = False,
    ) -> ObjectRecord | None:
        record = self.records.get(object_id, workspace_id=workspace_id)
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
            record=ObjectRecord.model_validate(row.payload),
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
        record = ObjectRecord.model_validate(row.payload) if row is not None else None
        if record is not None and not include_operations and _object_operation_active(record):
            return None
        return record

    def list_by_location(self, bucket: str, key: str) -> list[OwnedObjectRecord]:
        statement = select(ObjectTable).where(
            ObjectTable.bucket == bucket,
            ObjectTable.key == key,
        )
        return [
            OwnedObjectRecord(
                workspace_id=str(row.workspace_id),
                record=ObjectRecord.model_validate(row.payload),
            )
            for row in self.session.scalars(statement)
        ]

    def find_by_sha256(
        self,
        sha256: str,
        *,
        workspace_id: str,
        bucket: str | None = None,
    ) -> ObjectRecord | None:
        for record in self.records.list(workspace_id=workspace_id):
            if record.sha256 == sha256 and (bucket is None or record.bucket == bucket):
                return record
        return None

    def list(self, *, workspace_id: str) -> list[ObjectRecord]:
        return [
            record
            for record in self.records.list(workspace_id=workspace_id)
            if not _object_operation_active(record)
        ]

    def list_for_workspace_deletion(self, workspace_id: str) -> list[ObjectRecord]:
        """System cleanup listing that retains active operation records."""
        return self.records.list(workspace_id=workspace_id)

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

    def list_owned(self) -> list[OwnedObjectRecord]:
        """System retention listing over every workspace's objects."""
        return [
            OwnedObjectRecord(
                workspace_id=str(row.workspace_id),
                record=ObjectRecord.model_validate(row.payload),
            )
            for row in self.session.scalars(select(ObjectTable))
        ]

    def delete(self, object_id: str, *, workspace_id: str) -> bool:
        return self.records.delete(object_id, workspace_id=workspace_id)

    def delete_across_workspaces(self, object_id: str) -> bool:
        """System retention/cleanup deletion regardless of owning workspace."""
        return self.records.delete_across_workspaces(object_id)

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
        record = ObjectRecord.model_validate(row.payload)
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


def _object_write_payload(command: ObjectWriteCommand) -> dict[str, JsonValue]:
    """Copy one validated command into the durable object-record fields."""
    metadata: dict[str, JsonValue] = {}
    metadata.update(command.metadata)
    return {
        "bucket": command.bucket,
        "key": command.key,
        "path": command.path,
        "size": command.size,
        "sha256": command.sha256,
        "content_type": command.content_type,
        "metadata": metadata,
    }


def _write_object_row(row: ObjectTable, record: ObjectRecord) -> None:
    row.payload = record.model_dump(mode="json")
    row.bucket = record.bucket
    row.key = record.key
    row.path = record.path
    row.size = record.size
    row.sha256 = record.sha256
    row.content_type = record.content_type
    row.write_claim_id = record.write_claim_id
    row.write_claimed_at = record.write_claimed_at
    row.cleanup_kind = record.cleanup_kind
    row.cleanup_claimed_at = record.cleanup_claimed_at
    row.updated_at = utc_now()
    flag_modified(row, "payload")


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
                record=ObjectRecord.model_validate(row.payload),
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
        return [ImageRecord.model_validate(row.payload) for row in self.session.scalars(statement)]

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
        return [
            ImageBuildRecord.model_validate(row.payload)
            for row in self.session.scalars(statement.limit(limit))
        ]

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
                        _stub_json_text(self.session, "config", "object_id") == object_id,
                        _stub_json_text(
                            self.session,
                            "config",
                            "image",
                            "context_object_id",
                        )
                        == object_id,
                        _stub_payload_contains(
                            self.session, {"metadata": {"copied_object_ids": [object_id]}}
                        ),
                        _stub_payload_contains(
                            self.session,
                            {"config": {"metadata": {"copied_object_ids": [object_id]}}},
                        ),
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
                        _image_build_json_text(self.session, "image", "context_object_id")
                        == object_id,
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

    @property
    def records(self) -> WorkspaceTableRepository[VolumeRecord]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(VolumeTable, VolumeRecord, key_field="name"),
        )

    def upsert(self, record: VolumeRecord, *, workspace_id: str) -> VolumeRecord:
        return self.records.upsert(
            record,
            key=record.name,
            workspace_id=workspace_id,
            name=record.name,
        )

    def create(self, name: str, *, workspace_id: str) -> tuple[VolumeRecord, bool]:
        """Insert this volume, or return the one that beat us to the name.

        Returns whether this call is the one that created it, because the caller
        publishes a change and admits a new billed thing off that answer — doing
        either for a volume somebody else created would announce a creation twice.

        The lookup callers do before this one is not a lock. Workspace scoping
        takes `FOR KEY SHARE`, which does not serialize writers, so two containers
        mounting the same new volume name during an autoscaler ramp both read
        absence and both insert. `uq_volumes_workspace_name` is what makes that
        safe, and catching it here is what turns the loser's container start from
        an unmapped `IntegrityError` into the volume it was asking for.
        """

        return self.records.create_or_existing(
            {"name": name},
            workspace_id=workspace_id,
            name=name,
            existing=lambda: self.get(name, workspace_id=workspace_id),
        )

    def get(self, name: str, *, workspace_id: str) -> VolumeRecord | None:
        return self.records.get(name, workspace_id=workspace_id)

    def list(self, *, workspace_id: str) -> list[VolumeRecord]:
        return self.records.list(workspace_id=workspace_id)

    def delete(self, name: str, *, workspace_id: str) -> bool:
        return self.records.delete(name, workspace_id=workspace_id)

    def delete_for_workspace_deletion(self, name: str, *, workspace_id: str) -> bool:
        workspace = WorkspaceRepository(self.session).lock_for_deletion(workspace_id)
        if workspace.status is not WorkspaceStatus.Deleting:
            raise ConflictError(f"workspace cleanup requires deleting state: {workspace_id}")
        result = self.session.execute(
            delete(VolumeTable).where(
                VolumeTable.workspace_id == workspace_id,
                VolumeTable.name == name,
            )
        )
        self.session.flush()
        return isinstance(result, CursorResult) and result.rowcount > 0

    def list_metering_targets(
        self,
        *,
        metered_before: datetime,
        limit: int,
    ) -> tuple[VolumeMeteringTarget, ...]:
        statement = (
            select(VolumeTable, WorkspaceTable)
            .join(WorkspaceTable, WorkspaceTable.id == VolumeTable.workspace_id)
            .where(VolumeTable.metered_at <= metered_before)
            .order_by(VolumeTable.metered_at.asc(), VolumeTable.id.asc())
            .limit(limit)
        )
        return tuple(
            self._metering_target(row, WorkspaceRecord.model_validate(workspace.payload))
            for row, workspace in self.session.execute(statement).tuples()
        )

    def get_metering_target(
        self,
        name: str,
        *,
        workspace_id: str,
    ) -> VolumeMeteringTarget | None:
        statement = (
            select(VolumeTable, WorkspaceTable)
            .join(WorkspaceTable, WorkspaceTable.id == VolumeTable.workspace_id)
            .where(
                VolumeTable.workspace_id == workspace_id,
                VolumeTable.name == name,
            )
        )
        result = self.session.execute(statement).tuples().first()
        if result is None:
            return None
        row, workspace = result
        return self._metering_target(
            row,
            WorkspaceRecord.model_validate(workspace.payload),
        )

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
        workspace: WorkspaceRecord,
    ) -> VolumeMeteringTarget:
        return VolumeMeteringTarget(
            id=str(row.id),
            workspace_id=str(row.workspace_id),
            workspace_name=workspace.name,
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
        if self.session.get_bind().dialect.name == "postgresql":
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
        if self.session.get_bind().dialect.name == "sqlite":
            statement = (
                sqlite_insert(CacheEntryTable)
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
        raise RuntimeError("cache entries require PostgreSQL or SQLite")

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
                _uuid_text_without_hyphens(_stub_json_text(session, "config", "object_id"))
                == object_id,
                _uuid_text_without_hyphens(
                    _stub_json_text(session, "config", "image", "context_object_id")
                )
                == object_id,
                _stub_json_array_contains(
                    session,
                    ("metadata", "copied_object_ids"),
                    object_id,
                ),
                _stub_json_array_contains(
                    session,
                    ("config", "metadata", "copied_object_ids"),
                    object_id,
                ),
            ),
        )
        .correlate(ObjectTable)
    )
    build_reference = (
        exists()
        .where(
            ImageBuildTable.workspace_id == ObjectTable.workspace_id,
            _uuid_text_without_hyphens(
                _image_build_json_text(session, "image", "context_object_id")
            )
            == object_id,
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


def _stub_json_array_contains(
    session: Session,
    path: tuple[str, ...],
    value: ColumnElement[str],
) -> ColumnElement[bool]:
    if session.get_bind().dialect.name == "postgresql":
        payload = type_coerce(StubTable.payload, JSONB)
        array_value = func.jsonb_extract_path(payload, *path, type_=JSONB)
        safe_array = case(
            (func.jsonb_typeof(array_value) == "array", array_value),
            else_=cast(literal("[]"), JSONB),
        )
        values = func.jsonb_array_elements_text(safe_array).table_valued("value").alias()
    else:
        json_path = "$." + ".".join(path)
        safe_array = case(
            (
                func.json_type(StubTable.payload, json_path) == "array",
                func.json_extract(StubTable.payload, json_path),
            ),
            else_=literal("[]"),
        )
        values = func.json_each(safe_array).table_valued("key", "value").alias()
    return exists(
        select(literal(True))
        .select_from(values)
        .where(_uuid_text_without_hyphens(values.c.value) == value)
    ).correlate(StubTable, ObjectTable)


def _stub_payload_contains(
    session: Session,
    value: dict[str, JsonValue],
) -> ColumnElement[bool]:
    if session.get_bind().dialect.name == "postgresql":
        return type_coerce(StubTable.payload, JSONB).contains(value)
    return StubTable.payload.contains(value)


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
    stub_image_id = _stub_json_text(session, "config", "image", "image_id")
    runtime_image_id = _stub_json_text(session, "config", "runtime", "image_id")
    stub_reference = exists().where(
        StubTable.id.in_(live_stub_ids),
        StubTable.workspace_id == workspace_id,
        or_(stub_image_id == image_id, runtime_image_id == image_id),
    )
    container_reference = exists().where(
        ContainerTable.workspace_id == workspace_id,
        ContainerTable.status.in_([ContainerStatus.Pending.value, ContainerStatus.Running.value]),
        ContainerTable.image == image_id,
        ContainerTable.image != "",
    )
    return or_(stub_reference, container_reference)


def _stub_json_text(session: Session, *path: str) -> ColumnElement[str]:
    if session.get_bind().dialect.name == "postgresql":
        return func.jsonb_extract_path_text(StubTable.payload, *path, type_=String)
    return func.json_extract(StubTable.payload, "$." + ".".join(path), type_=String)


def _image_build_json_text(session: Session, *path: str) -> ColumnElement[str]:
    if session.get_bind().dialect.name == "postgresql":
        return func.jsonb_extract_path_text(ImageBuildTable.payload, *path, type_=String)
    return func.json_extract(ImageBuildTable.payload, "$." + ".".join(path), type_=String)


def _uuid_text_without_hyphens(
    value: ColumnElement[str] | InstrumentedAttribute[str],
) -> ColumnElement[str]:
    return func.replace(cast(value, String), "-", "", type_=String)
