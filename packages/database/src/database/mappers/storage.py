from __future__ import annotations

from shared.objects import ObjectRecord, ObjectWriteCommand
from shared.timestamps import to_utc, to_utc_or_none, utc_now
from shared.volumes import VolumeRecord

from database.tables.storage import ObjectTable, VolumeTable


def object_from_table(row: ObjectTable) -> ObjectRecord:
    write_target = None
    if row.write_claimed_at is not None:
        write_target = ObjectWriteCommand.model_validate(
            {
                "bucket": row.bucket,
                "key": row.key,
                "path": row.write_target_path,
                "size": row.write_target_size,
                "sha256": row.write_target_sha256,
                "content_type": row.write_target_content_type,
                "metadata": row.write_target_metadata,
                "artifact_task_id": row.write_target_artifact_task_id,
                "artifact_app_id": row.write_target_artifact_app_id,
                "artifact_app_name": row.write_target_artifact_app_name,
                "artifact_filename": row.write_target_artifact_filename,
                "artifact_retention_seconds": row.write_target_artifact_retention_seconds,
                "artifact_stored_at": to_utc_or_none(row.write_target_artifact_stored_at),
                "artifact_expires_at": to_utc_or_none(row.write_target_artifact_expires_at),
                "artifact_metered_at": to_utc_or_none(row.write_target_artifact_metered_at),
                "artifact_deletion_failed": row.write_target_artifact_deletion_failed,
            }
        )
    return ObjectRecord(
        id=str(row.id),
        bucket=row.bucket,
        key=row.key,
        path=row.path,
        size=row.size,
        sha256=row.sha256,
        content_type=row.content_type,
        metadata=dict(row.metadata_json),
        artifact_task_id=row.artifact_task_id,
        artifact_app_id=row.artifact_app_id,
        artifact_app_name=row.artifact_app_name,
        artifact_filename=row.artifact_filename,
        artifact_retention_seconds=row.artifact_retention_seconds,
        artifact_stored_at=to_utc_or_none(row.artifact_stored_at),
        artifact_expires_at=to_utc_or_none(row.artifact_expires_at),
        artifact_metered_at=to_utc_or_none(row.artifact_metered_at),
        artifact_deletion_failed=row.artifact_deletion_failed,
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
        write_claim_id=row.write_claim_id,
        write_claimed_at=to_utc_or_none(row.write_claimed_at),
        write_created=row.write_created,
        write_target=write_target,
        cleanup_kind=row.cleanup_kind,
        cleanup_claimed_at=to_utc_or_none(row.cleanup_claimed_at),
    )


def write_object_row(row: ObjectTable, record: ObjectRecord) -> None:
    row.bucket = record.bucket
    row.key = record.key
    row.path = record.path
    row.size = record.size
    row.sha256 = record.sha256
    row.content_type = record.content_type
    row.metadata_json = dict(record.metadata)
    row.artifact_task_id = record.artifact_task_id
    row.artifact_app_id = record.artifact_app_id
    row.artifact_app_name = record.artifact_app_name
    row.artifact_filename = record.artifact_filename
    row.artifact_retention_seconds = record.artifact_retention_seconds
    row.artifact_stored_at = record.artifact_stored_at
    row.artifact_expires_at = record.artifact_expires_at
    row.artifact_metered_at = record.artifact_metered_at
    row.artifact_deletion_failed = record.artifact_deletion_failed
    row.created_at = record.created_at
    row.updated_at = utc_now()
    row.write_claim_id = record.write_claim_id
    row.write_claimed_at = record.write_claimed_at
    row.write_created = record.write_created
    row.cleanup_kind = record.cleanup_kind
    row.cleanup_claimed_at = record.cleanup_claimed_at
    target = record.write_target
    if target is not None and (target.bucket != record.bucket or target.key != record.key):
        raise ValueError("object write target must retain its claimed location")
    if (target is not None) != (record.write_claimed_at is not None):
        raise ValueError("object write claim requires a target")
    row.write_target_path = target.path if target is not None else None
    row.write_target_size = target.size if target is not None else None
    row.write_target_sha256 = target.sha256 if target is not None else None
    row.write_target_content_type = target.content_type if target is not None else None
    row.write_target_metadata = dict(target.metadata) if target is not None else None
    row.write_target_artifact_task_id = target.artifact_task_id if target is not None else None
    row.write_target_artifact_app_id = target.artifact_app_id if target is not None else None
    row.write_target_artifact_app_name = target.artifact_app_name if target is not None else None
    row.write_target_artifact_filename = target.artifact_filename if target is not None else None
    row.write_target_artifact_retention_seconds = (
        target.artifact_retention_seconds if target is not None else None
    )
    row.write_target_artifact_stored_at = target.artifact_stored_at if target is not None else None
    row.write_target_artifact_expires_at = (
        target.artifact_expires_at if target is not None else None
    )
    row.write_target_artifact_metered_at = (
        target.artifact_metered_at if target is not None else None
    )
    row.write_target_artifact_deletion_failed = (
        target.artifact_deletion_failed if target is not None else None
    )


def volume_from_table(row: VolumeTable) -> VolumeRecord:
    return VolumeRecord(
        id=str(row.id),
        name=row.name,
        created_at=to_utc(row.created_at),
        deletion_requested_at=to_utc_or_none(row.deletion_requested_at),
        size_bytes=row.size_bytes,
    )
