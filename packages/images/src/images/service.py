from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from threading import Event, Lock, Thread
from uuid import uuid4

from database.repositories.images import (
    ImageArchiveRepository,
    ImageBuildRepository,
    ImageRepository,
)
from observability.events import EventService
from pydantic import BaseModel, JsonValue, TypeAdapter
from shared.errors import ConflictError, NotFoundError, UpstreamUnavailableError
from shared.events import EventLevel
from shared.image_building.authoring import ImageSpec
from shared.image_building.planning import ImageBuildPlan
from shared.image_building.records import (
    BuildStatus,
    ImageArchiveRecord,
    ImageBuildPhase,
    ImageBuildRecord,
    ImageRecord,
)
from shared.timestamps import utc_now
from storage.image_archive import ResolvedImageArchiveSettings

from images.building import (
    IMAGE_BUILD_CONTAINER_TTL_SECONDS,
    ImageBuildCredentialPlan,
    ImageBuildSessionPlan,
    ImageBuildStreamEventPlan,
    build_image_plan,
    image_build_stream_event_key,
    plan_image_build_cancellation,
    plan_image_build_complete_event,
    plan_image_build_failure_event,
    plan_image_build_log_event,
    plan_image_build_reused_stream,
    plan_image_build_session,
)
from images.cleanup import (
    ImageBuildCleanupExecutor,
    ImageBuildCleanupResult,
    LocalImageBuildCleanupExecutor,
    plan_image_build_cleanup,
)
from images.context import ImageContext
from images.execution import (
    ImageBuildExecutionRequest,
    ImageBuildExecutionResult,
    ImageBuildExecutor,
    ImageBuildExecutorKind,
    ManifestImageBuildExecutor,
)
from images.lifecycle import (
    ImageBuildContainerLifecycleService,
    ImageBuildContainerLifecycleStatus,
    image_build_lifecycle_session_metadata,
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

LOGGER = logging.getLogger(__name__)

DEFAULT_IMAGE_BUILD_DUPLICATE_WAIT_TIMEOUT_SECONDS = 900.0
DEFAULT_IMAGE_BUILD_DUPLICATE_WAIT_POLL_SECONDS = 0.5
DEFAULT_IMAGE_BUILD_CLAIM_LEASE_SECONDS = 120.0
DEFAULT_IMAGE_BUILD_CLAIM_HEARTBEAT_SECONDS = 30.0
DEFAULT_IMAGE_BUILD_PUBLICATION_CLAIM_LEASE_SECONDS = 900.0
DEFAULT_IMAGE_BUILD_SHUTDOWN_TIMEOUT_SECONDS = 30.0
IMAGE_BUILD_SERVICE_CLOSED_REASON = "Image build service is shutting down."
IMAGE_BUILD_REUSE_CANDIDATE_LIMIT = 16
MAX_IMAGE_BUILD_DIAGNOSTIC_LINES = 256
MAX_IMAGE_BUILD_DIAGNOSTIC_LINE_BYTES = 8 * 1024
MAX_IMAGE_BUILD_DIAGNOSTIC_BYTES = 64 * 1024
_JSON_VALUE_ADAPTER = TypeAdapter[JsonValue](JsonValue)

type ImageBuildSleep = Callable[[float], None]


@dataclass(slots=True)
class ImageBuildExecution:
    record: ImageBuildRecord
    session: ImageBuildSessionPlan
    events: list[ImageBuildStreamEventPlan]
    credential_plan: ImageBuildCredentialPlan | None = None


@dataclass(frozen=True, slots=True)
class ImageBuildClaim:
    record: ImageBuildRecord
    owned: bool = False


@dataclass(frozen=True, slots=True)
class ImageArchiveReservation:
    """The archive this build must use, and whether it still has to write it."""

    archive: ImageArchiveRecord
    upload_required: bool


@dataclass(slots=True)
class _ImageBuildClaimHeartbeat:
    stop_event: Event
    thread: Thread

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join()


@dataclass(slots=True, eq=False)
class ImageBuildExecutionController:
    """One service-owned background image build and its terminal state."""

    service: ImageBuildService
    image: ImageSpec
    workspace_id: str | None
    tag: str | None
    credential_plan: ImageBuildCredentialPlan | None
    registry_credential_payload: str
    build_args: dict[str, str]
    thread_name: str
    _state_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _stop_requested: Event = field(default_factory=Event, init=False, repr=False)
    _thread: Thread = field(init=False, repr=False)
    _execution: ImageBuildExecution | None = field(default=None, init=False, repr=False)
    _error: Exception | None = field(default=None, init=False, repr=False)
    _owned_build_id: str = field(default="", init=False, repr=False)
    _cancel_error: Exception | None = field(default=None, init=False, repr=False)
    _cancel_started: bool = field(default=False, init=False, repr=False)
    _cancel_reason: str = field(
        default=IMAGE_BUILD_SERVICE_CLOSED_REASON,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        self._thread = Thread(target=self._run, name=self.thread_name)

    @property
    def execution(self) -> ImageBuildExecution | None:
        with self._state_lock:
            return self._execution

    @property
    def error(self) -> Exception | None:
        with self._state_lock:
            return self._error

    @property
    def cancel_error(self) -> Exception | None:
        with self._state_lock:
            return self._cancel_error

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout=timeout)

    def request_cancel(self, reason: str) -> None:
        with self._state_lock:
            self._cancel_reason = reason
        self._stop_requested.set()
        self._cancel_owned_build(reason)

    def _run(self) -> None:
        try:
            if self._stop_requested.is_set():
                return
            execution = self.service.execute(
                self.image,
                workspace_id=self.workspace_id,
                tag=self.tag,
                credential_plan=self.credential_plan,
                registry_credential_payload=self.registry_credential_payload,
                build_args=self.build_args,
                on_build_started=self._track_owned_build,
            )
            with self._state_lock:
                self._execution = execution
        except Exception as exc:
            with self._state_lock:
                self._error = exc
        finally:
            self.service._complete_background_execution(self)

    def _track_owned_build(self, record: ImageBuildRecord) -> None:
        with self._state_lock:
            self._owned_build_id = record.id
            cancel_reason = self._cancel_reason
        if self._stop_requested.is_set():
            self._cancel_owned_build(cancel_reason)

    def _cancel_owned_build(self, reason: str) -> None:
        with self._state_lock:
            build_id = self._owned_build_id
            if not build_id or self._cancel_started:
                return
            self._cancel_started = True
        try:
            self.service.cancel(
                build_id,
                workspace_id=self.workspace_id,
                container_connected=True,
                reason=reason,
            )
        except Exception as exc:
            with self._state_lock:
                self._cancel_error = exc
                self._cancel_started = False


class ImageBuildShutdownIncompleteError(RuntimeError):
    """Raised when database-using image work did not quiesce before timeout."""


def _build_record_matches_executor(
    record: ImageBuildRecord,
    executor: ImageBuildExecutor,
) -> bool:
    if record.status is not BuildStatus.Complete:
        return False

    expected_markers = _executor_cache_markers(executor)
    if not expected_markers:
        return False

    actual_marker = record.cache_metadata.get("executor")
    if actual_marker not in expected_markers:
        return False

    if actual_marker == ImageBuildExecutorKind.Manifest.value:
        return bool(record.manifest_path or record.artifact_path)
    if actual_marker == ImageBuildExecutorKind.LocalDocker.value:
        return bool(record.published_ref)
    if actual_marker in {
        ImageBuildExecutorKind.BuildContainer.value,
        "container-client",
    }:
        return (
            bool(record.cache_metadata.get("scheduler_submit_status"))
            and record.cache_metadata.get("build_container_required") == "true"
        )
    return False


def _executor_cache_markers(executor: ImageBuildExecutor) -> set[str]:
    return set(executor.cache_markers)


def _active_build_status(status: BuildStatus) -> bool:
    return status in {BuildStatus.Pending, BuildStatus.Running}


@dataclass(slots=True)
class ImageBuildService:
    context: ImageContext
    events: EventService | None = None
    executor: ImageBuildExecutor = field(default_factory=ManifestImageBuildExecutor)
    publication_publisher: ImageBuildPublicationPublisher | None = None
    cleanup_executor: ImageBuildCleanupExecutor | None = None
    container_lifecycle: ImageBuildContainerLifecycleService | None = None
    archive_settings: ResolvedImageArchiveSettings | None = None
    archive_store: ImageBuildArchiveObjectStore | None = None
    duplicate_wait_timeout_seconds: float = DEFAULT_IMAGE_BUILD_DUPLICATE_WAIT_TIMEOUT_SECONDS
    duplicate_wait_poll_seconds: float = DEFAULT_IMAGE_BUILD_DUPLICATE_WAIT_POLL_SECONDS
    claim_lease_seconds: float = DEFAULT_IMAGE_BUILD_CLAIM_LEASE_SECONDS
    claim_heartbeat_seconds: float = DEFAULT_IMAGE_BUILD_CLAIM_HEARTBEAT_SECONDS
    publication_claim_lease_seconds: float = DEFAULT_IMAGE_BUILD_PUBLICATION_CLAIM_LEASE_SECONDS
    shutdown_timeout_seconds: float = DEFAULT_IMAGE_BUILD_SHUTDOWN_TIMEOUT_SECONDS
    sleep: ImageBuildSleep = time.sleep
    _execution_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _background_executions: set[ImageBuildExecutionController] = field(
        default_factory=set,
        init=False,
        repr=False,
    )
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def closed(self) -> bool:
        with self._execution_lock:
            return self._closed

    @property
    def active_background_execution_count(self) -> int:
        with self._execution_lock:
            return len(self._background_executions)

    def start_background_execution(
        self,
        image: ImageSpec,
        *,
        workspace_id: str | None = None,
        tag: str | None = None,
        credential_plan: ImageBuildCredentialPlan | None = None,
        registry_credential_payload: str | None = None,
        build_args: dict[str, str] | None = None,
    ) -> ImageBuildExecutionController:
        plan = build_image_plan(image)
        controller = ImageBuildExecutionController(
            service=self,
            image=image,
            workspace_id=workspace_id,
            tag=tag,
            credential_plan=credential_plan,
            registry_credential_payload=registry_credential_payload or "",
            build_args=dict(build_args or {}),
            thread_name=f"image-build-{plan.cache_key}",
        )
        with self._execution_lock:
            if self._closed:
                raise RuntimeError("image build service is closed")
            self._background_executions.add(controller)
            try:
                controller.start()
            except BaseException:
                self._background_executions.remove(controller)
                raise
        return controller

    def close(self) -> None:
        with self._execution_lock:
            self._closed = True
            executions = tuple(self._background_executions)
        if not executions:
            return

        failures: list[Exception] = []
        for execution in executions:
            execution.request_cancel(IMAGE_BUILD_SERVICE_CLOSED_REASON)

        deadline = time.monotonic() + max(self.shutdown_timeout_seconds, 0.0)
        for execution in executions:
            execution.join(timeout=max(deadline - time.monotonic(), 0.0))
        live = [execution for execution in executions if execution.is_alive]
        if live:
            failures.append(
                ImageBuildShutdownIncompleteError(
                    f"{len(live)} image build execution(s) remained active after shutdown"
                )
            )
        failures.extend(
            error for execution in executions if (error := execution.cancel_error) is not None
        )
        if failures:
            raise ExceptionGroup("image build shutdown was incomplete", failures)

    def _complete_background_execution(
        self,
        execution: ImageBuildExecutionController,
    ) -> None:
        with self._execution_lock:
            self._background_executions.discard(execution)

    def _ensure_open(self) -> None:
        with self._execution_lock:
            if self._closed:
                raise RuntimeError("image build service is closed")

    def build(
        self,
        image: ImageSpec,
        *,
        workspace_id: str | None = None,
        tag: str | None = None,
        credential_plan: ImageBuildCredentialPlan | None = None,
        registry_credential_payload: str | None = None,
        build_args: dict[str, str] | None = None,
    ) -> ImageBuildRecord:
        execution = self.start_background_execution(
            image,
            workspace_id=workspace_id,
            tag=tag,
            credential_plan=credential_plan,
            registry_credential_payload=registry_credential_payload,
            build_args=build_args,
        )
        execution.join()
        if execution.error is not None:
            raise execution.error
        result = execution.execution
        if result is None:
            raise RuntimeError("image build execution finished without a result")
        return result.record

    def start(
        self,
        image: ImageSpec,
        *,
        workspace_id: str | None = None,
        tag: str | None = None,
        credential_plan: ImageBuildCredentialPlan | None = None,
    ) -> ImageBuildExecution:
        self._ensure_open()
        resolved_workspace_id = self._resolve_workspace_id(workspace_id)
        plan = build_image_plan(image)
        claim = self._claim_build(plan, workspace_id=resolved_workspace_id, tag=tag)
        while not claim.owned:
            if _build_record_matches_executor(claim.record, self.executor):
                return self._reused_execution(
                    plan,
                    claim.record,
                    credential_plan=credential_plan,
                )
            record = self._wait_for_build_to_finish(
                claim.record,
                workspace_id=resolved_workspace_id,
            )
            if _build_record_matches_executor(record, self.executor):
                return self._reused_execution(plan, record, credential_plan=credential_plan)
            if _active_build_status(record.status):
                claim = self._claim_build(plan, workspace_id=resolved_workspace_id, tag=tag)
                if claim.owned:
                    break
                if _build_record_matches_executor(claim.record, self.executor):
                    return self._reused_execution(
                        plan,
                        claim.record,
                        credential_plan=credential_plan,
                    )
                raise TimeoutError(f"timed out waiting for equivalent image build {record.id}")
            claim = self._claim_build(plan, workspace_id=resolved_workspace_id, tag=tag)
        record = claim.record
        session = plan_image_build_session(
            plan.spec,
            require_archive_publication=self.executor.requires_archive_publication,
            image_id=plan.image_id,
            build_id=record.id,
        )
        record = self._record_lifecycle_session(
            record,
            session,
            workspace_id=resolved_workspace_id,
        )
        emitted: list[ImageBuildStreamEventPlan] = []
        for event in session.stream_events:
            if event.done:
                continue
            emitted.append(
                self.append_stream_event(
                    record.id,
                    event,
                    workspace_id=resolved_workspace_id,
                )
            )
        return ImageBuildExecution(
            record=self.get(record.id, workspace_id=resolved_workspace_id),
            session=session,
            events=emitted,
            credential_plan=credential_plan,
        )

    def execute(
        self,
        image: ImageSpec,
        *,
        workspace_id: str | None = None,
        tag: str | None = None,
        credential_plan: ImageBuildCredentialPlan | None = None,
        registry_credential_payload: str | None = None,
        build_args: dict[str, str] | None = None,
        on_build_started: Callable[[ImageBuildRecord], None] | None = None,
    ) -> ImageBuildExecution:
        self._ensure_open()
        resolved_workspace_id = self._resolve_workspace_id(workspace_id)
        plan = build_image_plan(image)
        claim = self._claim_build(plan, workspace_id=resolved_workspace_id, tag=tag)
        while not claim.owned:
            if _build_record_matches_executor(claim.record, self.executor):
                return self._reused_execution(
                    plan,
                    claim.record,
                    credential_plan=credential_plan,
                )
            completed = self._wait_for_build_to_finish(
                claim.record,
                workspace_id=resolved_workspace_id,
            )
            if _build_record_matches_executor(completed, self.executor):
                return self._reused_execution(plan, completed, credential_plan=credential_plan)
            if _active_build_status(completed.status):
                claim = self._claim_build(plan, workspace_id=resolved_workspace_id, tag=tag)
                if claim.owned:
                    break
                if _build_record_matches_executor(claim.record, self.executor):
                    return self._reused_execution(
                        plan,
                        claim.record,
                        credential_plan=credential_plan,
                    )
                raise TimeoutError(f"timed out waiting for equivalent image build {completed.id}")
            claim = self._claim_build(plan, workspace_id=resolved_workspace_id, tag=tag)

        record = claim.record
        if on_build_started is not None:
            on_build_started(record)
        session = plan_image_build_session(
            plan.spec,
            require_archive_publication=self.executor.requires_archive_publication,
            image_id=plan.image_id,
            build_id=record.id,
        )
        emitted: list[ImageBuildStreamEventPlan] = []
        sensitive_values = _image_build_sensitive_values(
            plan,
            registry_credential_payload=registry_credential_payload or "",
            build_args=build_args or {},
        )
        heartbeat = self._start_claim_heartbeat(record.id, workspace_id=resolved_workspace_id)
        try:
            record = self._record_lifecycle_session(
                record,
                session,
                workspace_id=resolved_workspace_id,
            )
            for event in session.stream_events:
                if event.done:
                    continue
                emitted.append(
                    self.append_stream_event(
                        record.id,
                        event,
                        workspace_id=resolved_workspace_id,
                        sensitive_values=sensitive_values,
                    )
                )
            cache_event = plan_image_build_log_event(
                f"cache key: {plan.cache_key}",
                image_id=plan.image_id,
                build_id=record.id,
                python_version=plan.spec.python_version,
            )
            emitted.append(
                self.append_stream_event(
                    record.id,
                    cache_event,
                    workspace_id=resolved_workspace_id,
                    sensitive_values=sensitive_values,
                )
            )
            self._write_build_files(
                record,
                plan,
                session=session,
                credential_plan=credential_plan,
                workspace_id=resolved_workspace_id,
            )
            manifest_event = plan_image_build_log_event(
                "manifest written",
                image_id=plan.image_id,
                build_id=record.id,
                python_version=plan.spec.python_version,
            )
            emitted.append(
                self.append_stream_event(
                    record.id,
                    manifest_event,
                    workspace_id=resolved_workspace_id,
                    sensitive_values=sensitive_values,
                )
            )
            execution_result = self.executor.execute(
                self._execution_request(
                    record,
                    plan,
                    session=session,
                    credential_plan=credential_plan,
                    registry_credential_payload=registry_credential_payload or "",
                    build_args=build_args or {},
                    workspace_id=resolved_workspace_id,
                    sensitive_values=sensitive_values,
                )
            )
            self._require_active_claim(record.id, workspace_id=resolved_workspace_id)
            emitted.extend(
                self._persist_execution_result(
                    record.id,
                    execution_result,
                    workspace_id=resolved_workspace_id,
                    sensitive_values=sensitive_values,
                )
            )
            completed = self.get(record.id, workspace_id=resolved_workspace_id)
            if completed.status is BuildStatus.Complete:
                self.persist_image_metadata(
                    record.id,
                    workspace_id=resolved_workspace_id,
                    clip_version=session.clip_version,
                )
        except Exception as exc:
            failed = self.fail(
                record.id,
                str(exc),
                workspace_id=resolved_workspace_id,
                sensitive_values=sensitive_values,
            )
            emitted.append(self._stream_event_from_record(failed))
            return ImageBuildExecution(
                record=failed,
                session=session,
                events=emitted,
                credential_plan=credential_plan,
            )
        finally:
            heartbeat.stop()
        completed = self.get(record.id, workspace_id=resolved_workspace_id)
        return ImageBuildExecution(
            record=completed,
            session=session,
            events=emitted,
            credential_plan=credential_plan,
        )

    def _reused_execution(
        self,
        plan: ImageBuildPlan,
        record: ImageBuildRecord,
        *,
        credential_plan: ImageBuildCredentialPlan | None,
    ) -> ImageBuildExecution:
        reused = record.model_copy(update={"phase": ImageBuildPhase.Reused})
        session = plan_image_build_session(
            reused.image,
            require_archive_publication=self.executor.requires_archive_publication,
            image_id=reused.image_id or plan.image_id,
            build_id=reused.id,
        )
        return ImageBuildExecution(
            record=reused,
            session=session,
            events=[
                *plan_image_build_reused_stream(
                    image_id=reused.image_id or plan.image_id,
                    build_id=reused.id,
                    python_version=reused.image.python_version,
                )
            ],
            credential_plan=credential_plan,
        )

    def _wait_for_build_to_finish(
        self,
        record: ImageBuildRecord,
        *,
        workspace_id: str,
    ) -> ImageBuildRecord:
        if not _active_build_status(record.status):
            return record
        timeout_seconds = max(self.duplicate_wait_timeout_seconds, 0.0)
        deadline = time.monotonic() + timeout_seconds
        current = record
        while _active_build_status(current.status) and time.monotonic() < deadline:
            self.sleep(max(self.duplicate_wait_poll_seconds, 0.0))
            current = self.get(record.id, workspace_id=workspace_id)
        return current

    def _claim_build(
        self,
        plan: ImageBuildPlan,
        *,
        workspace_id: str,
        tag: str | None,
    ) -> ImageBuildClaim:
        stale: list[ImageBuildRecord] = []
        with self.context.database.session() as session:
            repository = ImageBuildRepository(session)
            repository.lock_fingerprint(plan.cache_key, workspace_id=workspace_id)
            completed = repository.list_completed_by_fingerprint(
                plan.cache_key,
                workspace_id=workspace_id,
                limit=IMAGE_BUILD_REUSE_CANDIDATE_LIMIT,
            )
            reusable = next(
                (
                    record
                    for record in completed
                    if _build_record_matches_executor(record, self.executor)
                ),
                None,
            )
            if reusable is not None:
                return ImageBuildClaim(record=reusable)
            stale = repository.fail_stale_active(
                plan.cache_key,
                workspace_id=workspace_id,
                stale_before=utc_now() - timedelta(seconds=max(self.claim_lease_seconds, 0.0)),
                publication_stale_before=utc_now()
                - timedelta(seconds=max(self.publication_claim_lease_seconds, 0.01)),
            )
            active = repository.get_active_by_fingerprint(
                plan.cache_key,
                workspace_id=workspace_id,
            )
            if active is not None:
                return ImageBuildClaim(record=active)
            saved = self._create_build_record(
                repository,
                plan,
                workspace_id=workspace_id,
                tag=tag,
            )
        for record in stale:
            self._cancel_stale_build_container(record, workspace_id=workspace_id)
            self._emit(
                "lease.expired",
                record,
                workspace_id=workspace_id,
                level=EventLevel.Error,
            )
        self._emit("started", saved, workspace_id=workspace_id)
        return ImageBuildClaim(record=saved, owned=True)

    def _cancel_stale_build_container(
        self,
        record: ImageBuildRecord,
        *,
        workspace_id: str,
    ) -> None:
        if self.container_lifecycle is None:
            return
        result = self.container_lifecycle.cancel_container(
            record.id,
            context_cancelled=True,
            build_succeeded=False,
            container_connected=True,
            stopping_ttl_seconds=IMAGE_BUILD_CONTAINER_TTL_SECONDS,
        )
        if result.cache_metadata:
            self._merge_cache_metadata(
                record.id,
                result.cache_metadata,
                workspace_id=workspace_id,
            )
        if result.status is ImageBuildContainerLifecycleStatus.Error:
            self._emit(
                "lease.cancel.error",
                self.get(record.id, workspace_id=workspace_id),
                workspace_id=workspace_id,
                level=EventLevel.Error,
                data={"reason": result.reason},
            )

    def _start_claim_heartbeat(
        self,
        build_id: str,
        *,
        workspace_id: str,
    ) -> _ImageBuildClaimHeartbeat:
        stop_event = Event()
        interval = max(self.claim_heartbeat_seconds, 0.01)

        def heartbeat() -> None:
            while not stop_event.wait(interval):
                try:
                    with self.context.database.session() as session:
                        active = ImageBuildRepository(session).heartbeat_active(
                            build_id,
                            workspace_id=workspace_id,
                        )
                except Exception:
                    LOGGER.debug("build claim heartbeat failed", exc_info=True)
                    continue
                if not active:
                    return

        thread = Thread(
            target=heartbeat,
            name=f"image-build-claim-{build_id}",
            daemon=True,
        )
        thread.start()
        return _ImageBuildClaimHeartbeat(stop_event=stop_event, thread=thread)

    def _require_active_claim(self, build_id: str, *, workspace_id: str) -> None:
        record = self.get(build_id, workspace_id=workspace_id)
        if _active_build_status(record.status):
            return
        reason = record.error or record.status.value
        raise RuntimeError(f"image build ownership was lost before publication: {reason}")

    def _create_build_record(
        self,
        repository: ImageBuildRepository,
        plan: ImageBuildPlan,
        *,
        workspace_id: str,
        tag: str | None,
    ) -> ImageBuildRecord:
        fingerprint = plan.cache_key
        record = repository.records.create(
            {
                "image": plan.spec.model_dump(mode="json"),
                "fingerprint": fingerprint,
                "image_id": plan.image_id,
                "cache_key": plan.cache_key,
                "dockerfile": plan.dockerfile,
                "context_digest": plan.context_digest,
                "status": BuildStatus.Running.value,
                "phase": ImageBuildPhase.Submitted.value,
                "tag": tag or f"local:{fingerprint[:12]}",
                "started_at": utc_now().isoformat(),
                "logs": [],
            },
            workspace_id=workspace_id,
            status=BuildStatus.Running.value,
        )
        record.manifest_path = str(self.context.paths.build_path(record.id) / "manifest.json")
        return repository.records.upsert(
            record,
            workspace_id=workspace_id,
            status=record.status.value,
        )

    def _write_build_files(
        self,
        record: ImageBuildRecord,
        plan: ImageBuildPlan,
        *,
        session: ImageBuildSessionPlan,
        credential_plan: ImageBuildCredentialPlan | None,
        workspace_id: str,
    ) -> ImageBuildRecord:
        if record.manifest_path is None:
            msg = f"image build has no manifest path: {record.id}"
            raise ValueError(msg)
        manifest_path = Path(record.manifest_path)
        _dockerfile_path_for_manifest(manifest_path).write_text(
            plan.dockerfile,
            encoding="utf-8",
        )
        return self._write_manifest(
            record,
            plan,
            session=session,
            credential_plan=credential_plan,
            workspace_id=workspace_id,
        )

    def _write_manifest(
        self,
        record: ImageBuildRecord,
        plan: ImageBuildPlan,
        *,
        session: ImageBuildSessionPlan,
        credential_plan: ImageBuildCredentialPlan | None,
        workspace_id: str,
    ) -> ImageBuildRecord:
        if record.manifest_path is None:
            msg = f"image build has no manifest path: {record.id}"
            raise ValueError(msg)
        manifest_path = Path(record.manifest_path)
        manifest_path.write_text(
            json.dumps(
                _manifest_payload(
                    record,
                    plan,
                    session=session,
                    credential_plan=credential_plan,
                ),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        record = self.get(record.id, workspace_id=workspace_id)
        record.phase = ImageBuildPhase.Manifest
        with self.context.database.session() as database_session:
            return ImageBuildRepository(database_session).upsert(
                record,
                workspace_id=workspace_id,
            )

    def _execution_request(
        self,
        record: ImageBuildRecord,
        plan: ImageBuildPlan,
        *,
        session: ImageBuildSessionPlan,
        credential_plan: ImageBuildCredentialPlan | None,
        registry_credential_payload: str,
        build_args: dict[str, str],
        workspace_id: str,
        sensitive_values: tuple[str, ...],
    ) -> ImageBuildExecutionRequest:
        if record.manifest_path is None:
            msg = f"image build has no manifest path: {record.id}"
            raise ValueError(msg)
        manifest_path = Path(record.manifest_path)
        return ImageBuildExecutionRequest(
            build_id=record.id,
            workspace_id=workspace_id,
            image_id=plan.image_id,
            tag=record.tag or "",
            build_dir=str(manifest_path.parent),
            dockerfile_path=str(_dockerfile_path_for_manifest(manifest_path)),
            manifest_path=str(manifest_path),
            plan=plan,
            session=session,
            credential_plan=credential_plan,
            registry_credential_payload=registry_credential_payload,
            build_args=build_args,
            event_sink=lambda event: self._append_execution_stream_event(
                record.id,
                event,
                workspace_id=workspace_id,
                sensitive_values=sensitive_values,
            ),
        )

    def _append_execution_stream_event(
        self,
        build_id: str,
        event: ImageBuildStreamEventPlan,
        *,
        workspace_id: str,
        sensitive_values: tuple[str, ...],
    ) -> None:
        if event.done:
            return
        self.append_stream_event(
            build_id,
            event,
            workspace_id=workspace_id,
            sensitive_values=sensitive_values,
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

        with self.context.database.session() as session:
            repository = ImageBuildRepository(session)
            record = repository.get_claimed_publication(
                build_id,
                workspace_id=workspace_id,
                claim_id=claim_id,
            )
            if record is None:
                raise RuntimeError("image build publication ownership was lost before promotion")
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
        if self.executor.requires_archive_publication and not archive_published:
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

    def complete(
        self,
        build_id: str,
        *,
        workspace_id: str | None = None,
    ) -> ImageBuildRecord:
        resolved_workspace_id = self._resolve_workspace_id(workspace_id)
        record = self.get(build_id, workspace_id=resolved_workspace_id)
        if _is_terminal_build_status(record.status):
            return record
        event = plan_image_build_complete_event(
            image_id=record.image_id or "",
            build_id=record.id,
            python_version=record.image.python_version,
        )
        return self._persist_stream_event(
            record,
            event,
            workspace_id=resolved_workspace_id,
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

    def cancel(
        self,
        build_id: str,
        *,
        workspace_id: str | None = None,
        container_connected: bool = False,
        reason: str = "Build was aborted.",
    ) -> ImageBuildRecord:
        resolved_workspace_id = self._resolve_workspace_id(workspace_id)
        record = self.get(build_id, workspace_id=resolved_workspace_id)
        return self._cancel_record(
            record,
            workspace_id=resolved_workspace_id,
            container_connected=container_connected,
            reason=reason,
        )

    def cancel_for_workspace(
        self,
        build_id: str,
        *,
        workspace_id: str,
        container_connected: bool = False,
        reason: str = "Build was aborted.",
    ) -> ImageBuildRecord:
        record = self.get_for_workspace(build_id, workspace_id=workspace_id)
        return self._cancel_record(
            record,
            workspace_id=workspace_id,
            container_connected=container_connected,
            reason=reason,
        )

    def _cancel_record(
        self,
        record: ImageBuildRecord,
        *,
        workspace_id: str,
        container_connected: bool,
        reason: str,
    ) -> ImageBuildRecord:
        if _is_terminal_build_status(record.status):
            return record
        lifecycle_metadata: dict[str, str] = {}
        if self.container_lifecycle is not None:
            lifecycle = self.container_lifecycle.cancel_container(
                record.id,
                context_cancelled=True,
                build_succeeded=record.status is BuildStatus.Complete,
                container_connected=container_connected,
                stopping_ttl_seconds=IMAGE_BUILD_CONTAINER_TTL_SECONDS,
            )
            lifecycle_metadata = lifecycle.cache_metadata
            if lifecycle_metadata:
                record = self._merge_cache_metadata(
                    record.id,
                    lifecycle_metadata,
                    workspace_id=workspace_id,
                )
        plan = plan_image_build_cancellation(
            context_cancelled=True,
            build_succeeded=record.status is BuildStatus.Complete,
            container_connected=container_connected,
        )
        event = plan_image_build_failure_event(
            reason,
            image_id=record.image_id or "",
            build_id=record.id,
            python_version=record.image.python_version,
            status=BuildStatus.Cancelled,
        )
        return self._persist_stream_event(
            record,
            event,
            workspace_id=workspace_id,
            level=EventLevel.Info,
            data={
                "reason": reason,
                "actions": [action.value for action in plan.actions],
                "container_connected": container_connected,
                **lifecycle_metadata,
            },
        )

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
            )
        if reserved:
            return ImageArchiveReservation(archive=archive, upload_required=True)
        if archive.cleanup_claimed_at is not None:
            # Retention has claimed these bytes and may already be deleting them.
            # Adopting the row would let this build skip an upload for content that
            # is about to disappear, so fail retryably and let cleanup finish.
            raise ConflictError("image archive is being reclaimed")
        if self._archive_bytes_present(archive):
            return ImageArchiveReservation(archive=archive, upload_required=False)
        with self.context.database.session() as session:
            taken = ImageArchiveRepository(session).take_over(
                image_id,
                expected_sha256=archive.sha256,
                bucket=settings.bucket,
                object_key=object_key,
                size_bytes=size_bytes,
                sha256=sha256,
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

    def _record_lifecycle_session(
        self,
        record: ImageBuildRecord,
        session: ImageBuildSessionPlan,
        *,
        workspace_id: str,
    ) -> ImageBuildRecord:
        metadata = image_build_lifecycle_session_metadata(session)
        if self.container_lifecycle is not None:
            result = self.container_lifecycle.start_session(session)
            metadata = {**metadata, **result.cache_metadata}
            if result.reason:
                metadata["build_container_lifecycle_reason"] = result.reason
            if result.status is ImageBuildContainerLifecycleStatus.Error:
                self._merge_cache_metadata(
                    record.id,
                    metadata,
                    workspace_id=workspace_id,
                )
                msg = result.reason or "image build container lifecycle start failed"
                raise RuntimeError(msg)
        return self._merge_cache_metadata(
            record.id,
            metadata,
            workspace_id=workspace_id,
        )

    def _merge_cache_metadata(
        self,
        build_id: str,
        metadata: dict[str, str],
        *,
        workspace_id: str,
    ) -> ImageBuildRecord:
        record = self.get(build_id, workspace_id=workspace_id)
        if not metadata:
            return record
        record.cache_metadata = {**record.cache_metadata, **metadata}
        with self.context.database.session() as session:
            return ImageBuildRepository(session).upsert(record, workspace_id=workspace_id)

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

    def stream_events(
        self,
        build_id: str,
        *,
        workspace_id: str | None = None,
    ) -> list[ImageBuildStreamEventPlan]:
        resolved_workspace_id = self._resolve_workspace_id(workspace_id)
        record = self.get(build_id, workspace_id=resolved_workspace_id)
        terminal = _is_terminal_build_status(record.status)
        messages = list(record.logs)
        if terminal and messages:
            messages = messages[:-1]
        events = [
            plan_image_build_log_event(
                message,
                image_id=record.image_id or "",
                build_id=record.id,
                python_version=record.image.python_version,
            )
            for message in messages
        ]
        if terminal:
            events.append(self._stream_event_from_record(record))
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
                if _build_record_matches_executor(record, self.executor)
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
                if _build_record_matches_executor(record, self.executor)
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
        if event.message:
            _append_image_build_diagnostic(
                record,
                event.message,
                sensitive_values=sensitive_values,
            )
        record.status = event.status
        record.phase = event.phase
        if event.error:
            record.error = _sanitize_image_build_diagnostic(
                event.error,
                sensitive_values=sensitive_values,
            )
        if event.done:
            record.finished_at = utc_now()
        with self.context.database.session() as session:
            saved = ImageBuildRepository(session).upsert(record, workspace_id=workspace_id)
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


def _image_build_sensitive_values(
    plan: ImageBuildPlan,
    *,
    registry_credential_payload: str,
    build_args: dict[str, str],
) -> tuple[str, ...]:
    values = {
        *(value for value in plan.spec.env.values() if value),
        *(value for value in build_args.values() if value),
        *_json_string_values(registry_credential_payload),
    }
    if registry_credential_payload:
        values.add(registry_credential_payload)
    return tuple(sorted(values, key=len, reverse=True))


def _json_string_values(serialized: str) -> set[str]:
    if not serialized:
        return set()
    try:
        payload = _JSON_VALUE_ADAPTER.validate_json(serialized)
    except ValueError:
        return {serialized}

    values: set[str] = set()

    def collect(value: JsonValue) -> None:
        if isinstance(value, str):
            if value:
                values.add(value)
            return
        if isinstance(value, list):
            for item in value:
                collect(item)
            return
        if isinstance(value, dict):
            for item in value.values():
                collect(item)

    collect(payload)
    return values


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


def _manifest_payload(
    record: ImageBuildRecord,
    plan: ImageBuildPlan,
    *,
    session: ImageBuildSessionPlan,
    credential_plan: ImageBuildCredentialPlan | None,
) -> dict[str, JsonValue]:
    return {
        "id": record.id,
        "tag": record.tag,
        "fingerprint": record.fingerprint,
        "image_id": plan.image_id,
        "cache_key": plan.cache_key,
        "context_digest": plan.context_digest,
        "credential_keys": _string_json_values(plan.credential_keys),
        "dockerfile": plan.dockerfile,
        "dockerfile_path": str(
            _dockerfile_path_for_manifest(Path(record.manifest_path or "manifest.json"))
        ),
        "image": _model_json_value(plan.spec),
        "cache": {
            "cache_key": plan.cache_key,
            "context_digest": plan.context_digest,
            "fingerprint": record.fingerprint,
            "tag": record.tag,
        },
        "credential_plan": (
            _model_json_value(credential_plan) if credential_plan is not None else None
        ),
        "session": _model_json_value(session),
    }


def _model_json_value(model: BaseModel) -> JsonValue:
    return _JSON_VALUE_ADAPTER.validate_json(model.model_dump_json())


def _string_json_values(values: list[str]) -> list[JsonValue]:
    result: list[JsonValue] = []
    result.extend(values)
    return result


def _dockerfile_path_for_manifest(manifest_path: Path) -> Path:
    return manifest_path.with_name("Dockerfile")


def _is_terminal_build_status(status: BuildStatus) -> bool:
    return status in _TERMINAL_BUILD_STATUSES


_TERMINAL_BUILD_STATUSES = {
    BuildStatus.Complete,
    BuildStatus.Failed,
    BuildStatus.Cancelled,
    BuildStatus.Timeout,
}
