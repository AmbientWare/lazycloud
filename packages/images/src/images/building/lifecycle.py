from __future__ import annotations

from math import ceil

from shared.image_building.authoring import ImageSpec
from shared.image_building.records import BuildStatus, ImageBuildPhase

from images.building.models import (
    ImageBuildCancellationPlan,
    ImageBuildLifecycleAction,
    ImageBuildSessionPlan,
    ImageBuildSessionStep,
    ImageBuildSpinupTimeoutPlan,
    ImageBuildSpinupTimeoutReason,
    ImageBuildStreamEventKind,
    ImageBuildStreamEventPlan,
    ImageBuildWorkReason,
)
from images.building.work import image_build_work_plan

BUILD_CONTAINER_KEEPALIVE_INTERVAL_SECONDS = 10
IMAGE_BUILD_CONTAINER_TTL_SECONDS = 60
DEFAULT_BUILD_CONTAINER_SPINUP_TIMEOUT_SECONDS = 600
DOCKERFILE_BUILD_CONTAINER_SPINUP_TIMEOUT_SECONDS = 60 * 60


def plan_image_build_session(
    image: ImageSpec,
    *,
    require_archive_publication: bool = False,
    clip_version: int = 2,
    image_id: str = "",
    build_id: str = "",
    context_cancelled: bool = False,
    build_succeeded: bool = False,
    container_connected: bool = False,
    source_image_size_bytes: int | None = None,
    archive_nanoseconds_per_byte: int = 0,
    source_image_inspect_failed: bool = False,
) -> ImageBuildSessionPlan:
    work = image_build_work_plan(image)
    v2 = clip_version >= 2
    cancellation = plan_image_build_cancellation(
        context_cancelled=context_cancelled,
        build_succeeded=build_succeeded,
        container_connected=container_connected,
    )
    spinup_timeout = plan_image_build_spinup_timeout(
        image,
        source_image_size_bytes=source_image_size_bytes,
        archive_nanoseconds_per_byte=archive_nanoseconds_per_byte,
        source_image_inspect_failed=source_image_inspect_failed,
    )
    if not work.has_work and not require_archive_publication:
        return ImageBuildSessionPlan(
            clip_version=clip_version,
            image_id=image_id,
            build_id=build_id,
            container_id=build_id,
            build_container_required=False,
            v2=v2,
            steps=[ImageBuildSessionStep.Complete],
            work_reasons=[],
            spinup_timeout=spinup_timeout,
            cancellation=cancellation,
            stream_events=[
                plan_image_build_complete_event(
                    image_id=image_id,
                    build_id=build_id,
                    python_version=image.python_version,
                )
            ],
        )

    steps = [
        ImageBuildSessionStep.StartContainer,
        ImageBuildSessionStep.RefreshContainerTtl,
        ImageBuildSessionStep.StreamLogs,
        ImageBuildSessionStep.WaitForContainer,
    ]
    if not v2:
        steps.extend(
            [
                ImageBuildSessionStep.PrepareRuntimeCommands,
                ImageBuildSessionStep.ExecuteRuntimeCommands,
                ImageBuildSessionStep.ArchiveFilesystem,
            ]
        )
    steps.append(ImageBuildSessionStep.Complete)

    start_message = "Building image...\n" if v2 else "Setting up build container...\n"
    return ImageBuildSessionPlan(
        clip_version=clip_version,
        image_id=image_id,
        build_id=build_id,
        container_id=build_id,
        build_container_required=True,
        v2=v2,
        steps=steps,
        work_reasons=[
            *work.reasons,
            *(
                [ImageBuildWorkReason.ArchivePublication]
                if require_archive_publication and not work.has_work
                else []
            ),
        ],
        spinup_timeout=spinup_timeout,
        cancellation=cancellation,
        stream_events=[
            plan_image_build_log_event(
                start_message,
                image_id=image_id,
                build_id=build_id,
                python_version=image.python_version,
            )
        ],
        ttl_seconds=IMAGE_BUILD_CONTAINER_TTL_SECONDS,
        keepalive_interval_seconds=BUILD_CONTAINER_KEEPALIVE_INTERVAL_SECONDS,
    )


def plan_image_build_reused_stream(
    *,
    image_id: str,
    build_id: str,
    python_version: str,
) -> list[ImageBuildStreamEventPlan]:
    return [
        ImageBuildStreamEventPlan(
            kind=ImageBuildStreamEventKind.Reused,
            image_id=image_id,
            build_id=build_id,
            message="Image already exists\n",
            success=True,
            python_version=python_version,
            status=BuildStatus.Complete,
            phase=ImageBuildPhase.Reused,
        ),
        plan_image_build_complete_event(
            image_id=image_id,
            build_id=build_id,
            python_version=python_version,
            phase=ImageBuildPhase.Reused,
        ),
    ]


def plan_image_build_log_event(
    message: str,
    *,
    image_id: str = "",
    build_id: str = "",
    python_version: str = "",
    warning: bool = False,
) -> ImageBuildStreamEventPlan:
    normalized = _ensure_trailing_newline(message)
    return ImageBuildStreamEventPlan(
        kind=ImageBuildStreamEventKind.Warning if warning else ImageBuildStreamEventKind.Log,
        image_id=image_id,
        build_id=build_id,
        message=normalized,
        success=True,
        python_version=python_version,
        warning=warning,
        status=BuildStatus.Running,
        phase=image_build_log_phase(normalized),
    )


def plan_image_build_complete_event(
    *,
    image_id: str,
    build_id: str,
    python_version: str,
    phase: ImageBuildPhase = ImageBuildPhase.Complete,
) -> ImageBuildStreamEventPlan:
    return ImageBuildStreamEventPlan(
        kind=ImageBuildStreamEventKind.Complete,
        image_id=image_id,
        build_id=build_id,
        message="Build completed successfully\n",
        done=True,
        success=True,
        python_version=python_version,
        status=BuildStatus.Complete,
        phase=phase,
    )


def plan_image_build_failure_event(
    error: str,
    *,
    image_id: str = "",
    build_id: str = "",
    python_version: str = "",
    status: BuildStatus = BuildStatus.Failed,
    phase: ImageBuildPhase = ImageBuildPhase.Failed,
) -> ImageBuildStreamEventPlan:
    message = error or "image build failed"
    return ImageBuildStreamEventPlan(
        kind=_stream_kind_for_status(status),
        image_id=image_id,
        build_id=build_id,
        message=_ensure_trailing_newline(message),
        done=True,
        success=False,
        python_version=python_version,
        status=status,
        phase=phase,
        error=message,
    )


def image_build_log_phase(message: str) -> ImageBuildPhase:
    normalized = message.strip()
    if normalized.startswith("cache key:"):
        return ImageBuildPhase.Planning
    if normalized == "manifest written":
        return ImageBuildPhase.Manifest
    return ImageBuildPhase.Submitted


def plan_image_build_cancellation(
    *,
    context_cancelled: bool,
    build_succeeded: bool,
    container_connected: bool,
) -> ImageBuildCancellationPlan:
    if not context_cancelled:
        return ImageBuildCancellationPlan(reason="context is still active")
    if build_succeeded:
        return ImageBuildCancellationPlan(reason="build already completed successfully")

    actions = [ImageBuildLifecycleAction.SendStopEvent]
    if container_connected:
        actions.extend(
            [
                ImageBuildLifecycleAction.MarkStopping,
                ImageBuildLifecycleAction.KillContainer,
            ]
        )
        return ImageBuildCancellationPlan(
            actions=actions,
            stop_build_event=True,
            mark_stopping=True,
            kill_container=True,
            reason="cancel connected build container",
        )

    actions.append(ImageBuildLifecycleAction.DeletePendingState)
    return ImageBuildCancellationPlan(
        actions=actions,
        stop_build_event=True,
        delete_pending_state=True,
        reason="cancel pending build container",
    )


def plan_image_build_spinup_timeout(
    image: ImageSpec,
    *,
    source_image_size_bytes: int | None = None,
    archive_nanoseconds_per_byte: int = 0,
    source_image_inspect_failed: bool = False,
) -> ImageBuildSpinupTimeoutPlan:
    if image.dockerfile:
        return ImageBuildSpinupTimeoutPlan(
            timeout_seconds=DOCKERFILE_BUILD_CONTAINER_SPINUP_TIMEOUT_SECONDS,
            reason=ImageBuildSpinupTimeoutReason.Dockerfile,
        )

    if source_image_inspect_failed:
        return ImageBuildSpinupTimeoutPlan(
            timeout_seconds=DEFAULT_BUILD_CONTAINER_SPINUP_TIMEOUT_SECONDS,
            reason=ImageBuildSpinupTimeoutReason.SourceImageInspectFailed,
        )

    if source_image_size_bytes is not None and archive_nanoseconds_per_byte > 0:
        timeout_seconds = max(
            1,
            ceil(source_image_size_bytes * archive_nanoseconds_per_byte * 10 / 1_000_000_000),
        )
        return ImageBuildSpinupTimeoutPlan(
            timeout_seconds=timeout_seconds,
            reason=ImageBuildSpinupTimeoutReason.SourceImageSize,
            source_image_size_bytes=source_image_size_bytes,
            archive_nanoseconds_per_byte=archive_nanoseconds_per_byte,
        )

    return ImageBuildSpinupTimeoutPlan(
        timeout_seconds=DEFAULT_BUILD_CONTAINER_SPINUP_TIMEOUT_SECONDS,
        reason=ImageBuildSpinupTimeoutReason.Default,
        source_image_size_bytes=source_image_size_bytes or 0,
        archive_nanoseconds_per_byte=archive_nanoseconds_per_byte,
    )


def _ensure_trailing_newline(message: str) -> str:
    if not message:
        return "\n"
    return message if message.endswith("\n") else f"{message}\n"


def image_build_stream_event_key(
    event: ImageBuildStreamEventPlan,
) -> tuple[str, str, str, str, str, bool, str]:
    """Identify one stream event so a replayed build does not emit it twice."""

    return (
        event.kind.value,
        event.build_id,
        event.message,
        event.status.value,
        event.phase.value,
        event.done,
        event.error,
    )


def _stream_kind_for_status(status: BuildStatus) -> ImageBuildStreamEventKind:
    if status is BuildStatus.Cancelled:
        return ImageBuildStreamEventKind.Cancelled
    if status is BuildStatus.Timeout:
        return ImageBuildStreamEventKind.Timeout
    return ImageBuildStreamEventKind.Failed
