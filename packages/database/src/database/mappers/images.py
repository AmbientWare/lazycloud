from __future__ import annotations

from shared.checkpoints import CheckpointRecord, CheckpointStatus
from shared.image_building.authoring import ImageSpec
from shared.image_building.records import (
    BuildStatus,
    ImageArchiveRecord,
    ImageBuildPhase,
    ImageBuildRecord,
    ImageRecord,
)
from shared.runtime_paths import archive_path_digest, normalize_runtime_path
from shared.timestamps import to_utc, to_utc_or_none

from database.tables.images import (
    CheckpointTable,
    ImageArchiveTable,
    ImageBuildTable,
    ImageTable,
)


def image_from_table(row: ImageTable) -> ImageRecord:
    return ImageRecord(
        id=str(row.id),
        workspace_id=str(row.workspace_id),
        image_id=row.image_id,
        clip_version=row.clip_version,
        aliases=list(row.aliases),
        cleanup_claimed_at=to_utc_or_none(row.cleanup_claimed_at),
        cleanup_completed_at=to_utc_or_none(row.cleanup_completed_at),
    )


def image_archive_from_table(row: ImageArchiveTable) -> ImageArchiveRecord:
    return ImageArchiveRecord(
        id=str(row.id),
        image_id=row.image_id,
        bucket=row.bucket,
        object_key=row.object_key,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        registry_ref=row.registry_ref,
        manifest_digest=row.manifest_digest,
        architecture=row.architecture,
        format_version=row.format_version,
        cleanup_claimed_at=to_utc_or_none(row.cleanup_claimed_at),
    )


def image_build_from_table(row: ImageBuildTable) -> ImageBuildRecord:
    image_definition = dict(row.image_definition)
    image_definition["context_object_id"] = row.context_object_id
    cache_metadata = dict(row.cache_details)
    for key, value in (
        ("dockerfile_path", row.dockerfile_path_value or None),
        ("manifest_path", row.cache_manifest_path_value or None),
        ("cache_publish_key", row.cache_publish_key or None),
        ("image_archive_status", row.image_archive_status),
    ):
        if value is not None:
            cache_metadata[key] = value
    if row.build_container_required is not None:
        cache_metadata["build_container_required"] = (
            "true" if row.build_container_required else "false"
        )
    if row.image_archive_format_version is not None:
        cache_metadata["image_archive_format_version"] = str(row.image_archive_format_version)
    return ImageBuildRecord(
        id=str(row.id),
        image=ImageSpec.model_validate(image_definition),
        fingerprint=row.fingerprint,
        image_id=row.image_id,
        cache_key=row.cache_key,
        dockerfile=row.dockerfile,
        context_digest=row.context_digest,
        status=BuildStatus(row.status),
        phase=ImageBuildPhase(row.phase),
        tag=row.tag,
        manifest_path=row.manifest_path_value or None,
        published_ref=row.published_ref,
        artifact_path=row.archive_path_value or None,
        cache_metadata=cache_metadata,
        logs=list(row.diagnostic_lines),
        error=row.error,
        created_at=to_utc(row.created_at),
        started_at=to_utc_or_none(row.started_at),
        finished_at=to_utc_or_none(row.finished_at),
        cleanup_claimed_at=to_utc_or_none(row.cleanup_claimed_at),
    )


def write_image_build_row(row: ImageBuildTable, build: ImageBuildRecord) -> None:
    row.image_definition = build.image.model_dump(mode="json", exclude={"context_object_id"})
    row.context_object_id = build.image.context_object_id
    row.fingerprint = build.fingerprint
    row.image_id = build.image_id
    row.cache_key = build.cache_key
    row.dockerfile = build.dockerfile
    row.context_digest = build.context_digest
    row.status = build.status.value
    row.phase = build.phase.value
    row.tag = build.tag
    row.published_ref = build.published_ref
    row.diagnostic_lines = list(build.logs)
    row.error = build.error
    row.created_at = build.created_at
    row.started_at = build.started_at
    row.finished_at = build.finished_at
    row.cleanup_claimed_at = build.cleanup_claimed_at
    row.archive_path_value = normalize_runtime_path(build.artifact_path)
    row.archive_path_digest = archive_path_digest(row.archive_path_value)
    row.manifest_path_value = normalize_runtime_path(build.manifest_path)
    row.manifest_path_digest = archive_path_digest(row.manifest_path_value)
    row.cache_details = dict(build.cache_metadata)
    row.dockerfile_path_value = row.cache_details.pop("dockerfile_path", "")
    row.dockerfile_path_digest = archive_path_digest(row.dockerfile_path_value)
    row.cache_manifest_path_value = row.cache_details.pop("manifest_path", "")
    row.cache_manifest_path_digest = archive_path_digest(row.cache_manifest_path_value)
    row.cache_publish_key = row.cache_details.pop("cache_publish_key", "")
    required = row.cache_details.pop("build_container_required", None)
    if required not in {None, "true", "false"}:
        raise ValueError("build_container_required must be true or false")
    row.build_container_required = None if required is None else required == "true"
    version = row.cache_details.pop("image_archive_format_version", None)
    row.image_archive_format_version = int(version) if version is not None else None
    row.image_archive_status = row.cache_details.pop("image_archive_status", None)


def checkpoint_from_table(row: CheckpointTable) -> CheckpointRecord:
    return CheckpointRecord(
        checkpoint_id=row.checkpoint_id,
        source_container_id=row.source_container_id or "",
        container_ip=row.container_ip,
        status=CheckpointStatus(row.status),
        remote_key=row.remote_key,
        workspace_id=row.workspace_id or "",
        stub_id=row.stub_id or "",
        stub_type=row.stub_type,
        app_id=row.app_id or "",
        exposed_ports=list(row.exposed_ports),
        cache_hash=row.cache_hash,
        cache_size_bytes=row.cache_size_bytes,
        origin_key=row.origin_key,
        locality=row.locality,
        accelerator=row.accelerator,
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
        last_restored_at=to_utc_or_none(row.last_restored_at),
        retention_expires_at=to_utc_or_none(row.retention_expires_at),
        cleanup_claimed_at=to_utc_or_none(row.cleanup_claimed_at),
        deleted_at=to_utc_or_none(row.deleted_at),
    )


def write_checkpoint_row(row: CheckpointTable, checkpoint: CheckpointRecord) -> None:
    row.source_container_id = checkpoint.source_container_id or None
    row.container_ip = checkpoint.container_ip
    row.status = checkpoint.status.value
    row.remote_key = checkpoint.remote_key
    row.workspace_id = checkpoint.workspace_id or None
    row.stub_id = checkpoint.stub_id or None
    row.stub_type = checkpoint.stub_type
    row.app_id = checkpoint.app_id or None
    row.exposed_ports = list(checkpoint.exposed_ports)
    row.cache_hash = checkpoint.cache_hash
    row.cache_size_bytes = checkpoint.cache_size_bytes
    row.origin_key = checkpoint.origin_key
    row.locality = checkpoint.locality
    row.accelerator = checkpoint.accelerator
    row.created_at = checkpoint.created_at
    row.updated_at = checkpoint.updated_at
    row.last_restored_at = checkpoint.last_restored_at
    row.retention_expires_at = checkpoint.retention_expires_at
    row.cleanup_claimed_at = checkpoint.cleanup_claimed_at
    row.deleted_at = checkpoint.deleted_at
