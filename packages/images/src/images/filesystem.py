from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from database.repositories.images import ImageBuildRepository
from shared.containers import ContainerRecord, ContainerStatus
from shared.errors import ConflictError, NotFoundError, UpstreamUnavailableError
from shared.image_building.authoring import ImageFilesystemSource, ImageSpec

from images.control import stream_build_events
from images.service import ImageBuildService


@dataclass(slots=True)
class FilesystemImageService:
    images: ImageBuildService

    def create(self, container: ContainerRecord) -> str:
        worker_id = container.runtime_worker_id or container.worker_id
        if container.status is not ContainerStatus.Running or not worker_id:
            raise ConflictError("filesystem snapshots require a running Sandbox")
        with self.images.context.database.session() as session:
            source = ImageBuildRepository(session).get_latest_by_image_id(
                container.image, workspace_id=container.workspace_id
            )
        if source is None:
            raise NotFoundError("source image build metadata is unavailable")
        capture_id = uuid4().hex
        build = self.images.build(
            ImageSpec(
                architecture=source.image.architecture,
                python_version=source.image.python_version,
                ignore_python=True,
                dockerfile="FROM scratch\nADD rootfs.tar /\n",
                context_digest=capture_id,
                filesystem_source=ImageFilesystemSource(
                    container_id=container.id, worker_id=worker_id
                ),
            ),
            workspace_id=container.workspace_id,
        )
        for event in stream_build_events(
            self.images, build.id, workspace_id=container.workspace_id
        ):
            if event.response.done:
                if not event.response.success:
                    raise UpstreamUnavailableError(
                        event.response.error
                        or event.response.msg
                        or "filesystem image build failed"
                    )
                return event.response.image_id
        raise UpstreamUnavailableError("filesystem image build ended without a result")
