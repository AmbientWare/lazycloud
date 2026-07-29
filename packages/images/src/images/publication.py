from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from database.repositories.images import ImageArchiveRepository
from pydantic import Field
from shared.cache_records import CacheEntry
from shared.contracts import ContractModel
from shared.image_building.records import ImageBuildRecord
from storage.image_archive import ResolvedImageArchiveSettings

from images.context import ImageContext
from images.execution import ImageBuildExecutionResult


class ImageBuildPublicationStatus(StrEnum):
    Skipped = "skipped"
    Published = "published"


class ImageBuildPublicationTargetKind(StrEnum):
    LocalTag = "local-tag"
    RegistryRef = "registry-ref"
    Artifact = "artifact"
    CacheMetadata = "cache-metadata"


class ImageBuildPublicationPublishStatus(StrEnum):
    Skipped = "skipped"
    Published = "published"
    Error = "error"


class ImageBuildRegistryPushStatus(StrEnum):
    Skipped = "skipped"
    Pushed = "pushed"
    Error = "error"


class ImageBuildPublicationCacheStorage(Protocol):
    def put(
        self,
        namespace: str,
        key: str,
        source: str | Path,
        *,
        expires_at: datetime | None = None,
    ) -> CacheEntry: ...


class ImageBuildPublicationPublisher(Protocol):
    def publish(
        self,
        build: ImageBuildRecord,
        publication: ImageBuildPublication,
    ) -> ImageBuildPublicationPublishResult: ...


class ImageBuildRegistryClient(Protocol):
    def push(self, request: ImageBuildRegistryPushRequest) -> ImageBuildRegistryPushResult: ...


@runtime_checkable
class ImageBuildArchiveObjectStore(Protocol):
    def head(
        self,
        key: str,
        *,
        bucket: str | None = None,
    ) -> ImageBuildArchiveObjectInfo: ...


class ImageBuildArchiveObjectInfo(Protocol):
    size: int | None
    metadata: dict[str, str]


class ImageBuildPublicationTarget(ContractModel):
    kind: ImageBuildPublicationTargetKind
    reference: str


class ImageBuildPublication(ContractModel):
    status: ImageBuildPublicationStatus
    workspace_id: str = ""
    published_ref: str = ""
    artifact_path: str = ""
    cache_metadata: dict[str, str] = Field(default_factory=dict)
    targets: list[ImageBuildPublicationTarget] = Field(default_factory=list)
    reason: str = ""

    @property
    def published(self) -> bool:
        return self.status is ImageBuildPublicationStatus.Published


class ImageBuildPublicationPublishResult(ContractModel):
    status: ImageBuildPublicationPublishStatus
    cache_metadata: dict[str, str] = Field(default_factory=dict)
    reason: str = ""

    @property
    def published(self) -> bool:
        return self.status is ImageBuildPublicationPublishStatus.Published


class ImageBuildRegistryPushRequest(ContractModel):
    build_id: str = ""
    image_id: str = ""
    source_ref: str = ""
    target_ref: str = ""
    artifact_path: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)


class ImageBuildRegistryPushResult(ContractModel):
    status: ImageBuildRegistryPushStatus
    source_ref: str = ""
    target_ref: str = ""
    digest: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)
    reason: str = ""

    @property
    def pushed(self) -> bool:
        return self.status is ImageBuildRegistryPushStatus.Pushed


@dataclass(slots=True)
class CompositeImageBuildPublicationPublisher:
    publishers: tuple[ImageBuildPublicationPublisher, ...]

    def publish(
        self,
        build: ImageBuildRecord,
        publication: ImageBuildPublication,
    ) -> ImageBuildPublicationPublishResult:
        metadata: dict[str, str] = {}
        statuses: list[str] = []
        reasons: list[str] = []
        for publisher in self.publishers:
            result = publisher.publish(build, publication)
            statuses.append(result.status.value)
            metadata.update(result.cache_metadata)
            if result.reason:
                reasons.append(result.reason)
        status = (
            ImageBuildPublicationPublishStatus.Error
            if ImageBuildPublicationPublishStatus.Error.value in statuses
            else ImageBuildPublicationPublishStatus.Published
            if ImageBuildPublicationPublishStatus.Published.value in statuses
            else ImageBuildPublicationPublishStatus.Skipped
        )
        metadata["publisher_results"] = ",".join(statuses)
        return ImageBuildPublicationPublishResult(
            status=status,
            cache_metadata=metadata,
            reason="; ".join(reasons),
        )


@dataclass(slots=True)
class ArchiveImageBuildPublicationPublisher:
    """Verifies that the global archive for this image id really holds its bytes.

    The archive row is written when the upload is reserved, so publication no longer
    promotes anything. What it still owes is proof that the object the row describes
    exists at the recorded size and digest before a workspace is authorized for it.
    """

    object_store: ImageBuildArchiveObjectStore
    settings: ResolvedImageArchiveSettings
    context: ImageContext

    def publish(
        self,
        build: ImageBuildRecord,
        publication: ImageBuildPublication,
    ) -> ImageBuildPublicationPublishResult:
        published_key = publication.cache_metadata.get("published_archive_object_key", "")
        published_size = _positive_int(
            publication.cache_metadata.get("published_archive_size_bytes", "")
        )
        published_sha256 = publication.cache_metadata.get("published_archive_sha256", "")
        if not published_key or published_size is None or not _is_sha256(published_sha256):
            return ImageBuildPublicationPublishResult(
                status=ImageBuildPublicationPublishStatus.Error,
                reason="image build published archive identity is incomplete",
            )
        if not build.image_id:
            return ImageBuildPublicationPublishResult(
                status=ImageBuildPublicationPublishStatus.Error,
                reason="image build has no image id for archive publication",
            )
        if not publication.workspace_id:
            return ImageBuildPublicationPublishResult(
                status=ImageBuildPublicationPublishStatus.Error,
                reason="image build publication workspace is unavailable",
            )
        try:
            with self.context.database.session() as session:
                archive = ImageArchiveRepository(session).get(build.image_id)
            if archive is None:
                return ImageBuildPublicationPublishResult(
                    status=ImageBuildPublicationPublishStatus.Error,
                    reason="image build archive was not reserved",
                )
            if (
                archive.bucket != self.settings.bucket
                or archive.object_key != published_key
                or archive.size_bytes != published_size
                or archive.sha256 != published_sha256
            ):
                return ImageBuildPublicationPublishResult(
                    status=ImageBuildPublicationPublishStatus.Error,
                    reason="image build archive record does not match publication",
                )
            head = self.object_store.head(
                self.settings.physical_key(archive.object_key),
                bucket=self.settings.bucket,
            )
        except Exception as exc:
            return ImageBuildPublicationPublishResult(
                status=ImageBuildPublicationPublishStatus.Error,
                reason=f"image archive verification failed: {type(exc).__name__}",
            )
        if head.size != published_size or head.metadata.get("artifact-sha256") != published_sha256:
            return ImageBuildPublicationPublishResult(
                status=ImageBuildPublicationPublishStatus.Error,
                reason="image build archive failed size or sha256 verification",
            )
        return ImageBuildPublicationPublishResult(
            status=ImageBuildPublicationPublishStatus.Published,
            cache_metadata={
                "image_archive_key": published_key,
                "image_archive_size_bytes": str(published_size),
                "image_archive_sha256": published_sha256,
                "image_archive_status": "ready",
            },
            reason="image build archive verified",
        )


@dataclass(slots=True)
class CacheImageBuildPublicationPublisher:
    cache_storage: ImageBuildPublicationCacheStorage
    namespace: str = "image-builds"

    def publish(
        self,
        build: ImageBuildRecord,
        publication: ImageBuildPublication,
    ) -> ImageBuildPublicationPublishResult:
        if not publication.published:
            return ImageBuildPublicationPublishResult(
                status=ImageBuildPublicationPublishStatus.Skipped,
                reason="image build publication was skipped",
            )
        source = _publish_source_path(publication)
        if source is None:
            return ImageBuildPublicationPublishResult(
                status=ImageBuildPublicationPublishStatus.Skipped,
                reason="image build publication has no local artifact path",
            )
        key = build.image_id or build.id
        try:
            entry = self.cache_storage.put(self.namespace, key, source)
        except OSError as exc:
            return ImageBuildPublicationPublishResult(
                status=ImageBuildPublicationPublishStatus.Error,
                reason=str(exc),
            )
        return ImageBuildPublicationPublishResult(
            status=ImageBuildPublicationPublishStatus.Published,
            cache_metadata={
                "cache_publish_status": ImageBuildPublicationPublishStatus.Published.value,
                "cache_publish_namespace": self.namespace,
                "cache_publish_key": entry.key,
                "cache_publish_path": entry.path,
                "cache_publish_sha256": entry.sha256,
                "cache_publish_size": str(entry.size),
            },
            reason="image build artifact published to cache",
        )


@dataclass(slots=True)
class RegistryImageBuildPublicationPublisher:
    registry_client: ImageBuildRegistryClient
    target_ref: str = ""

    def publish(
        self,
        build: ImageBuildRecord,
        publication: ImageBuildPublication,
    ) -> ImageBuildPublicationPublishResult:
        if not publication.published:
            return ImageBuildPublicationPublishResult(
                status=ImageBuildPublicationPublishStatus.Skipped,
                reason="image build publication was skipped",
            )
        source_ref = publication.published_ref or build.published_ref or build.tag or ""
        if not source_ref:
            return ImageBuildPublicationPublishResult(
                status=ImageBuildPublicationPublishStatus.Skipped,
                reason="image build publication has no source reference",
            )
        target_ref = self.target_ref or source_ref
        push = self.registry_client.push(
            ImageBuildRegistryPushRequest(
                build_id=build.id,
                image_id=build.image_id or "",
                source_ref=source_ref,
                target_ref=target_ref,
                artifact_path=publication.artifact_path or build.artifact_path or "",
                metadata=publication.cache_metadata,
            )
        )
        if push.status is ImageBuildRegistryPushStatus.Error:
            return ImageBuildPublicationPublishResult(
                status=ImageBuildPublicationPublishStatus.Error,
                cache_metadata={
                    "registry_push_status": push.status.value,
                    "registry_push_source": push.source_ref or source_ref,
                    "registry_push_target": push.target_ref or target_ref,
                },
                reason=push.reason or "image registry push failed",
            )
        if push.status is ImageBuildRegistryPushStatus.Skipped:
            return ImageBuildPublicationPublishResult(
                status=ImageBuildPublicationPublishStatus.Skipped,
                cache_metadata={
                    "registry_push_status": push.status.value,
                    "registry_push_source": push.source_ref or source_ref,
                    "registry_push_target": push.target_ref or target_ref,
                },
                reason=push.reason or "image registry push skipped",
            )
        return ImageBuildPublicationPublishResult(
            status=ImageBuildPublicationPublishStatus.Published,
            cache_metadata={
                "registry_push_status": push.status.value,
                "registry_push_source": push.source_ref or source_ref,
                "registry_push_target": push.target_ref or target_ref,
                "registry_push_digest": push.digest,
                **push.metadata,
            },
            reason=push.reason or "image registry push complete",
        )


def image_build_publication_from_execution(
    build: ImageBuildRecord,
    result: ImageBuildExecutionResult,
) -> ImageBuildPublication:
    if not result.complete:
        return ImageBuildPublication(
            status=ImageBuildPublicationStatus.Skipped,
            reason="image build execution did not complete",
        )

    published_ref = result.published_ref or build.published_ref or build.tag or build.image_id or ""
    artifact_path = result.artifact_path or build.artifact_path or build.manifest_path or ""
    cache_metadata = {**build.cache_metadata, **result.cache_metadata}
    targets = _publication_targets(published_ref, artifact_path, cache_metadata)
    return ImageBuildPublication(
        status=ImageBuildPublicationStatus.Published,
        published_ref=published_ref,
        artifact_path=artifact_path,
        cache_metadata={
            **cache_metadata,
            "targets": ",".join(target.kind.value for target in targets),
        },
        targets=targets,
        reason="image build publication metadata recorded",
    )


def _publication_targets(
    published_ref: str,
    artifact_path: str,
    cache_metadata: dict[str, str],
) -> list[ImageBuildPublicationTarget]:
    targets: list[ImageBuildPublicationTarget] = []
    if published_ref:
        kind = (
            ImageBuildPublicationTargetKind.LocalTag
            if published_ref.startswith("local:")
            else ImageBuildPublicationTargetKind.RegistryRef
        )
        targets.append(ImageBuildPublicationTarget(kind=kind, reference=published_ref))
    if artifact_path:
        targets.append(
            ImageBuildPublicationTarget(
                kind=ImageBuildPublicationTargetKind.Artifact,
                reference=artifact_path,
            )
        )
    if cache_metadata:
        cache_reference = cache_metadata.get("cache_key") or cache_metadata.get("tag") or "metadata"
        targets.append(
            ImageBuildPublicationTarget(
                kind=ImageBuildPublicationTargetKind.CacheMetadata,
                reference=cache_reference,
            )
        )
    return targets


def _publish_source_path(publication: ImageBuildPublication) -> Path | None:
    candidates = [
        publication.artifact_path,
        publication.cache_metadata.get("manifest_path", ""),
        publication.cache_metadata.get("dockerfile_path", ""),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_file():
            return path
    return None


def _positive_int(value: str) -> int | None:
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
