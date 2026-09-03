from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Generator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pytest
from api.server.services import ApiServices
from database.repositories.images import ImageArchiveRepository, ImageBuildRepository
from images.building import (
    ImageBuildCredentialPlan,
    ImageBuildStreamEventKind,
    ImageBuildStreamEventPlan,
)
from images.cleanup import (
    ImageBuildCleanupAction,
    ImageBuildCleanupStatus,
    LocalImageBuildCleanupExecutor,
    plan_image_build_cleanup,
)
from images.control import ImageControlService
from images.execution import (
    ImageBuildExecutionRequest,
    ImageBuildExecutionResult,
    ImageBuildExecutorKind,
    ImageBuildProcessResult,
    LocalDockerImageBuildExecutor,
    ManifestImageBuildExecutor,
)
from images.publication import (
    ArchiveImageBuildPublicationPublisher,
    ImageBuildPublication,
    ImageBuildPublicationPublishStatus,
    ImageBuildPublicationStatus,
)
from images.service import ImageBuildExecution, ImageBuildService
from pydantic import JsonValue, TypeAdapter
from shared.http.images import (
    BuildImageRequest,
    BuildImageResponse,
    VerifyImageBuildRequest,
    VerifyImageBuildResponse,
)
from shared.image_building.authoring import ImageSpec
from shared.image_building.records import BuildStatus, ImageBuildPhase, ImageBuildRecord
from storage.image_archive import ResolvedImageArchiveSettings
from storage_client.s3 import S3ObjectInfo, S3ObjectStoreSettings

_TEST_BASE_IMAGE_DIGEST = f"sha256:{'a' * 64}"
_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


def _archive_settings() -> ResolvedImageArchiveSettings:
    return ResolvedImageArchiveSettings(
        storage=S3ObjectStoreSettings(bucket="image-archives"),
        prefix="",
        presign_seconds=900,
    )


def _image_control_service(services: ApiServices) -> ImageControlService:
    return ImageControlService(
        services,
        base_image_digest_inspector=lambda _source, _credentials: _TEST_BASE_IMAGE_DIGEST,
    )


def _default_workspace_id(services: ApiServices) -> str:
    with services.context.database.session() as session:
        return services.context.default_workspace_id(session)


def _verify_image(
    service: ImageControlService,
    services: ApiServices,
    request: VerifyImageBuildRequest,
) -> VerifyImageBuildResponse:
    return service.verify_image_build(
        request,
        workspace_id=_default_workspace_id(services),
    )


def _build_image(
    service: ImageControlService,
    services: ApiServices,
    request: BuildImageRequest,
) -> Generator[BuildImageResponse, None, None]:
    return service.build_image(
        request,
        workspace_id=_default_workspace_id(services),
    )


def test_image_control_rejects_unresolved_mutable_base(isolated_services: ApiServices) -> None:
    service = ImageControlService(
        isolated_services,
        base_image_digest_inspector=lambda _source, _credentials: "",
    )

    response = _verify_image(
        service,
        isolated_services,
        VerifyImageBuildRequest(existing_image_uri="registry.example/team/app:latest"),
    )

    assert not response.valid
    assert not response.exists
    assert "digest could not be resolved" in response.reason


def test_image_control_rejects_oversized_context_before_download(
    isolated_services: ApiServices,
) -> None:
    downloaded = False

    class ContextReader:
        @dataclass(slots=True)
        class Record:
            size: int

        def get_by_id_for_workspace(
            self,
            object_id: str,
            *,
            workspace_id: str,
        ) -> Record:
            assert workspace_id == _default_workspace_id(isolated_services)
            assert object_id == "oversized-context"
            return self.Record(size=257 * 1024 * 1024)

        def download_by_id_for_workspace(
            self,
            object_id: str,
            target: str | Path,
            *,
            workspace_id: str,
        ) -> Record:
            assert workspace_id == _default_workspace_id(isolated_services)
            nonlocal downloaded
            downloaded = True
            return self.get_by_id_for_workspace(object_id, workspace_id=workspace_id)

    service = ImageControlService(
        isolated_services,
        build_context_reader=ContextReader(),
    )

    response = _verify_image(
        service, isolated_services, VerifyImageBuildRequest(build_ctx_object="oversized-context")
    )

    assert not response.valid
    assert "exceeds maximum size" in response.reason
    assert not downloaded


def test_concurrent_equivalent_builds_share_one_durable_record(
    isolated_services: ApiServices,
) -> None:
    delegate = ManifestImageBuildExecutor()

    class DelayedManifestExecutor:
        cache_markers: ClassVar[frozenset[str]] = frozenset({ImageBuildExecutorKind.Manifest.value})
        requires_archive_publication: ClassVar[bool] = False

        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.executions = 0
            self.lock = threading.Lock()

        def execute(self, request: ImageBuildExecutionRequest) -> ImageBuildExecutionResult:
            with self.lock:
                self.executions += 1
            self.started.set()
            assert self.release.wait(timeout=5)
            return delegate.execute(request)

    executor = DelayedManifestExecutor()
    isolated_services.images.executor = executor
    isolated_services.images.duplicate_wait_poll_seconds = 0.01
    isolated_services.images.duplicate_wait_timeout_seconds = 5
    spec = ImageSpec(ignore_python=True, commands=["printf identity"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(isolated_services.images.execute, spec)
        assert executor.started.wait(timeout=5)
        second = pool.submit(isolated_services.images.execute, spec)
        time.sleep(0.1)
        executor.release.set()
        first_result = first.result(timeout=5)
        second_result = second.result(timeout=5)

    matching = [
        record
        for record in isolated_services.images.list()
        if record.fingerprint == first_result.record.fingerprint
    ]
    assert first_result.record.id == second_result.record.id
    assert executor.executions == 1
    assert len(matching) == 1
    assert second_result.record.phase is ImageBuildPhase.Reused


def test_archive_executor_rebuilds_legacy_completed_image(
    isolated_services: ApiServices,
) -> None:
    class LegacyBuildContainerExecutor:
        cache_markers: ClassVar[frozenset[str]] = frozenset(
            {ImageBuildExecutorKind.BuildContainer.value}
        )
        requires_archive_publication: ClassVar[bool] = False

        def execute(self, request: ImageBuildExecutionRequest) -> ImageBuildExecutionResult:
            del request
            return ImageBuildExecutionResult(
                status=BuildStatus.Complete,
                cache_metadata={
                    "executor": ImageBuildExecutorKind.BuildContainer.value,
                    "scheduler_submit_status": "submitted",
                    "build_container_required": "true",
                },
            )

    class CurrentBuildContainerExecutor(LegacyBuildContainerExecutor):
        requires_archive_publication: ClassVar[bool] = True

    service = isolated_services.images
    spec = ImageSpec(ignore_python=True, commands=["printf archive-cutover"])
    service.executor = LegacyBuildContainerExecutor()
    legacy = service.execute(spec).record

    service.executor = CurrentBuildContainerExecutor()
    replacement = service.start(spec).record

    assert legacy.status is BuildStatus.Complete
    assert replacement.id != legacy.id
    assert replacement.status is BuildStatus.Running


def test_stale_image_build_claim_is_failed_and_replaced_after_crash(
    isolated_services: ApiServices,
) -> None:
    service = isolated_services.images
    spec = ImageSpec(ignore_python=True, commands=["printf crashed-owner"])
    crashed = service.start(spec)
    service.claim_lease_seconds = 0

    replacement = service.execute(spec)
    crashed_record = service.get(crashed.record.id)

    assert crashed_record.status is BuildStatus.Failed
    assert crashed_record.phase is ImageBuildPhase.Failed
    assert "lease expired" in (crashed_record.error or "")
    assert replacement.record.status is BuildStatus.Complete
    assert replacement.record.id != crashed.record.id


def test_stale_owner_cannot_publish_after_claim_takeover(isolated_services: ApiServices) -> None:
    delegate = ManifestImageBuildExecutor()

    class RecordingArchiveStore:
        def __init__(self) -> None:
            self.checks: list[tuple[str, str | None]] = []

        def exists(self, key: str, *, bucket: str | None = None) -> bool:
            return True

        def head(
            self,
            key: str,
            *,
            bucket: str | None = None,
        ) -> S3ObjectInfo:
            self.checks.append((key, bucket))
            return S3ObjectInfo(
                bucket=bucket or "image-archives",
                key=key,
                size=1,
                metadata={"artifact-sha256": "a" * 64},
            )

    class DelayedManifestExecutor:
        cache_markers: ClassVar[frozenset[str]] = frozenset({ImageBuildExecutorKind.Manifest.value})
        requires_archive_publication: ClassVar[bool] = True

        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def execute(self, request: ImageBuildExecutionRequest) -> ImageBuildExecutionResult:
            self.started.set()
            assert self.release.wait(timeout=5)
            result = delegate.execute(request)
            return result.model_copy(
                update={
                    "cache_metadata": {
                        **result.cache_metadata,
                        "published_archive_object_key": (
                            f"image-archives/{request.image_id}.rclip"
                        ),
                    }
                }
            )

    executor = DelayedManifestExecutor()
    archive_store = RecordingArchiveStore()
    service = isolated_services.images
    service.executor = executor
    service.publication_publisher = ArchiveImageBuildPublicationPublisher(
        object_store=archive_store,
        settings=_archive_settings(),
        context=isolated_services.context,
    )
    service.claim_heartbeat_seconds = 30
    service.claim_lease_seconds = 300
    spec = ImageSpec(ignore_python=True, commands=["printf fenced-owner"])
    results: list[ImageBuildRecord] = []

    thread = threading.Thread(target=lambda: results.append(service.execute(spec).record))
    thread.start()
    assert executor.started.wait(timeout=5)
    service.claim_lease_seconds = 0
    replacement = service.start(spec)
    executor.release.set()
    thread.join(timeout=5)

    assert len(results) == 1
    assert results[0].status is BuildStatus.Failed
    assert "lease expired" in (results[0].error or "")
    assert replacement.record.id != results[0].id
    assert replacement.record.status is BuildStatus.Running
    assert archive_store.checks == []


@pytest.mark.parametrize(
    ("head_size", "head_sha256"),
    [
        (1025, "a" * 64),
        (1024, "b" * 64),
    ],
)
def test_archive_publication_rejects_head_integrity_mismatch(
    isolated_services: ApiServices,
    head_size: int,
    head_sha256: str,
) -> None:
    workspace_id = _default_workspace_id(isolated_services)
    image_id = "image-integrity"
    build_id = "build-integrity"
    archive_sha256 = "a" * 64
    archive_key = f"image-archives/{image_id}.rclip"
    with isolated_services.context.database.session() as session:
        archive, _ = ImageArchiveRepository(session).reserve(
            image_id,
            bucket="image-archives",
            object_key=archive_key,
            size_bytes=1024,
            sha256=archive_sha256,
            registry_ref=f"registry.example.com/workloads@sha256:{'c' * 64}",
            manifest_digest="sha256:" + "c" * 64,
            architecture="amd64",
            format_version=2,
        )

    class IntegrityMismatchStore:
        def exists(self, key: str, *, bucket: str | None = None) -> bool:
            return True

        def head(self, key: str, *, bucket: str | None = None) -> S3ObjectInfo:
            return S3ObjectInfo(
                bucket=bucket or "image-archives",
                key=key,
                size=head_size,
                metadata={"artifact-sha256": head_sha256},
            )

    result = ArchiveImageBuildPublicationPublisher(
        object_store=IntegrityMismatchStore(),
        settings=_archive_settings(),
        context=isolated_services.context,
    ).publish(
        ImageBuildRecord(
            id=build_id,
            image=ImageSpec(ignore_python=True, commands=["true"]),
            fingerprint="integrity",
            image_id=image_id,
        ),
        ImageBuildPublication(
            status=ImageBuildPublicationStatus.Published,
            workspace_id=workspace_id,
            cache_metadata={
                "published_archive_object_key": archive.object_key,
                "published_archive_size_bytes": str(archive.size_bytes),
                "published_archive_sha256": archive.sha256,
            },
        ),
    )

    assert result.status is ImageBuildPublicationPublishStatus.Error
    assert result.reason == "image build archive failed size or sha256 verification"


def test_image_build_cleanup_plans_and_removes_build_directory(
    isolated_services: ApiServices,
) -> None:
    record = isolated_services.images.build(ImageSpec(packages=["httpx"]), tag="local:cleanup")
    assert record.manifest_path is not None
    build_dir = Path(record.manifest_path).parent
    assert build_dir.exists()

    keep_plan = plan_image_build_cleanup(record, keep_artifacts=True)
    assert keep_plan.actions == [ImageBuildCleanupAction.KeepArtifacts]
    assert not keep_plan.should_cleanup

    cleanup = isolated_services.images.cleanup_build(
        record.id,
        keep_artifacts=False,
        executor=LocalImageBuildCleanupExecutor(),
    )
    updated = isolated_services.images.get(record.id)

    assert cleanup.status is ImageBuildCleanupStatus.Complete
    assert str(build_dir) in cleanup.removed_paths
    assert not build_dir.exists()
    assert updated.cache_metadata["cleanup_status"] == ImageBuildCleanupStatus.Complete.value
    assert updated.cache_metadata["cleanup_actions"] == (
        ImageBuildCleanupAction.RemoveBuildDirectory.value
    )

    repeated = isolated_services.images.cleanup_build(
        record.id,
        keep_artifacts=False,
        executor=LocalImageBuildCleanupExecutor(),
    )
    assert repeated.status is ImageBuildCleanupStatus.Complete
    assert repeated.removed_paths == []


def test_image_control_service_delivers_build_secrets_without_persisting_values(
    isolated_services: ApiServices,
) -> None:
    commands: list[list[str]] = []

    def runner(
        command: Sequence[str],
        _cwd: Path,
        _timeout_seconds: int | None,
    ) -> ImageBuildProcessResult:
        commands.append(list(command))
        return ImageBuildProcessResult(exit_code=0, stdout="built\n")

    isolated_services.images.executor = LocalDockerImageBuildExecutor(runner=runner)
    isolated_services.secrets.set("API_TOKEN", "stored-secret")

    responses = list(
        _build_image(
            _image_control_service(isolated_services),
            isolated_services,
            BuildImageRequest(
                python_packages=["httpx"],
                secrets=["API_TOKEN"],
            ),
        )
    )
    record = isolated_services.images.list()[0]

    assert responses[-1].status is BuildStatus.Complete
    command_text = " ".join(commands[0])
    assert "API_TOKEN=stored-secret" in command_text
    assert any("API_TOKEN=<redacted>" in log for log in record.logs)

    serialized_manifest = Path(record.manifest_path or "").read_text(encoding="utf-8")
    serialized_logs = json.dumps(record.logs)
    serialized_responses = json.dumps([response.model_dump(mode="json") for response in responses])
    assert "stored-secret" not in serialized_manifest
    assert "stored-secret" not in serialized_logs
    assert "stored-secret" not in serialized_responses

    manifest = json.loads(serialized_manifest)
    assert manifest["image"]["secrets"] == ["API_TOKEN"]
    assert manifest["image"]["build_secret_versions"]["API_TOKEN"]


def test_image_control_stream_uses_execution_terminal_after_durable_event_cleanup(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_stream_events = ImageBuildService.stream_events

    def nonterminal_stream_events(
        service: ImageBuildService,
        build_id: str,
        *,
        workspace_id: str | None = None,
    ) -> list[ImageBuildStreamEventPlan]:
        return [
            event
            for event in original_stream_events(
                service,
                build_id,
                workspace_id=workspace_id,
            )
            if not event.done
        ]

    monkeypatch.setattr(ImageBuildService, "stream_events", nonterminal_stream_events)

    responses = list(
        _build_image(
            _image_control_service(isolated_services),
            isolated_services,
            BuildImageRequest(python_packages=["httpx"]),
        )
    )

    assert responses[-1].done
    assert responses[-1].success
    assert responses[-1].status is BuildStatus.Complete


def test_image_control_stream_uses_execution_terminal_after_durable_record_cleanup(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_execute = ImageBuildService.execute

    def execute_and_delete_record(
        service: ImageBuildService,
        image: ImageSpec,
        *,
        workspace_id: str | None = None,
        tag: str | None = None,
        credential_plan: ImageBuildCredentialPlan | None = None,
        registry_credential_payload: str | None = None,
        build_args: dict[str, str] | None = None,
        on_build_started: Callable[[ImageBuildRecord], None] | None = None,
    ) -> ImageBuildExecution:
        execution = original_execute(
            service,
            image,
            workspace_id=workspace_id,
            tag=tag,
            credential_plan=credential_plan,
            registry_credential_payload=registry_credential_payload,
            build_args=build_args,
            on_build_started=on_build_started,
        )
        with service.context.database.session() as session:
            removed = ImageBuildRepository(session).delete_across_workspaces(execution.record.id)
        assert removed
        return execution

    monkeypatch.setattr(ImageBuildService, "execute", execute_and_delete_record)

    responses = list(
        _build_image(
            _image_control_service(isolated_services),
            isolated_services,
            BuildImageRequest(python_packages=["httpx"]),
        )
    )

    assert responses[-1].done
    assert responses[-1].success
    assert responses[-1].status is BuildStatus.Complete


def test_image_control_secret_version_invalidates_identity_and_inline_values_fail(
    isolated_services: ApiServices,
) -> None:
    service = _image_control_service(isolated_services)
    isolated_services.secrets.set("API_TOKEN", "first")
    request = VerifyImageBuildRequest(secrets=["API_TOKEN"])
    first = _verify_image(service, isolated_services, request)

    isolated_services.secrets.set("API_TOKEN", "second")
    second = _verify_image(service, isolated_services, request)
    inline = _verify_image(
        service,
        isolated_services,
        VerifyImageBuildRequest(secrets=["API_TOKEN=plaintext"]),
    )

    assert first.valid
    assert second.valid
    assert first.cache_key != second.cache_key
    assert first.image_id != second.image_id
    assert not inline.valid
    assert "stored secret names" in inline.reason


def test_image_control_service_cancels_running_build_when_stream_closes(
    isolated_services: ApiServices,
) -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingExecutor:
        cache_markers: ClassVar[frozenset[str]] = frozenset({"blocking-test"})
        requires_archive_publication: ClassVar[bool] = False

        def execute(self, request: ImageBuildExecutionRequest) -> ImageBuildExecutionResult:
            del request
            started.set()
            release.wait(timeout=5)
            return ImageBuildExecutionResult(status=BuildStatus.Complete)

    isolated_services.images.executor = BlockingExecutor()
    stream = _build_image(
        _image_control_service(isolated_services),
        isolated_services,
        BuildImageRequest(python_packages=[]),
    )

    first_response = next(stream)
    try:
        assert first_response.build_id
        assert started.wait(timeout=2)

        stream.close()

        cancelled = isolated_services.images.get(first_response.build_id)
        assert cancelled.status is BuildStatus.Cancelled
        assert cancelled.error == "Build stream was closed."
    finally:
        release.set()

    isolated_services.images.close()
    isolated_services.images.close()

    assert isolated_services.images.active_background_execution_count == 0
    assert not [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith(("image-build-", "image-build-claim-"))
    ]
    with pytest.raises(RuntimeError, match="image build service is closed"):
        isolated_services.images.start_background_execution(ImageSpec(ignore_python=True))


def test_runtime_image_build_preserves_executor_terminal_status_without_events(
    isolated_services: ApiServices,
) -> None:
    class TimeoutExecutor:
        cache_markers: ClassVar[frozenset[str]] = frozenset({"timeout-test"})
        requires_archive_publication: ClassVar[bool] = False

        def execute(self, request: ImageBuildExecutionRequest) -> ImageBuildExecutionResult:
            del request
            return ImageBuildExecutionResult(
                status=BuildStatus.Timeout,
                reason="build executor timed out",
            )

    isolated_services.images.executor = TimeoutExecutor()

    record = isolated_services.images.build(ImageSpec(packages=["httpx"]), tag="local:test")

    assert record.status is BuildStatus.Timeout
    assert record.error == "build executor timed out"
    assert (
        isolated_services.images.get_image_metadata(
            record.image_id or "",
            workspace_id=_default_workspace_id(isolated_services),
        )
        is None
    )


def test_runtime_image_build_start_and_cancel_are_persisted(isolated_services: ApiServices) -> None:
    execution = isolated_services.images.start(
        ImageSpec(packages=["httpx"]),
        tag="local:cancel",
    )

    assert execution.record.status is BuildStatus.Running
    assert execution.session.build_container_required is True
    assert execution.session.container_id == execution.record.id
    assert execution.events[0].message == "Building image...\n"

    cancelled = isolated_services.images.cancel(
        execution.session.container_id,
        container_connected=True,
        reason="Build was aborted.",
    )
    assert cancelled.status is BuildStatus.Cancelled
    assert cancelled.phase is ImageBuildPhase.Failed
    assert cancelled.error == "Build was aborted."
    assert cancelled.finished_at is not None
    assert isolated_services.images.complete(cancelled.id).status is BuildStatus.Cancelled

    stream_events = isolated_services.images.stream_events(cancelled.id)
    assert stream_events[-1].kind is ImageBuildStreamEventKind.Cancelled
    assert stream_events[-1].done is True

    image_events = isolated_services.events.list()
    cancel_events = [event for event in image_events if event.action == "image.build.cancelled"]
    assert cancel_events
    actions = cancel_events[-1].data["actions"]
    assert isinstance(actions, list)
    assert "kill-container" in actions


def _json_object(value: JsonValue, *, name: str) -> dict[str, JsonValue]:
    assert isinstance(value, dict), f"{name} must be a JSON object"
    return value
