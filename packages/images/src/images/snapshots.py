from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from shared.app_identity import SOURCE_PACKAGE_BUCKET
from shared.env import parse_environment
from shared.errors import NotFoundError
from shared.image_building.authoring import (
    FilesystemSnapshotMetadata,
    FilesystemSnapshotSource,
    ImageSpec,
)
from shared.objects import ObjectRecord
from storage.service import ObjectStorage

from images.control import ImageBuildWorkflow, stream_build_events


class FilesystemSnapshotExporter(Protocol):
    def archive(self, container_id: str, target: Path) -> FilesystemSnapshotMetadata: ...


def validate_snapshot_source(record: ObjectRecord, source: FilesystemSnapshotSource) -> None:
    if (
        record.id != source.object_id
        or record.sha256 != source.sha256
        or record.size != source.size_bytes
        or record.bucket != SOURCE_PACKAGE_BUCKET
        or record.key != f"sources/image-snapshots/{source.ownership_id}/snapshot.tar"
        or record.metadata.get("filesystem_snapshot_owner") != source.ownership_id
    ):
        raise ValueError("filesystem snapshot source ownership or content does not match")


def delete_snapshot_source(
    objects: ObjectStorage, source: FilesystemSnapshotSource, *, workspace_id: str
) -> None:
    try:
        record = objects.get_by_id_for_workspace(source.object_id, workspace_id=workspace_id)
    except NotFoundError:
        return
    validate_snapshot_source(record, source)
    objects.delete_required_for_workspace(
        workspace_id=workspace_id, bucket=record.bucket, key=record.key
    )


@dataclass(slots=True)
class ImageFilesystemSnapshotService:
    images: ImageBuildWorkflow
    objects: ObjectStorage

    def create(
        self, container_id: str, *, workspace_id: str, exporter: FilesystemSnapshotExporter
    ) -> str:
        ownership_id = uuid4().hex
        with tempfile.TemporaryDirectory(prefix="lazycloud-filesystem-snapshot-") as directory:
            archive = Path(directory) / "snapshot.tar"
            metadata = exporter.archive(container_id, archive)
            record = self.objects.put_file_for_workspace(
                workspace_id=workspace_id,
                bucket=SOURCE_PACKAGE_BUCKET,
                key=f"sources/image-snapshots/{ownership_id}/snapshot.tar",
                source=archive,
                content_type="application/x-tar",
                metadata={"filesystem_snapshot_owner": ownership_id},
                overwrite=False,
            )
        source = FilesystemSnapshotSource(
            object_id=record.id,
            ownership_id=ownership_id,
            sha256=record.sha256,
            size_bytes=record.size,
        )
        try:
            build = self.images.build(
                ImageSpec(
                    base="scratch",
                    python_version="",
                    ignore_python=True,
                    architecture=metadata.architecture,
                    env=parse_environment(metadata.env),
                    workdir=metadata.workdir,
                    filesystem_snapshot=source,
                ),
                workspace_id=workspace_id,
                request_id=ownership_id,
            )
        except BaseException:
            prior = self.images.find_by_request_id(ownership_id, workspace_id=workspace_id)
            if prior is None or prior.image.filesystem_snapshot != source:
                delete_snapshot_source(self.objects, source, workspace_id=workspace_id)
            raise
        if build.image.filesystem_snapshot != source:
            delete_snapshot_source(self.objects, source, workspace_id=workspace_id)
        for event in stream_build_events(self.images, build.id, workspace_id=workspace_id):
            if event.response.done:
                if event.response.success and event.response.image_id:
                    return event.response.image_id
                raise RuntimeError(event.response.error or "filesystem snapshot image build failed")
        raise RuntimeError("filesystem snapshot image build ended before publication")
