from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Generator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar

import pytest
from api.server.services import ApiServices
from database.repositories.images import ImageBuildRepository
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
from storage.service import ObjectStorage
from storage_client.s3 import S3ObjectInfo

_TEST_BASE_IMAGE_DIGEST = f"sha256:{'a' * 64}"
_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


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
                        "staging_archive_object_key": (
                            f"image-builds/{request.build_id}/{request.image_id}.rclip"
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
        bucket="image-archives",
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


def test_stale_archive_candidate_cannot_replace_selected_winner(
    isolated_services: ApiServices,
) -> None:
    delegate = ManifestImageBuildExecutor()
    archive_coordinates = ObjectStorage(
        isolated_services.context,
        object_client=isolated_services.object_storage.object_client,
        default_bucket=isolated_services.object_storage.default_bucket,
        allowed_buckets=("image-archives",),
    )

    class ArchiveRequiredExecutor:
        cache_markers: ClassVar[frozenset[str]] = frozenset({ImageBuildExecutorKind.Manifest.value})
        requires_archive_publication: ClassVar[bool] = True

        def __init__(self, store: BlockingArchiveStore, content: bytes) -> None:
            self.store = store
            self.content = content

        def execute(self, request: ImageBuildExecutionRequest) -> ImageBuildExecutionResult:
            result = delegate.execute(request)
            key = f"image-builds/{request.build_id}/{request.image_id}.rclip"
            digest = hashlib.sha256(self.content).hexdigest()
            reserved = archive_coordinates.reserve_for_workspace(
                workspace_id=request.workspace_id,
                bucket="image-archives",
                key=key,
                size=len(self.content),
                sha256=digest,
                content_type="application/x-tar",
                metadata={
                    "kind": "image-build-staging",
                    "build_id": request.build_id,
                    "container_id": request.session.container_id,
                    "image_id": request.image_id,
                },
            )
            physical_bucket = archive_coordinates.physical_bucket("image-archives")
            physical_key = archive_coordinates.physical_key_for_workspace(
                request.workspace_id,
                bucket="image-archives",
                key=key,
            )
            self.store.objects[(physical_bucket, physical_key)] = self.content
            return result.model_copy(
                update={
                    "cache_metadata": {
                        **result.cache_metadata,
                        "container_id": request.session.container_id,
                        "staging_archive_object_id": reserved.id,
                        "staging_archive_object_key": key,
                        "staging_archive_size_bytes": str(len(self.content)),
                        "staging_archive_sha256": digest,
                    }
                }
            )

    class BlockingArchiveStore:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.objects: dict[tuple[str, str], bytes] = {}
            self.old_key = ""

        def head(
            self,
            key: str,
            *,
            bucket: str | None = None,
        ) -> S3ObjectInfo:
            resolved_bucket = bucket or "image-archives"
            content = self.objects[(resolved_bucket, key)]
            if content == b"stale-owner":
                self.old_key = key
                self.started.set()
                assert self.release.wait(timeout=5)
            return S3ObjectInfo(
                bucket=resolved_bucket,
                key=key,
                size=len(content),
                metadata={"artifact-sha256": hashlib.sha256(content).hexdigest()},
            )

    archive_store = BlockingArchiveStore()
    publisher = ArchiveImageBuildPublicationPublisher(
        object_store=archive_store,
        bucket="image-archives",
        object_coordinates=archive_coordinates,
    )
    old_service = replace(
        isolated_services.images,
        executor=ArchiveRequiredExecutor(archive_store, b"stale-owner"),
        publication_publisher=publisher,
        claim_lease_seconds=0,
        claim_heartbeat_seconds=60,
        publication_claim_lease_seconds=0.05,
    )
    replacement_service = replace(
        isolated_services.images,
        executor=ArchiveRequiredExecutor(archive_store, b"winner"),
        publication_publisher=publisher,
        claim_lease_seconds=0,
        claim_heartbeat_seconds=0.01,
        publication_claim_lease_seconds=0.05,
    )
    spec = ImageSpec(ignore_python=True, commands=["echo fenced-promotion"])
    original: list[ImageBuildRecord] = []

    build_thread = threading.Thread(
        target=lambda: original.append(old_service.execute(spec).record)
    )
    build_thread.start()
    assert archive_store.started.wait(timeout=5), [
        (record.status, record.error) for record in original
    ]
    time.sleep(0.1)

    winner = replacement_service.execute(spec).record
    workspace_id = _default_workspace_id(isolated_services)
    selected = replacement_service.get_image_metadata(
        winner.image_id or "",
        workspace_id=workspace_id,
    )
    assert selected is not None
    selected_physical_key = archive_coordinates.physical_key_for_workspace(
        workspace_id,
        bucket="image-archives",
        key=selected.archive_object_key,
    )
    assert selected_physical_key != archive_store.old_key
    assert (
        archive_store.objects[
            (
                archive_coordinates.physical_bucket("image-archives"),
                selected_physical_key,
            )
        ]
        == b"winner"
    )

    archive_store.release.set()
    build_thread.join(timeout=5)

    assert len(original) == 1
    assert original[0].status is BuildStatus.Failed
    assert winner.status is BuildStatus.Complete
    after_stale_resume = replacement_service.get_image_metadata(
        winner.image_id or "",
        workspace_id=workspace_id,
    )
    assert after_stale_resume is not None
    assert after_stale_resume.archive_object_key == selected.archive_object_key
    assert (
        archive_store.objects[
            (
                archive_coordinates.physical_bucket("image-archives"),
                archive_store.old_key,
            )
        ]
        == b"stale-owner"
    )


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
    container_id = "container-integrity"
    archive_sha256 = "a" * 64
    archive_key = f"image-builds/{build_id}/{image_id}.rclip"
    coordinates = ObjectStorage(
        isolated_services.context,
        object_client=isolated_services.object_storage.object_client,
        default_bucket=isolated_services.object_storage.default_bucket,
        allowed_buckets=("image-archives",),
    )
    reserved = coordinates.reserve_for_workspace(
        workspace_id=workspace_id,
        bucket="image-archives",
        key=archive_key,
        size=1024,
        sha256=archive_sha256,
        content_type="application/x-tar",
        metadata={
            "kind": "image-build-staging",
            "build_id": build_id,
            "container_id": container_id,
            "image_id": image_id,
        },
    )

    class IntegrityMismatchStore:
        def head(self, key: str, *, bucket: str | None = None) -> S3ObjectInfo:
            return S3ObjectInfo(
                bucket=bucket or "image-archives",
                key=key,
                size=head_size,
                metadata={"artifact-sha256": head_sha256},
            )

    result = ArchiveImageBuildPublicationPublisher(
        object_store=IntegrityMismatchStore(),
        bucket="image-archives",
        object_coordinates=coordinates,
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
                "container_id": container_id,
                "staging_archive_object_id": reserved.id,
                "staging_archive_object_key": reserved.key,
                "staging_archive_size_bytes": str(reserved.size),
                "staging_archive_sha256": reserved.sha256,
            },
        ),
    )

    assert result.status is ImageBuildPublicationPublishStatus.Error
    assert result.reason == "image build archive candidate failed size or sha256 verification"


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
    assert execution.session.container_id.startswith("build-")
    assert execution.events[0].message == "Building image...\n"

    cancelled = isolated_services.images.cancel_build_container(execution.session.container_id)
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
