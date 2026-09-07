from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import uuid4

from database.repositories.image_build_dispatch import ImageBuildDispatchRepository
from database.repositories.image_build_logs import ImageBuildLogRepository
from database.repositories.images import (
    ImageArchiveRepository,
    ImageBuildRepository,
    ImageRepository,
)
from observability.events import EventService
from pydantic import JsonValue, TypeAdapter
from shared.errors import ConflictError, NotFoundError, UpstreamUnavailableError
from shared.events import EventLevel
from shared.image_building.authoring import ImageSpec
from shared.image_building.records import (
    BuildStatus,
    ImageArchiveRecord,
    ImageBuildRecord,
    ImageRecord,
)
from shared.timestamps import utc_now
from storage.image_archive import ImageArchiveSettings

from images.building import (
    ImageBuildCredentialPlan,
    ImageBuildStreamEventPlan,
    image_build_stream_event_key,
    plan_image_build_complete_event,
    plan_image_build_failure_event,
    plan_image_build_log_event,
)
from images.cleanup import (
    ImageBuildCleanupExecutor,
    ImageBuildCleanupResult,
    LocalImageBuildCleanupExecutor,
    plan_image_build_cleanup,
)
from images.context import ImageContext
from images.execution import (
    ImageBuildExecutionResult,
)
from images.metadata import (
    CURRENT_IMAGE_CLIP_VERSION,
    image_metadata_aliases_for_build,
    merge_image_metadata_aliases,
)
from images.publication import (
    ImageBuildArchiveObjectStore,
    ImageBuildPublicationPublisher,
    ImageBuildPublicationPublishStatus,
    image_build_publication_from_execution,
)
from images.submission import ImageBuildSubmissionService

LOGGER = logging.getLogger(__name__)

IMAGE_BUILD_REUSE_CANDIDATE_LIMIT = 16
MAX_IMAGE_BUILD_DIAGNOSTIC_LINES = 256
MAX_IMAGE_BUILD_DIAGNOSTIC_LINE_BYTES = 8 * 1024
MAX_IMAGE_BUILD_DIAGNOSTIC_BYTES = 64 * 1024
_JSON_VALUE_ADAPTER = TypeAdapter[JsonValue](JsonValue)


@dataclass(frozen=True, slots=True)
class ImageArchiveReservation:
    """The archive this build must use, and whether it still has to write it."""

    archive: ImageArchiveRecord
    upload_required: bool


def _reusable_build(record: ImageBuildRecord) -> bool:
    return (
        record.status is BuildStatus.Complete
        and record.cache_metadata.get("build_container_required") == "true"
        and record.cache_metadata.get("image_archive_format_version") == "2"
    )


def _active_build_status(status: BuildStatus) -> bool:
    return status in {BuildStatus.Pending, BuildStatus.Running}


@dataclass(slots=True)
class ImageBuildService:
    context: ImageContext
    submission: ImageBuildSubmissionService
    events: EventService | None = None
    publication_publisher: ImageBuildPublicationPublisher | None = None
    cleanup_executor: ImageBuildCleanupExecutor | None = None
    archive_settings: ImageArchiveSettings | None = None
    archive_store: ImageBuildArchiveObjectStore | None = None

    def build(
        self,
        image: ImageSpec,
        *,
        workspace_id: str | None = None,
        tag: str | None = None,
        credential_plan: ImageBuildCredentialPlan | None = None,
        registry_credential_payload: str | None = None,
        build_args: dict[str, str] | None = None,
        request_id: str | None = None,
    ) -> ImageBuildRecord:
        return self.submission.submit(
            image,
            workspace_id=self._resolve_workspace_id(workspace_id),
            tag=tag,
            credential_plan=credential_plan,
            registry_credential_payload=registry_credential_payload,
            build_args=build_args,
            request_id=request_id,
        )

    def _persist_execution_result(
        self,
        build_id: str,
        result: ImageBuildExecutionResult,
        *,
        workspace_id: str,
        sensitive_values: tuple[str, ...],
    ) -> list[ImageBuildStreamEventPlan]:
        emitted: list[ImageBuildStreamEventPlan] = []
        existing_events = self.stream_events(build_id, workspace_id=workspace_id)
        seen = {image_build_stream_event_key(event) for event in existing_events}
        seen_messages = {event.message for event in existing_events if event.message}
        for event in result.events:
            if event.done:
                continue
            if image_build_stream_event_key(event) in seen or (
                event.message and not event.done and event.message in seen_messages
            ):
                continue
            emitted.append(
                self.append_stream_event(
                    build_id,
                    event,
                    workspace_id=workspace_id,
                    sensitive_values=sensitive_values,
                )
            )
            seen.add(image_build_stream_event_key(event))
            if event.message:
                seen_messages.add(event.message)

        if result.status is BuildStatus.Complete:
            completed = self._publish_and_complete_execution(
                build_id,
                result,
                workspace_id=workspace_id,
                sensitive_values=sensitive_values,
            )
            emitted.append(self._stream_event_from_record(completed))
            return emitted
        record = self.get(build_id, workspace_id=workspace_id)
        if _is_terminal_build_status(record.status):
            return emitted
        status = result.status if result.status in _TERMINAL_BUILD_STATUSES else BuildStatus.Failed
        error = _image_build_failure_diagnostic(
            record,
            result.reason or "image build failed",
            sensitive_values=sensitive_values,
        )
        event = plan_image_build_failure_event(
            error,
            image_id=record.image_id or "",
            build_id=record.id,
            python_version=record.image.python_version,
            status=status,
        )
        failed = self._persist_stream_event(
            record,
            event,
            workspace_id=workspace_id,
            sensitive_values=sensitive_values,
        )
        emitted.append(self._stream_event_from_record(failed))
        return emitted

    def _publish_and_complete_execution(
        self,
        build_id: str,
        result: ImageBuildExecutionResult,
        *,
        workspace_id: str,
        sensitive_values: tuple[str, ...],
    ) -> ImageBuildRecord:
        claim_id = uuid4().hex
        initial = self.get(build_id, workspace_id=workspace_id)
        with self.context.database.session() as session:
            repository = ImageBuildRepository(session)
            repository.lock_fingerprint(initial.fingerprint, workspace_id=workspace_id)
            if not repository.claim_publication(
                build_id,
                workspace_id=workspace_id,
                claim_id=claim_id,
            ):
                raise RuntimeError("image build publication ownership is no longer active")

        try:
            with self.context.database.session() as session:
                repository = ImageBuildRepository(session)
                record = repository.get_claimed_publication(
                    build_id,
                    workspace_id=workspace_id,
                    claim_id=claim_id,
                )
                if record is None:
                    raise RuntimeError(
                        "image build publication ownership was lost before promotion"
                    )
            publication = image_build_publication_from_execution(record, result).model_copy(
                update={"workspace_id": workspace_id}
            )
            if not publication.published:
                raise RuntimeError(publication.reason or "image build publication was skipped")

            publish_result = None
            if self.publication_publisher is not None:
                publish_result = self.publication_publisher.publish(record, publication)
                publication.cache_metadata = {
                    **publication.cache_metadata,
                    **publish_result.cache_metadata,
                    "cache_publish_result": publish_result.status.value,
                }
                if publish_result.reason:
                    publication.cache_metadata["cache_publish_reason"] = publish_result.reason
                if publish_result.status is ImageBuildPublicationPublishStatus.Error:
                    raise RuntimeError(publish_result.reason or "image build publication failed")

            archive_published = (
                publish_result is not None
                and publish_result.status is ImageBuildPublicationPublishStatus.Published
                and publish_result.cache_metadata.get("image_archive_status") == "ready"
            )
            if not archive_published:
                reason = publish_result.reason if publish_result is not None else ""
                raise RuntimeError(reason or "required image archive publication was not completed")

            record.published_ref = publication.published_ref
            record.artifact_path = publication.artifact_path
            record.cache_metadata = publication.cache_metadata
            completion_event = plan_image_build_complete_event(
                image_id=record.image_id or "",
                build_id=record.id,
                python_version=record.image.python_version,
            )
            if completion_event.message:
                _append_image_build_diagnostic(
                    record,
                    completion_event.message,
                    sensitive_values=sensitive_values,
                )
            record.status = completion_event.status
            record.phase = completion_event.phase
            record.finished_at = utc_now()

            with self.context.database.session() as session:
                repository = ImageBuildRepository(session)
                repository.lock_fingerprint(initial.fingerprint, workspace_id=workspace_id)
                completed = repository.finalize_publication(
                    record,
                    workspace_id=workspace_id,
                    claim_id=claim_id,
                    clip_version=CURRENT_IMAGE_CLIP_VERSION,
                    archive_published=archive_published,
                )

            self._emit(
                completion_event.kind.value,
                completed,
                workspace_id=workspace_id,
                data={
                    "phase": completion_event.phase.value,
                    "status": completion_event.status.value,
                },
            )
            return completed
        finally:
            with self.context.database.session() as session:
                ImageBuildRepository(session).release_publication(
                    build_id, workspace_id=workspace_id, claim_id=claim_id
                )

    def fail(
        self,
        build_id: str,
        error: str,
        *,
        workspace_id: str | None = None,
        sensitive_values: tuple[str, ...] = (),
    ) -> ImageBuildRecord:
        resolved_workspace_id = self._resolve_workspace_id(workspace_id)
        record = self.get(build_id, workspace_id=resolved_workspace_id)
        if _is_terminal_build_status(record.status):
            return record
        event = plan_image_build_failure_event(
            error,
            image_id=record.image_id or "",
            build_id=record.id,
            python_version=record.image.python_version,
        )
        return self._persist_stream_event(
            record,
            event,
            workspace_id=resolved_workspace_id,
            sensitive_values=sensitive_values,
        )

    def record_worker_execution_result(
        self,
        build_id: str,
        *,
        workspace_id: str,
        image_id: str,
        container_id: str,
        status: BuildStatus,
        object_key: str = "",
        archive_size_bytes: int = 0,
        archive_sha256: str = "",
        logs: list[str] | None = None,
        error_message: str = "",
    ) -> ImageBuildRecord:
        record = self.get(build_id, workspace_id=workspace_id)
        if record.image_id != image_id:
            raise ConflictError("reported image build result does not match the build image")
        if _is_terminal_build_status(record.status):
            return record
        if status not in _TERMINAL_BUILD_STATUSES:
            raise ConflictError("reported image build result is not terminal")

        events = [
            plan_image_build_log_event(
                line,
                image_id=image_id,
                build_id=build_id,
                python_version=record.image.python_version,
            )
            for line in logs or []
            if line.strip()
        ]
        result = ImageBuildExecutionResult(
            status=status,
            events=events,
            published_ref=(record.tag or image_id) if status is BuildStatus.Complete else "",
            artifact_path=(record.manifest_path or "") if status is BuildStatus.Complete else "",
            cache_metadata=(
                {
                    "executor": "container-client",
                    "scheduler_submit_status": "queued",
                    "container_id": container_id,
                    "clip_version": "2",
                    "published_archive_object_key": object_key,
                    "published_archive_size_bytes": str(archive_size_bytes),
                    "published_archive_sha256": archive_sha256,
                }
                if status is BuildStatus.Complete
                else {}
            ),
            reason=error_message,
        )
        self._persist_execution_result(
            build_id,
            result,
            workspace_id=workspace_id,
            sensitive_values=(),
        )
        completed = self.get(build_id, workspace_id=workspace_id)
        if completed.status is BuildStatus.Complete:
            self.persist_image_metadata(
                build_id,
                workspace_id=workspace_id,
                clip_version=2,
            )
            completed = self.get(build_id, workspace_id=workspace_id)
        return completed

    def cancel(
        self,
        build_id: str,
        *,
        workspace_id: str | None = None,
        reason: str = "Build was aborted.",
    ) -> ImageBuildRecord:
        return self.submission.cancel(
            build_id, workspace_id=self._resolve_workspace_id(workspace_id), reason=reason
        )

    def cancel_for_workspace(
        self,
        build_id: str,
        *,
        workspace_id: str,
        reason: str = "Build was aborted.",
    ) -> ImageBuildRecord:
        return self.submission.cancel(build_id, workspace_id=workspace_id, reason=reason)

    def persist_image_metadata(
        self,
        build_id: str,
        *,
        workspace_id: str | None = None,
        clip_version: int = CURRENT_IMAGE_CLIP_VERSION,
    ) -> ImageRecord:
        resolved_workspace_id = self._resolve_workspace_id(workspace_id)
        record = self.get(build_id, workspace_id=resolved_workspace_id)
        if not record.image_id:
            msg = f"image build has no image id: {build_id}"
            raise ValueError(msg)
        with self.context.database.session() as session:
            repository = ImageRepository(session)
            existing = repository.get(
                record.image_id,
                workspace_id=resolved_workspace_id,
            )
            metadata = ImageRecord(
                id=existing.id if existing is not None else "",
                workspace_id=resolved_workspace_id,
                image_id=record.image_id,
                clip_version=clip_version,
                aliases=merge_image_metadata_aliases(
                    existing.aliases if existing is not None else [],
                    image_metadata_aliases_for_build(record),
                ),
            )
            saved = repository.upsert(metadata)
        self._emit(
            "metadata",
            record,
            workspace_id=resolved_workspace_id,
            data={"clip_version": clip_version},
        )
        return saved

    def get_image_metadata(
        self,
        image_id: str,
        *,
        workspace_id: str,
    ) -> ImageRecord | None:
        with self.context.database.session() as session:
            return ImageRepository(session).get(
                image_id,
                workspace_id=workspace_id,
            )

    def reserve_image_archive(
        self,
        image_id: str,
        *,
        object_key: str,
        size_bytes: int,
        sha256: str,
        registry_ref: str,
        manifest_digest: str,
        architecture: str,
        format_version: int,
    ) -> ImageArchiveReservation:
        """Claim the one archive for this image id, or defer to the one already there.

        Three outcomes, and only the first two let a build write bytes:
        nothing there, so we reserve it; something there whose bytes check out, so
        this build skips the upload entirely; or something there whose bytes are
        gone or wrong, which we may replace only by compare-and-set on the digest we
        just read. The CAS is what keeps row and bytes moving together — the
        recorded digest changes in the same statement that claims the right to
        replace the object, so a valid archive is never silently overwritten.
        """

        settings = self.archive_settings
        if settings is None or self.archive_store is None:
            raise UpstreamUnavailableError("image archive storage is not configured")
        with self.context.database.session() as session:
            archive, reserved = ImageArchiveRepository(session).reserve(
                image_id,
                bucket=settings.bucket,
                object_key=object_key,
                size_bytes=size_bytes,
                sha256=sha256,
                registry_ref=registry_ref,
                manifest_digest=manifest_digest,
                architecture=architecture,
                format_version=format_version,
            )
        if reserved:
            return ImageArchiveReservation(archive=archive, upload_required=True)
        if archive.cleanup_claimed_at is not None:
            # Retention has claimed these bytes and may already be deleting them.
            # Adopting the row would let this build skip an upload for content that
            # is about to disappear, so fail retryably and let cleanup finish.
            raise ConflictError("image archive is being reclaimed")
        if archive.format_version >= 2 and self._archive_bytes_present(archive):
            return ImageArchiveReservation(archive=archive, upload_required=False)
        with self.context.database.session() as session:
            taken = ImageArchiveRepository(session).take_over(
                image_id,
                expected_sha256=archive.sha256,
                bucket=settings.bucket,
                object_key=object_key,
                size_bytes=size_bytes,
                sha256=sha256,
                registry_ref=registry_ref,
                manifest_digest=manifest_digest,
                architecture=architecture,
                format_version=format_version,
            )
        if taken is None:
            raise ConflictError("image archive was claimed by another build")
        return ImageArchiveReservation(archive=taken, upload_required=True)

    def _archive_bytes_present(self, archive: ImageArchiveRecord) -> bool:
        settings = self.archive_settings
        if settings is None or self.archive_store is None:
            return False
        key = settings.physical_key(archive.object_key)
        # An unreachable store must not read as "absent": the caller takes the
        # archive row away from its owner on a False, and a build that cannot
        # see the bytes has not established that they are gone.
        if not self.archive_store.exists(key, bucket=settings.bucket):
            return False
        head = self.archive_store.head(key, bucket=settings.bucket)
        return (
            head.size == archive.size_bytes
            and head.metadata.get("artifact-sha256") == archive.sha256
        )

    def get_authorized_image_archive(
        self,
        image_id: str,
        *,
        workspace_id: str,
    ) -> ImageArchiveRecord | None:
        """The global archive, only for a workspace that holds an authorization row.

        Stays workspace-scoped on purpose. One archive row now serves every tenant
        and its key carries no tenant component, so this join is the entire download
        boundary; resolving globally here would hand any workspace any image.
        """

        with self.context.database.session() as session:
            return ImageArchiveRepository(session).get_authorized(
                image_id,
                workspace_id=workspace_id,
            )

    def cleanup_build(
        self,
        build_id: str,
        *,
        workspace_id: str | None = None,
        keep_artifacts: bool = True,
        container_id: str = "",
        executor: ImageBuildCleanupExecutor | None = None,
    ) -> ImageBuildCleanupResult:
        resolved_workspace_id = self._resolve_workspace_id(workspace_id)
        build = self.get(build_id, workspace_id=resolved_workspace_id)
        cleanup_executor = executor or self.cleanup_executor
        if cleanup_executor is None:
            cleanup_executor = LocalImageBuildCleanupExecutor()
        plan = plan_image_build_cleanup(
            build,
            keep_artifacts=keep_artifacts,
            container_id=container_id,
        )
        result = cleanup_executor.cleanup(plan)
        build.cache_metadata = {
            **build.cache_metadata,
            "cleanup_status": result.status.value,
            "cleanup_actions": ",".join(action.value for action in result.actions),
        }
        if result.reason:
            build.cache_metadata["cleanup_reason"] = result.reason
        with self.context.database.session() as session:
            ImageBuildRepository(session).upsert(build, workspace_id=resolved_workspace_id)
        return result

    def append_stream_event(
        self,
        build_id: str,
        event: ImageBuildStreamEventPlan,
        *,
        workspace_id: str | None = None,
        sensitive_values: tuple[str, ...] = (),
    ) -> ImageBuildStreamEventPlan:
        resolved_workspace_id = self._resolve_workspace_id(workspace_id)
        record = self.get(build_id, workspace_id=resolved_workspace_id)
        sanitized = _sanitize_image_build_stream_event(
            event,
            sensitive_values=sensitive_values,
        )
        self._persist_stream_event(
            record,
            sanitized,
            workspace_id=resolved_workspace_id,
            sensitive_values=sensitive_values,
        )
        return sanitized

    def record_worker_progress(
        self, build_id: str, *, workspace_id: str, after: int, messages: list[str]
    ) -> int:
        with self.context.database.session() as session:
            logs = ImageBuildLogRepository(session)
            sequence = logs.append(
                build_id, workspace_id=workspace_id, after=after, messages=messages
            )
            repository = ImageBuildRepository(session)
            record = repository.get(build_id, workspace_id=workspace_id)
            if record is not None and _active_build_status(record.status):
                record.status = BuildStatus.Running
                record.started_at = record.started_at or utc_now()
                repository.upsert(record, workspace_id=workspace_id)
            return sequence

    def stream_events(
        self,
        build_id: str,
        *,
        workspace_id: str | None = None,
        after: int = 0,
    ) -> list[ImageBuildStreamEventPlan]:
        resolved_workspace_id = self._resolve_workspace_id(workspace_id)
        record = self.get(build_id, workspace_id=resolved_workspace_id)
        with self.context.database.session() as session:
            repository = ImageBuildLogRepository(session)
            messages = repository.page(build_id, after=after)
            last = repository.last_sequence(build_id)
        events = [
            plan_image_build_log_event(
                message,
                image_id=record.image_id or "",
                build_id=record.id,
                python_version=record.image.python_version,
            ).model_copy(update={"sequence": sequence})
            for sequence, message in messages
        ]
        if _is_terminal_build_status(record.status) and (not messages or messages[-1][0] == last):
            events.append(
                self._stream_event_from_record(record).model_copy(update={"sequence": last + 1})
            )
        return events

    def get(self, build_id: str, *, workspace_id: str | None = None) -> ImageBuildRecord:
        with self.context.database.session() as session:
            repository = ImageBuildRepository(session)
            record = (
                repository.get(build_id, workspace_id=workspace_id)
                if workspace_id is not None
                else repository.get_across_workspaces(build_id)
            )
        if record is None:
            msg = f"image build not found: {build_id}"
            raise NotFoundError(msg)
        return record

    def _resolve_workspace_id(self, workspace_id: str | None) -> str:
        if workspace_id:
            return workspace_id
        with self.context.database.session() as session:
            return self.context.default_workspace_id(session)

    def get_for_workspace(self, build_id: str, *, workspace_id: str) -> ImageBuildRecord:
        return self.get(build_id, workspace_id=workspace_id)

    def find_by_request_id(self, request_id: str, *, workspace_id: str) -> ImageBuildRecord | None:
        with self.context.database.session() as session:
            build_id = ImageBuildDispatchRepository(session).request_build_id(
                request_id, workspace_id=workspace_id
            )
            return (
                ImageBuildRepository(session).get(build_id, workspace_id=workspace_id)
                if build_id
                else None
            )

    def find_by_fingerprint(
        self,
        fingerprint: str,
        *,
        workspace_id: str | None = None,
    ) -> ImageBuildRecord | None:
        resolved_workspace_id = self._resolve_workspace_id(workspace_id)
        with self.context.database.session() as session:
            return ImageBuildRepository(session).get_latest_by_fingerprint(
                fingerprint,
                workspace_id=resolved_workspace_id,
            )

    def find_reusable_by_fingerprint(
        self,
        fingerprint: str,
        *,
        workspace_id: str | None = None,
    ) -> ImageBuildRecord | None:
        resolved_workspace_id = self._resolve_workspace_id(workspace_id)
        return next(
            (
                record
                for record in self._list_completed_by_fingerprint(
                    fingerprint,
                    workspace_id=resolved_workspace_id,
                )
                if _reusable_build(record)
            ),
            None,
        )

    def find_active_by_fingerprint(
        self,
        fingerprint: str,
        *,
        workspace_id: str | None = None,
    ) -> ImageBuildRecord | None:
        resolved_workspace_id = self._resolve_workspace_id(workspace_id)
        with self.context.database.session() as session:
            return ImageBuildRepository(session).get_active_by_fingerprint(
                fingerprint,
                workspace_id=resolved_workspace_id,
            )

    def find_by_image_id(
        self,
        image_id: str,
        *,
        workspace_id: str | None = None,
    ) -> ImageBuildRecord | None:
        resolved_workspace_id = self._resolve_workspace_id(workspace_id)
        with self.context.database.session() as session:
            return ImageBuildRepository(session).get_latest_by_image_id(
                image_id,
                workspace_id=resolved_workspace_id,
            )

    def find_reusable_by_image_id(
        self,
        image_id: str,
        *,
        workspace_id: str | None = None,
    ) -> ImageBuildRecord | None:
        resolved_workspace_id = self._resolve_workspace_id(workspace_id)
        return next(
            (
                record
                for record in self._list_completed_by_image_id(
                    image_id,
                    workspace_id=resolved_workspace_id,
                )
                if _reusable_build(record)
            ),
            None,
        )

    def _list_completed_by_fingerprint(
        self,
        fingerprint: str,
        *,
        workspace_id: str,
    ) -> list[ImageBuildRecord]:
        with self.context.database.session() as session:
            return ImageBuildRepository(session).list_completed_by_fingerprint(
                fingerprint,
                workspace_id=workspace_id,
                limit=IMAGE_BUILD_REUSE_CANDIDATE_LIMIT,
            )

    def _list_completed_by_image_id(
        self,
        image_id: str,
        *,
        workspace_id: str,
    ) -> list[ImageBuildRecord]:
        with self.context.database.session() as session:
            return ImageBuildRepository(session).list_completed_by_image_id(
                image_id,
                workspace_id=workspace_id,
                limit=IMAGE_BUILD_REUSE_CANDIDATE_LIMIT,
            )

    def list(self, *, workspace_id: str | None = None) -> list[ImageBuildRecord]:
        with self.context.database.session() as session:
            repository = ImageBuildRepository(session)
            records = (
                repository.list(workspace_id=workspace_id)
                if workspace_id is not None
                else repository.list_across_workspaces()
            )
        records.sort(key=lambda item: item.created_at, reverse=True)
        return records

    def list_for_workspace(self, *, workspace_id: str) -> list[ImageBuildRecord]:
        return self.list(workspace_id=workspace_id)

    def _persist_stream_event(
        self,
        record: ImageBuildRecord,
        event: ImageBuildStreamEventPlan,
        *,
        workspace_id: str,
        sensitive_values: tuple[str, ...] = (),
        level: EventLevel | None = None,
        data: dict[str, JsonValue] | None = None,
    ) -> ImageBuildRecord:
        with self.context.database.session() as session:
            repository = ImageBuildRepository(session)
            current = repository.lock_build(record.id, workspace_id=workspace_id)
            if _is_terminal_build_status(current.status):
                return current
            if event.message:
                _append_image_build_diagnostic(
                    current, event.message, sensitive_values=sensitive_values
                )
            current.status = event.status
            current.phase = event.phase
            if event.error:
                current.error = _sanitize_image_build_diagnostic(
                    event.error, sensitive_values=sensitive_values
                )
            if event.done:
                current.finished_at = utc_now()
            saved = repository.upsert(current, workspace_id=workspace_id)
        self._emit(
            event.kind.value,
            saved,
            workspace_id=workspace_id,
            level=level or (EventLevel.Error if event.error else EventLevel.Info),
            data={
                "phase": event.phase.value,
                "status": event.status.value,
                **(data or {}),
            },
        )
        return saved

    def _stream_event_from_record(
        self,
        record: ImageBuildRecord,
    ) -> ImageBuildStreamEventPlan:
        if record.status is BuildStatus.Complete:
            return plan_image_build_complete_event(
                image_id=record.image_id or "",
                build_id=record.id,
                python_version=record.image.python_version,
                phase=record.phase,
            )
        return plan_image_build_failure_event(
            record.error or "image build failed",
            image_id=record.image_id or "",
            build_id=record.id,
            python_version=record.image.python_version,
            status=record.status,
            phase=record.phase,
        )

    def _emit(
        self,
        action: str,
        record: ImageBuildRecord,
        *,
        workspace_id: str | None = None,
        level: EventLevel = EventLevel.Info,
        data: dict[str, JsonValue] | None = None,
    ) -> None:
        if self.events is None:
            return
        event_data: dict[str, JsonValue] = {
            "image_id": record.image_id,
            "cache_key": record.cache_key,
        }
        if data is not None:
            event_data.update(data)
        self.events.emit(
            f"image.build.{action}",
            resource_type="image_build",
            resource_id=record.id,
            message=f"image build {record.id} {action}",
            level=level,
            data=event_data,
            workspace_id=workspace_id,
        )


def _sanitize_image_build_diagnostic(
    value: str,
    *,
    sensitive_values: tuple[str, ...],
) -> str:
    sanitized = value
    for secret in sensitive_values:
        if secret:
            sanitized = sanitized.replace(secret, "<redacted>")
    encoded = sanitized.encode("utf-8")
    if len(encoded) <= MAX_IMAGE_BUILD_DIAGNOSTIC_LINE_BYTES:
        return sanitized
    suffix = "... [truncated]"
    budget = MAX_IMAGE_BUILD_DIAGNOSTIC_LINE_BYTES - len(suffix.encode("utf-8"))
    return encoded[:budget].decode("utf-8", errors="ignore") + suffix


def _sanitize_image_build_stream_event(
    event: ImageBuildStreamEventPlan,
    *,
    sensitive_values: tuple[str, ...],
) -> ImageBuildStreamEventPlan:
    return event.model_copy(
        update={
            "message": _sanitize_image_build_diagnostic(
                event.message,
                sensitive_values=sensitive_values,
            ),
            "error": _sanitize_image_build_diagnostic(
                event.error,
                sensitive_values=sensitive_values,
            ),
        }
    )


def _append_image_build_diagnostic(
    record: ImageBuildRecord,
    value: str,
    *,
    sensitive_values: tuple[str, ...],
) -> None:
    message = _sanitize_image_build_diagnostic(
        value.rstrip("\n"),
        sensitive_values=sensitive_values,
    )
    if not message or (record.logs and record.logs[-1] == message):
        return
    record.logs.append(message)
    while len(record.logs) > MAX_IMAGE_BUILD_DIAGNOSTIC_LINES:
        record.logs.pop(0)
    while (
        sum(len(item.encode("utf-8")) for item in record.logs) > MAX_IMAGE_BUILD_DIAGNOSTIC_BYTES
        and len(record.logs) > 1
    ):
        record.logs.pop(0)


def _image_build_failure_diagnostic(
    record: ImageBuildRecord,
    reason: str,
    *,
    sensitive_values: tuple[str, ...],
) -> str:
    sanitized_reason = _sanitize_image_build_diagnostic(
        reason,
        sensitive_values=sensitive_values,
    )
    generic = {
        "image build failed",
        "image build container execution failed",
        "v2 build container exited with failure",
        "v1 build container exited before image creation",
    }
    if sanitized_reason.lower() not in generic:
        return sanitized_reason.rstrip("\n")
    for message in reversed(record.logs):
        lowered = message.lower()
        if any(marker in lowered for marker in ("error", "exception", "failed", "exit code")):
            return message.rstrip("\n")
    return sanitized_reason.rstrip("\n")


def _is_terminal_build_status(status: BuildStatus) -> bool:
    return status in _TERMINAL_BUILD_STATUSES


_TERMINAL_BUILD_STATUSES = {
    BuildStatus.Complete,
    BuildStatus.Failed,
    BuildStatus.Cancelled,
    BuildStatus.Timeout,
}
