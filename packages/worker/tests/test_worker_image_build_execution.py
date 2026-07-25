from __future__ import annotations

import hashlib
import io
from base64 import b64encode
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter
from scheduler.state import SchedulerWorkerRequest
from worker.container_artifacts import ContainerImageArchiveResult
from worker.container_client.models import ContainerStatusRequest
from worker.container_service.service import WorkerContainerService
from worker.container_service.state import LocalWorkerContainerInstanceStore
from worker.events import ContainerRequestContext
from worker.image_build_architecture import ImageBuildArchitectureRuntime
from worker.image_build_execution import (
    BuildahWorkerImageBuilder,
    ImageBuildLog,
    RepositoryImageBuildContextLoader,
    RepositoryWorkerImageArchivePublisher,
    WorkerImageArchiveBuildResult,
    WorkerImageArchivePublishResult,
    WorkerImageBuildRequestPayload,
)
from worker.image_build_runtime_credentials import RemoteImageBuildCredentialLoader
from worker.image_build_scratch import ImageBuildScratchLease, ImageBuildScratchManager
from worker.image_lifecycle import BuildahDirectoryPlan, BuildahStorageDriver
from worker.origin_access import (
    ImageArchiveUploadCredentialRequest,
    ImageArchiveUploadCredentials,
)
from worker.repository_payloads import (
    GetImageBuildCredentialsRequest,
    GetImageBuildCredentialsResponse,
    ImageBuildPrivateInputs,
    ImageBuildRegistryAuth,
    PrepareImageBuildContextDownloadRequest,
    PrepareImageBuildContextDownloadResponse,
)

from worker import image_build_execution

type JsonObject = dict[str, JsonValue]

_JSON_OBJECT: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


def _buildah_path(binary: str) -> str | None:
    return "/usr/bin/buildah" if binary == "buildah" else None


def test_remote_image_build_credential_loader_consumes_once_without_polling() -> None:
    requests: list[GetImageBuildCredentialsRequest] = []

    class MissingCredentialRepository:
        def get_image_build_credentials(
            self,
            request: GetImageBuildCredentialsRequest,
        ) -> GetImageBuildCredentialsResponse:
            requests.append(request)
            return GetImageBuildCredentialsResponse()

    loaded = RemoteImageBuildCredentialLoader(MissingCredentialRepository()).load(
        workspace_id="workspace-1",
        build_id="build-1",
        container_id="container-1",
        registry="registry.example.com",
        cache_key="one-use-capability",
    )

    assert loaded.empty
    assert requests == [
        GetImageBuildCredentialsRequest(
            workspace_id="workspace-1",
            build_id="build-1",
            container_id="container-1",
            registry="registry.example.com",
            cache_key="one-use-capability",
        )
    ]


def test_image_build_worker_rejects_managed_package_version_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_digest = "a" * 64
    worker_digest = "b" * 64
    builder = _RecordingImageBuilder()
    publisher = _RecordingImagePublisher()
    instances = LocalWorkerContainerInstanceStore()
    service = image_build_execution.WorkerImageBuildExecutionService(
        address_publisher=_RecordingAddressPublisher(),
        instances=instances,
        builder=builder,
        publisher=publisher,
    )
    request = SchedulerWorkerRequest(
        workspace_id="workspace-1",
        stub_id="image-build",
        container_id="build-container-1",
        payload={
            "kind": "image-build",
            "workspace_id": "spoofed-workspace",
            "build_id": "build-1",
            "image_id": "image-1",
            "build_options": {"managed_package_digest": expected_digest},
        },
    )
    monkeypatch.setattr(
        image_build_execution, "managed_package_source_digest", lambda: worker_digest
    )

    result = service.execute(request)

    assert not result.ok
    assert result.error_message == (
        "RuntimeError: managed package digest mismatch: "
        f"control plane expected {expected_digest}, image worker resolved {worker_digest}"
    )
    assert builder.requests == []
    assert publisher.image_ids == []
    assert instances.instances[request.container_id].status == "failed"
    status = WorkerContainerService(instances=instances).container_status(
        ContainerStatusRequest(container_id=request.container_id)
    )
    assert status.error_msg == result.error_message


def test_image_build_worker_consumes_bound_source_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_digest = "a" * 64
    registry_auth = ImageBuildRegistryAuth(
        registry="registry.example.com",
        auth=b64encode(b"builder:private-secret").decode("ascii"),
    )
    builder = _RecordingImageBuilder()
    loader = _RecordingCredentialLoader(registry_auth)
    service = image_build_execution.WorkerImageBuildExecutionService(
        address_publisher=_RecordingAddressPublisher(),
        instances=LocalWorkerContainerInstanceStore(),
        builder=builder,
        publisher=_RecordingImagePublisher(),
        credential_loader=loader,
    )
    request = SchedulerWorkerRequest(
        workspace_id="workspace-1",
        stub_id="image-build",
        container_id="build-container-1",
        payload={
            "kind": "image-build",
            "workspace_id": "spoofed-workspace",
            "build_id": "build-1",
            "image_id": "image-1",
            "build_options": {
                "managed_package_digest": package_digest,
                "source_image": "registry.example.com/team/base:latest",
            },
            "credential_metadata": {
                "registry": "registry.example.com",
                "source": "ephemeral-private-inputs",
                "cache_key": "credential-cache-key",
            },
        },
    )
    monkeypatch.setattr(
        image_build_execution, "managed_package_source_digest", lambda: package_digest
    )

    result = service.execute(request)

    assert result.ok
    assert builder.requests[0].workspace_id == "workspace-1"
    assert loader.calls == [
        {
            "workspace_id": "workspace-1",
            "build_id": "build-1",
            "container_id": "build-container-1",
            "registry": "registry.example.com",
            "cache_key": "credential-cache-key",
        }
    ]
    assert builder.registry_auths == [registry_auth]
    assert builder.build_args == [{}]


def test_worker_registry_authfile_is_private_and_contains_docker_auth(tmp_path: Path) -> None:
    registry_auth = ImageBuildRegistryAuth(
        registry="registry.example.com",
        auth=b64encode(b"builder:private-secret").decode("ascii"),
    )

    authfile = image_build_execution._write_registry_auth_file(
        tmp_path,
        registry_auth,
    )

    assert authfile is not None
    assert authfile.stat().st_mode & 0o777 == 0o600
    payload = _JSON_OBJECT.validate_json(authfile.read_bytes())
    auths = payload.get("auths")
    assert isinstance(auths, dict)
    registry_auth = auths.get("registry.example.com")
    assert isinstance(registry_auth, dict)
    assert isinstance(registry_auth.get("auth"), str)


def test_worker_private_build_args_are_redacted_from_results_and_instance_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_digest = "a" * 64
    private_value = "private-build-argument"
    builder = _RecordingImageBuilder(log_private_values=True)
    instances = LocalWorkerContainerInstanceStore()
    service = image_build_execution.WorkerImageBuildExecutionService(
        address_publisher=_RecordingAddressPublisher(),
        instances=instances,
        builder=builder,
        publisher=_RecordingImagePublisher(),
        credential_loader=_RecordingCredentialLoader(
            build_args={"PRIVATE_TOKEN": private_value},
        ),
    )
    request = SchedulerWorkerRequest(
        workspace_id="workspace-1",
        stub_id="image-build",
        container_id="build-container-1",
        payload={
            "kind": "image-build",
            "build_id": "build-1",
            "image_id": "image-1",
            "build_options": {"managed_package_digest": package_digest},
            "credential_metadata": {
                "source": "ephemeral-private-inputs",
                "cache_key": "private-input-cache-key",
            },
        },
    )
    monkeypatch.setattr(
        image_build_execution, "managed_package_source_digest", lambda: package_digest
    )

    result = service.execute(request)

    assert result.ok
    assert builder.build_args == [{"PRIVATE_TOKEN": private_value}]
    assert private_value not in result.model_dump_json()
    assert private_value not in instances.instances[request.container_id].model_dump_json()
    assert "<redacted>" in result.model_dump_json()
    status = WorkerContainerService(instances=instances).container_status(
        ContainerStatusRequest(container_id=request.container_id)
    )
    assert status.build_archive_object_id == "object-1"
    assert status.build_archive_object_key == "image-builds/build-1/image-1.rclip"
    assert status.build_archive_size_bytes == 7
    assert status.build_archive_sha256 == "a" * 64


def test_worker_publication_failure_redacts_private_logs_and_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_digest = "a" * 64
    private_value = "private-publication-argument"
    instances = LocalWorkerContainerInstanceStore()
    service = image_build_execution.WorkerImageBuildExecutionService(
        address_publisher=_RecordingAddressPublisher(),
        instances=instances,
        builder=_RecordingImageBuilder(log_private_values=True),
        publisher=_RecordingImagePublisher(error=f"upload rejected: {private_value}"),
        credential_loader=_RecordingCredentialLoader(
            build_args={"PRIVATE_TOKEN": private_value},
        ),
    )
    request = SchedulerWorkerRequest(
        workspace_id="workspace-1",
        stub_id="image-build",
        container_id="build-container-1",
        payload={
            "kind": "image-build",
            "build_id": "build-1",
            "image_id": "image-1",
            "build_options": {"managed_package_digest": package_digest},
            "credential_metadata": {
                "source": "ephemeral-private-inputs",
                "cache_key": "private-input-cache-key",
            },
        },
    )
    monkeypatch.setattr(
        image_build_execution, "managed_package_source_digest", lambda: package_digest
    )

    result = service.execute(request)

    assert not result.ok
    assert private_value not in result.model_dump_json()
    assert private_value not in instances.instances[request.container_id].model_dump_json()
    assert "upload rejected: <redacted>" in result.error_message
    assert "archive output: <redacted>" in result.logs


def test_repository_archive_publisher_requests_and_propagates_exact_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"archive identity"
    archive = tmp_path / "image.rclip"
    archive.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    signed_headers = {
        "content-type": "application/x-tar",
        "content-length": str(len(content)),
        "x-amz-meta-artifact-sha256": digest,
    }
    repository = _FakeArchiveUploadRepository(
        ImageArchiveUploadCredentials(
            archive_object_id="object-1",
            bucket="image-archives",
            object_key="image-builds/build-1/image-1.rclip",
            upload_url="https://objects.example.test/signed",
            upload_headers=signed_headers,
        )
    )
    uploads: list[dict[str, object]] = []

    def capture_upload(
        url: str,
        path: Path,
        *,
        headers: dict[str, str],
        archive_size_bytes: int,
        archive_sha256: str,
        content_type: str,
        timeout_seconds: float,
    ) -> None:
        uploads.append(
            {
                "url": url,
                "path": path,
                "headers": headers,
                "archive_size_bytes": archive_size_bytes,
                "archive_sha256": archive_sha256,
                "content_type": content_type,
                "timeout_seconds": timeout_seconds,
            }
        )

    monkeypatch.setattr(image_build_execution, "upload_image_archive", capture_upload)

    result = RepositoryWorkerImageArchivePublisher(repository).publish_image_archive(
        image_id="image-1",
        build_id="build-1",
        container_id="container-1",
        upload_capability="a" * 32,
        archive_path=archive,
        workspace_id="workspace-1",
        stub_id="stub-1",
    )

    request = repository.requests[0]
    assert request.archive_size_bytes == len(content)
    assert request.archive_sha256 == digest
    assert uploads[0]["headers"] == signed_headers
    assert result.archive_object_id == "object-1"
    assert result.object_key == "image-builds/build-1/image-1.rclip"
    assert result.size_bytes == len(content)
    assert result.sha256 == digest


def test_buildah_failure_cleans_every_resource_in_its_isolated_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_buildah(
        _self: BuildahWorkerImageBuilder,
        args: Sequence[str],
        *,
        directories: BuildahDirectoryPlan,
        driver: BuildahStorageDriver,
        env: dict[str, str],
        cwd: Path,
        log: ImageBuildLog,
        capture_stdout: bool = False,
        scratch: ImageBuildScratchLease | None = None,
    ) -> str:
        del directories, driver, env, cwd, log, capture_stdout, scratch
        assert args[0] == "bud"
        raise RuntimeError("deliberate build failure")

    monkeypatch.setattr(
        image_build_execution.shutil,
        "which",
        _buildah_path,
    )
    monkeypatch.setattr(
        image_build_execution.BuildahWorkerImageBuilder,
        "_run_buildah",
        fail_buildah,
    )
    builder = image_build_execution.BuildahWorkerImageBuilder(
        archiver=_SuccessfulImageArchiver(tmp_path / "image.rclip"),
        scratch=ImageBuildScratchManager(
            root=tmp_path / "build-root",
            worker_id="worker-1",
            max_bytes=32 * 1024 * 1024,
            per_build_max_bytes=16 * 1024 * 1024,
            minimum_free_bytes=0,
            buildah_binary="cleanup-buildah-missing",
        ),
        architecture_preparer=ImageBuildArchitectureRuntime(host_machine=lambda: "x86_64"),
        storage_driver=image_build_execution.BuildahStorageDriver.Vfs,
        fallback_storage_driver=image_build_execution.BuildahStorageDriver.Vfs,
    )
    payload = WorkerImageBuildRequestPayload.model_validate(
        {
            "build_id": "build-1",
            "image_id": "image-1",
            "build_options": {
                "dockerfile": "FROM scratch\nRUN false\n",
                "managed_package_digest": "a" * 64,
            },
        }
    )

    result = builder.build_image_archive(
        payload,
        container_id="container-1",
        log=lambda _message: None,
    )

    assert not result.ok
    assert not list((tmp_path / "build-root").glob("image-build-*"))


def test_buildah_cleanup_failure_still_releases_isolated_scratch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_buildah(
        _self: BuildahWorkerImageBuilder,
        _args: Sequence[str],
        *,
        directories: BuildahDirectoryPlan,
        driver: BuildahStorageDriver,
        env: dict[str, str],
        cwd: Path,
        log: ImageBuildLog,
        capture_stdout: bool = False,
        scratch: ImageBuildScratchLease | None = None,
    ) -> str:
        del directories, driver, env, cwd, log, capture_stdout, scratch
        raise RuntimeError("deliberate build failure")

    def fail_cleanup(
        _self: ImageBuildScratchManager,
        _root: Path,
        *,
        driver: BuildahStorageDriver,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        del driver, env
        raise RuntimeError("deliberate cleanup failure")

    monkeypatch.setattr(
        image_build_execution.shutil,
        "which",
        _buildah_path,
    )
    monkeypatch.setattr(
        image_build_execution.BuildahWorkerImageBuilder,
        "_run_buildah",
        fail_buildah,
    )
    monkeypatch.setattr(ImageBuildScratchManager, "cleanup_store", fail_cleanup)
    builder = image_build_execution.BuildahWorkerImageBuilder(
        archiver=_SuccessfulImageArchiver(tmp_path / "image.rclip"),
        scratch=ImageBuildScratchManager(
            root=tmp_path / "build-root",
            worker_id="worker-1",
            max_bytes=32 * 1024 * 1024,
            per_build_max_bytes=16 * 1024 * 1024,
            minimum_free_bytes=0,
        ),
        architecture_preparer=ImageBuildArchitectureRuntime(host_machine=lambda: "x86_64"),
        storage_driver=image_build_execution.BuildahStorageDriver.Vfs,
        fallback_storage_driver=image_build_execution.BuildahStorageDriver.Vfs,
    )
    payload = WorkerImageBuildRequestPayload.model_validate(
        {
            "build_id": "build-1",
            "image_id": "image-1",
            "build_options": {
                "dockerfile": "FROM scratch\nRUN false\n",
                "managed_package_digest": "a" * 64,
            },
        }
    )

    result = builder.build_image_archive(
        payload,
        container_id="container-1",
        log=lambda _message: None,
    )

    assert not result.ok
    assert "deliberate cleanup failure" in result.error_message
    assert not list((tmp_path / "build-root").glob("image-build-*"))


@pytest.mark.parametrize(
    ("content_length_delta", "sha256", "message"),
    [
        (1, None, "body is incomplete"),
        (0, "0" * 64, "SHA-256 does not match"),
    ],
)
def test_repository_build_context_loader_rejects_incomplete_or_modified_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content_length_delta: int,
    sha256: str | None,
    message: str,
) -> None:
    body = b"not-a-complete-context"
    broker = _BuildContextBroker(
        PrepareImageBuildContextDownloadResponse(
            object_id="context-object",
            download_url="https://objects.example.test/context.zip?signature=private",
            content_length=len(body) + content_length_delta,
            sha256=sha256 or hashlib.sha256(body).hexdigest(),
            expires_at=image_build_execution.utc_now() + timedelta(minutes=5),
        )
    )
    _install_context_download(monkeypatch, _HttpResponse(body))

    result = RepositoryImageBuildContextLoader(broker).extract_build_context(
        "context-object",
        tmp_path / "context",
        workspace_id="workspace-1",
        build_id="build-1",
        container_id="container-1",
    )

    assert not result.ok
    assert message in result.error_message
    assert "signature=private" not in result.error_message
    assert not (tmp_path / "context").exists()


def test_repository_build_context_error_never_discloses_capability_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "never-log-this-context-signature"
    broker = _BuildContextBroker(
        PrepareImageBuildContextDownloadResponse(
            object_id="context-object",
            download_url=(f"https://objects.example.test/context.zip?X-Amz-Signature={sentinel}"),
            content_length=4,
            sha256=hashlib.sha256(b"data").hexdigest(),
            expires_at=image_build_execution.utc_now() + timedelta(minutes=5),
        )
    )

    class FailingConnection:
        def __init__(
            self,
            host: str,
            port: int | None = None,
            timeout: float | None = None,
        ) -> None:
            _ = host, port, timeout

        def request(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs
            raise RuntimeError(f"failed request with {sentinel}")

        def close(self) -> None:
            return

    monkeypatch.setattr(
        image_build_execution.http.client,
        "HTTPSConnection",
        FailingConnection,
    )

    result = RepositoryImageBuildContextLoader(broker).extract_build_context(
        "context-object",
        tmp_path / "context",
        workspace_id="workspace-1",
        build_id="build-1",
        container_id="container-1",
    )

    assert not result.ok
    assert sentinel not in result.error_message
    assert "RuntimeError" in result.error_message


@dataclass(slots=True)
class _RecordingAddressPublisher:
    requests: list[ContainerRequestContext] = field(default_factory=list)

    def publish_worker_address(self, request: ContainerRequestContext) -> None:
        self.requests.append(request)


class _HttpResponse(io.BytesIO):
    status = 200

    def __init__(self, body: bytes, *, content_length: int | None = None) -> None:
        super().__init__(body)
        self.headers = {} if content_length is None else {"Content-Length": str(content_length)}

    def getheader(self, name: str) -> str | None:
        return self.headers.get(name)


def _install_context_download(
    monkeypatch: pytest.MonkeyPatch,
    response: _HttpResponse,
) -> None:
    class ContextDownloadConnection:
        def __init__(
            self,
            host: str,
            port: int | None = None,
            timeout: float | None = None,
        ) -> None:
            self.host = host
            self.port = port
            self.timeout = timeout

        def request(self, method: str, target: str) -> None:
            assert method == "GET"
            assert target == "/context.zip?signature=private"

        def getresponse(self) -> _HttpResponse:
            return response

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        image_build_execution.http.client,
        "HTTPSConnection",
        ContextDownloadConnection,
    )


@dataclass(slots=True)
class _BuildContextBroker:
    response: PrepareImageBuildContextDownloadResponse
    requests: list[PrepareImageBuildContextDownloadRequest] = field(default_factory=list)

    def prepare_image_build_context_download(
        self,
        request: PrepareImageBuildContextDownloadRequest,
    ) -> PrepareImageBuildContextDownloadResponse:
        self.requests.append(request)
        return self.response


@dataclass(slots=True)
class _RecordingImageBuilder:
    requests: list[WorkerImageBuildRequestPayload] = field(default_factory=list)
    registry_auths: list[ImageBuildRegistryAuth | None] = field(default_factory=list)
    build_args: list[dict[str, str]] = field(default_factory=list)
    log_private_values: bool = False

    def build_image_archive(
        self,
        payload: WorkerImageBuildRequestPayload,
        *,
        container_id: str,
        registry_auth: ImageBuildRegistryAuth | None,
        build_args: dict[str, str],
        log: ImageBuildLog,
    ) -> WorkerImageArchiveBuildResult:
        self.requests.append(payload)
        self.registry_auths.append(registry_auth)
        self.build_args.append(build_args)
        _ = container_id
        private_values = list(build_args.values())
        if self.log_private_values and private_values:
            log(f"builder output: {private_values[0]}")
        return WorkerImageArchiveBuildResult(
            ok=True,
            image_id=payload.image_id,
            logs=[f"archive output: {private_values[0]}"]
            if self.log_private_values and private_values
            else [],
        )


@dataclass(slots=True)
class _RecordingCredentialLoader:
    registry_auth: ImageBuildRegistryAuth | None = None
    build_args: dict[str, str] = field(default_factory=dict)
    calls: list[dict[str, str]] = field(default_factory=list)

    def load(
        self,
        *,
        workspace_id: str,
        build_id: str,
        container_id: str,
        registry: str,
        cache_key: str,
    ) -> ImageBuildPrivateInputs:
        self.calls.append(
            {
                "workspace_id": workspace_id,
                "build_id": build_id,
                "container_id": container_id,
                "registry": registry,
                "cache_key": cache_key,
            }
        )
        return ImageBuildPrivateInputs(
            registry_auth=self.registry_auth,
            build_args=self.build_args,
        )


@dataclass(slots=True)
class _SuccessfulImageArchiver:
    archive_path: Path

    def archive_image(
        self,
        source_path: Path,
        image_id: str,
        progress: Callable[[int], None],
    ) -> ContainerImageArchiveResult:
        del source_path, image_id, progress
        self.archive_path.write_bytes(b"archive")
        return ContainerImageArchiveResult(
            success=True,
            archive_path=str(self.archive_path),
        )


@dataclass(slots=True)
class _RecordingImagePublisher:
    image_ids: list[str] = field(default_factory=list)
    error: str = ""

    def publish_image_archive(
        self,
        *,
        image_id: str,
        build_id: str = "",
        container_id: str = "",
        upload_capability: str = "",
        archive_path: Path,
        workspace_id: str = "",
        stub_id: str = "",
    ) -> WorkerImageArchivePublishResult:
        self.image_ids.append(image_id)
        _ = archive_path, workspace_id, stub_id, build_id, container_id, upload_capability
        return WorkerImageArchivePublishResult(
            ok=not self.error,
            image_id=image_id,
            archive_object_id="object-1" if not self.error else "",
            object_key="image-builds/build-1/image-1.rclip" if not self.error else "",
            size_bytes=7 if not self.error else 0,
            sha256="a" * 64 if not self.error else "",
            error_message=self.error,
        )


@dataclass(slots=True)
class _UploadCredentialsResponse:
    credentials: ImageArchiveUploadCredentials | None


class _FakeArchiveUploadRepository:
    def __init__(self, credentials: ImageArchiveUploadCredentials) -> None:
        self.credentials = credentials
        self.requests: list[ImageArchiveUploadCredentialRequest] = []

    def get_image_archive_upload_credentials(
        self,
        request: ImageArchiveUploadCredentialRequest,
    ) -> _UploadCredentialsResponse:
        self.requests.append(request)
        return _UploadCredentialsResponse(credentials=self.credentials)


@dataclass(slots=True)
class _UploadResponse:
    status: int
    body: bytes

    def read(self) -> bytes:
        return self.body
