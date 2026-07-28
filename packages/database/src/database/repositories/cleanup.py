from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from database.records.apps import StubRecord
from database.tables.apps import StubTable
from database.tables.images import CheckpointTable, ImageBuildTable, ImageTable
from database.tables.storage import ObjectTable
from pydantic import BaseModel, JsonValue
from shared.checkpoints import CheckpointRecord
from shared.errors import ConflictError
from shared.image_building.records import ImageBuildRecord, ImageRecord
from shared.objects import ObjectRecord
from shared.runtime_paths import archive_path_digest, normalize_runtime_path
from shared.workload_config import StubConfig
from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.sql.elements import ColumnElement

OBJECT_CLEANUP_CHECKPOINT = "checkpoint-retention"
OBJECT_CLEANUP_DELETE = "object-delete"
OBJECT_CLEANUP_SOURCE = "source-retention"


def object_location_lock_key(workspace_id: str, bucket: str, key: str) -> str:
    return f"location:{workspace_id}:{bucket}/{key}"


@dataclass(slots=True)
class CleanupRepository:
    session: Session

    def lock_keys(self, keys: set[str]) -> None:
        if self.session.bind is None or self.session.bind.dialect.name != "postgresql":
            return
        for key in sorted(keys):
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"artifact-cleanup:{key}"},
            )

    def assert_references_available(
        self,
        *,
        workspace_id: str,
        object_ids: set[str],
        image_ids: set[str],
    ) -> None:
        self.lock_keys(
            {f"object:{object_id}" for object_id in object_ids}
            | {f"image:{workspace_id}:{image_id}" for image_id in image_ids}
        )
        object_rows = (
            list(
                self.session.scalars(
                    select(ObjectTable).where(
                        ObjectTable.workspace_id == workspace_id,
                        ObjectTable.id.in_(object_ids),
                    )
                )
            )
            if object_ids
            else []
        )
        missing_objects = sorted(object_ids - {str(row.id) for row in object_rows})
        claimed_objects = [str(row.id) for row in object_rows if row.cleanup_claimed_at is not None]
        claimed_images = (
            [
                row.image_id
                for row in self.session.scalars(
                    select(ImageTable).where(
                        ImageTable.workspace_id == workspace_id,
                        ImageTable.image_id.in_(image_ids),
                        (
                            ImageTable.cleanup_claimed_at.is_not(None)
                            | ImageTable.cleanup_completed_at.is_not(None)
                        ),
                    )
                )
            ]
            if image_ids
            else []
        )
        if missing_objects:
            raise ConflictError(f"artifact is unavailable: {', '.join(missing_objects)}")
        claimed = sorted([*claimed_objects, *claimed_images])
        if claimed:
            raise ConflictError(f"artifact cleanup is in progress: {', '.join(claimed)}")

    def assert_stub_config_available(
        self,
        config: StubConfig,
        *,
        workspace_id: str,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> None:
        self.assert_references_available(
            workspace_id=workspace_id,
            object_ids=_stub_object_ids(config, metadata=metadata),
            image_ids=_stub_image_ids(config),
        )

    def assert_image_write_available(self, image_id: str, *, workspace_id: str) -> None:
        self.lock_keys({f"image:{workspace_id}:{image_id}"})
        claimed = self.session.scalars(
            select(ImageTable).where(
                ImageTable.workspace_id == workspace_id,
                ImageTable.image_id == image_id,
                ImageTable.cleanup_claimed_at.is_not(None),
            )
        ).first()
        if claimed is not None:
            raise ConflictError(f"image cleanup is in progress: {image_id}")

    def assert_stub_available(self, stub_id: str) -> None:
        row = self.session.get(StubTable, stub_id)
        if row is None:
            return
        stub = StubRecord.model_validate(row.payload)
        self.assert_stub_config_available(
            stub.config,
            workspace_id=str(row.workspace_id),
            metadata=stub.metadata,
        )

    def assert_object_write_available(
        self,
        record: ObjectRecord,
        *,
        workspace_id: str,
    ) -> None:
        location_key = object_location_lock_key(workspace_id, record.bucket, record.key)
        self.lock_keys({f"object:{record.id}", location_key})
        claimed = self.session.scalars(
            select(ObjectTable).where(
                ObjectTable.workspace_id == workspace_id,
                (
                    (ObjectTable.id == record.id)
                    | ((ObjectTable.bucket == record.bucket) & (ObjectTable.key == record.key))
                ),
                (
                    ObjectTable.cleanup_claimed_at.is_not(None)
                    | ObjectTable.write_claimed_at.is_not(None)
                ),
            )
        ).first()
        if claimed is not None:
            raise ConflictError(f"object operation is in progress: {record.bucket}/{record.key}")

    def assert_object_location_available(
        self,
        *,
        workspace_id: str,
        bucket: str,
        key: str,
    ) -> None:
        self.lock_keys({object_location_lock_key(workspace_id, bucket, key)})
        claimed = self.session.scalars(
            select(ObjectTable).where(
                ObjectTable.workspace_id == workspace_id,
                ObjectTable.bucket == bucket,
                ObjectTable.key == key,
                (
                    ObjectTable.cleanup_claimed_at.is_not(None)
                    | ObjectTable.write_claimed_at.is_not(None)
                ),
            )
        ).first()
        if claimed is not None:
            raise ConflictError(f"object operation is in progress: {bucket}/{key}")

    def assert_checkpoint_available(self, checkpoint_id: str) -> None:
        self.lock_keys({f"checkpoint:{checkpoint_id}"})
        claimed = self.session.scalars(
            select(CheckpointTable).where(
                CheckpointTable.checkpoint_id == checkpoint_id,
                CheckpointTable.cleanup_claimed_at.is_not(None),
            )
        ).first()
        if claimed is not None:
            raise ConflictError(f"checkpoint cleanup is in progress: {checkpoint_id}")

    def assert_build_available(self, build: ImageBuildRecord, *, workspace_id: str) -> None:
        resource_keys = {f"build:{build.id}"}
        resource_keys.update(_build_resource_keys(build))
        if build.image.context_object_id:
            resource_keys.add(f"object:{build.image.context_object_id}")
        if build.image_id:
            resource_keys.add(f"image:{workspace_id}:{build.image_id}")
        self.lock_keys(resource_keys)
        self.assert_references_available(
            workspace_id=workspace_id,
            object_ids={build.image.context_object_id} if build.image.context_object_id else set(),
            image_ids={build.image_id} if build.image_id else set(),
        )
        existing = self.session.get(ImageBuildTable, build.id)
        if existing is not None and existing.cleanup_claimed_at is not None:
            raise ConflictError(f"artifact cleanup is in progress: {build.id}")
        paths = _build_paths(build)
        cache_publish_key = build.cache_metadata.get("cache_publish_key", "")
        resource_clauses: list[ColumnElement[bool]] = []
        if paths:
            path_digests = {archive_path_digest(path) for path in paths}
            resource_clauses.append(
                or_(
                    (ImageBuildTable.archive_path_digest.in_(path_digests))
                    & (ImageBuildTable.archive_path_value.in_(paths)),
                    (ImageBuildTable.manifest_path_digest.in_(path_digests))
                    & (ImageBuildTable.manifest_path_value.in_(paths)),
                    (ImageBuildTable.dockerfile_path_digest.in_(path_digests))
                    & (ImageBuildTable.dockerfile_path_value.in_(paths)),
                    (ImageBuildTable.cache_manifest_path_digest.in_(path_digests))
                    & (ImageBuildTable.cache_manifest_path_value.in_(paths)),
                )
            )
        if cache_publish_key:
            resource_clauses.append(ImageBuildTable.cache_publish_key == cache_publish_key)
        if build.cache_key:
            resource_clauses.append(ImageBuildTable.cache_key == build.cache_key)
        if resource_clauses:
            claimed = self.session.scalars(
                select(ImageBuildTable).where(
                    ImageBuildTable.cleanup_claimed_at.is_not(None),
                    or_(*resource_clauses),
                )
            ).first()
            if claimed is not None:
                raise ConflictError(f"build artifact cleanup is in progress: {claimed.id}")

    def mark_object_claimed(
        self,
        object_id: str,
        *,
        claimed_at: datetime,
        cleanup_kind: str,
    ) -> ObjectRecord:
        row = self.session.get(ObjectTable, object_id)
        if row is None:
            raise KeyError(object_id)
        if row.write_claimed_at is not None:
            raise ConflictError(f"object write is in progress: {row.bucket}/{row.key}")
        if row.cleanup_claimed_at is not None:
            current = ObjectRecord.model_validate(row.payload)
            if current.cleanup_kind != cleanup_kind:
                raise ConflictError(f"object cleanup is in progress: {row.bucket}/{row.key}")
            return current
        payload: dict[str, JsonValue] = {
            **row.payload,
            "cleanup_claimed_at": claimed_at.isoformat(),
            "cleanup_kind": cleanup_kind,
        }
        row.payload = payload
        row.cleanup_claimed_at = claimed_at
        row.cleanup_kind = cleanup_kind
        flag_modified(row, "payload")
        return ObjectRecord.model_validate(payload)

    def mark_image_claimed(
        self,
        image_id: str,
        *,
        workspace_id: str,
        claimed_at: datetime,
    ) -> ImageRecord:
        row = self.session.scalars(
            select(ImageTable).where(
                ImageTable.workspace_id == workspace_id,
                ImageTable.image_id == image_id,
            )
        ).one()
        return _mark_claimed(row, ImageRecord, claimed_at)

    def mark_build_claimed(self, build_id: str, *, claimed_at: datetime) -> ImageBuildRecord:
        row = self.session.get(ImageBuildTable, build_id)
        if row is None:
            raise KeyError(build_id)
        return _mark_claimed(row, ImageBuildRecord, claimed_at)

    def mark_builds_claimed(
        self,
        build_ids: list[str],
        *,
        claimed_at: datetime,
    ) -> list[ImageBuildRecord]:
        if not build_ids:
            return []
        rows = list(
            self.session.scalars(
                select(ImageBuildTable)
                .where(
                    ImageBuildTable.id.in_(build_ids),
                    ImageBuildTable.cleanup_claimed_at.is_(None),
                )
                .order_by(ImageBuildTable.created_at.asc(), ImageBuildTable.id.asc())
            )
        )
        return [_mark_claimed(row, ImageBuildRecord, claimed_at) for row in rows]

    def mark_checkpoint_claimed(
        self,
        checkpoint_id: str,
        *,
        claimed_at: datetime,
    ) -> CheckpointRecord:
        row = self.session.scalars(
            select(CheckpointTable).where(CheckpointTable.checkpoint_id == checkpoint_id)
        ).one()
        return _mark_claimed(row, CheckpointRecord, claimed_at)

    def list_claimed_objects(
        self,
        *,
        limit: int,
        cleanup_kind: str | None = None,
    ) -> list[ObjectRecord]:
        statement = select(ObjectTable).where(ObjectTable.cleanup_claimed_at.is_not(None))
        if cleanup_kind is not None:
            statement = statement.where(ObjectTable.cleanup_kind == cleanup_kind)
        statement = statement.order_by(
            ObjectTable.cleanup_claimed_at.asc(), ObjectTable.id.asc()
        ).limit(limit)
        return [ObjectRecord.model_validate(row.payload) for row in self.session.scalars(statement)]

    def list_claimed_images(self, *, limit: int) -> list[ImageRecord]:
        return [
            ImageRecord.model_validate(row.payload)
            for row in self.session.scalars(
                select(ImageTable)
                .where(ImageTable.cleanup_claimed_at.is_not(None))
                .order_by(ImageTable.cleanup_claimed_at.asc(), ImageTable.id.asc())
                .limit(limit)
            )
        ]

    def list_claimed_builds(self, *, limit: int) -> list[ImageBuildRecord]:
        return [
            ImageBuildRecord.model_validate(row.payload)
            for row in self.session.scalars(
                select(ImageBuildTable)
                .where(ImageBuildTable.cleanup_claimed_at.is_not(None))
                .order_by(ImageBuildTable.cleanup_claimed_at.asc(), ImageBuildTable.id.asc())
                .limit(limit)
            )
        ]

    def list_claimed_checkpoints(self, *, limit: int) -> list[CheckpointRecord]:
        return [
            CheckpointRecord.model_validate(row.payload)
            for row in self.session.scalars(
                select(CheckpointTable)
                .where(CheckpointTable.cleanup_claimed_at.is_not(None))
                .order_by(CheckpointTable.cleanup_claimed_at.asc(), CheckpointTable.id.asc())
                .limit(limit)
            )
        ]


def _mark_claimed[TModel: BaseModel](
    row: ImageTable | ImageBuildTable | CheckpointTable,
    model_type: type[TModel],
    claimed_at: datetime,
) -> TModel:
    payload: dict[str, JsonValue] = {
        **row.payload,
        "cleanup_claimed_at": claimed_at.isoformat(),
    }
    row.payload = payload
    row.cleanup_claimed_at = claimed_at
    flag_modified(row, "payload")
    return model_type.model_validate(payload)


def _stub_object_ids(
    config: StubConfig,
    *,
    metadata: Mapping[str, JsonValue] | None,
) -> set[str]:
    object_ids = {value for value in (config.object_id, config.image.context_object_id) if value}
    for source in (metadata or {}, config.metadata):
        copied = source.get("copied_object_ids")
        if isinstance(copied, list):
            object_ids.update(value for value in copied if isinstance(value, str) and value)
    return object_ids


def _stub_image_ids(config: StubConfig) -> set[str]:
    return {
        value
        for value in (
            config.image.image_id,
            config.runtime.image_id,
        )
        if value
    }


def _build_resource_keys(build: ImageBuildRecord) -> set[str]:
    paths = _build_paths(build)
    cache_key = build.cache_metadata.get("cache_publish_key", "")
    keys = {f"path:{path}" for path in paths}
    if cache_key:
        keys.add(f"cache:{cache_key}")
    if build.cache_key:
        keys.add(f"build-cache-key:{build.cache_key}")
    return keys


def _build_paths(build: ImageBuildRecord) -> set[str]:
    return {
        normalize_runtime_path(raw)
        for raw in (
            build.artifact_path,
            build.manifest_path,
            build.cache_metadata.get("dockerfile_path"),
            build.cache_metadata.get("manifest_path"),
        )
        if raw
    }


__all__ = [
    "OBJECT_CLEANUP_CHECKPOINT",
    "OBJECT_CLEANUP_DELETE",
    "OBJECT_CLEANUP_SOURCE",
    "CleanupRepository",
]
