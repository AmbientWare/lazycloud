from __future__ import annotations

import hashlib
from base64 import b64encode
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from networking.internal_http import InternalHttpClient
from pydantic import JsonValue, TypeAdapter
from scheduler.state import SchedulerWorkerRequest
from worker.container_service.state import LocalWorkerContainerInstanceStore
from worker.events import ContainerRequestContext, WorkerBuildCancelRegistry
from worker.image_build_execution import (
    ImageBuildLog,
    RepositoryImageBuildContextLoader,
    RepositoryWorkerImageArchivePublisher,
    WorkerImageArchiveBuildResult,
    WorkerImageArchivePublishResult,
    WorkerImageBuildRequestPayload,
)
from worker.image_build_resources import ImageBuildResources
from worker.origin_access import (
    CacheOriginCredentialRequest,
    CacheOriginCredentials,
    ImageArchiveUploadCredentialRequest,
    ImageArchiveUploadCredentials,
)
from worker.repository_payloads import (
    ImageBuildPrivateInputs,
    ImageBuildRegistryAuth,
    PrepareImageBuildContextDownloadRequest,
    PrepareImageBuildContextDownloadResponse,
)

from worker import image_build_execution

type JsonObject = dict[str, JsonValue]

_JSON_OBJECT: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


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
    tmp_path: Path,
) -> None:
    private_value = "private-build-argument"
    builder = _RecordingImageBuilder(log_private_values=True)
    instances = LocalWorkerContainerInstanceStore()
    service = image_build_execution.WorkerImageBuildExecutionService(
        address_publisher=_RecordingAddressPublisher(),
        cancellations=WorkerBuildCancelRegistry(),
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
            "build_options": {},
            "credential_metadata": {
                "source": "ephemeral-private-inputs",
                "cache_key": "private-input-cache-key",
            },
        },
    )

    result = service.execute(request, resources=ImageBuildResources(tmp_path))

    assert not result.ok
    assert builder.build_args == [{"PRIVATE_TOKEN": private_value}]
    assert private_value not in result.model_dump_json()
    assert private_value not in instances.instances[request.container_id].model_dump_json()
    assert "<redacted>" in result.model_dump_json()


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
        "x-amz-checksum-sha256": b64encode(bytes.fromhex(digest)).decode(),
        "x-amz-meta-artifact-sha256": digest,
    }
    repository = _FakeArchiveUploadRepository(
        ImageArchiveUploadCredentials(
            bucket="image-archives",
            object_key="image-archives/image-1.rclip",
            archive_size_bytes=len(content),
            archive_sha256=digest,
            upload_url="https://objects.example.test/signed",
            upload_headers=signed_headers,
        )
    )
    uploads: list[dict[str, object]] = []

    def capture_upload(
        _http: InternalHttpClient,
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
        registry_ref="registry.example.com/workloads@sha256:" + "b" * 64,
        manifest_digest="sha256:" + "b" * 64,
        architecture="amd64",
        format_version=2,
        workspace_id="workspace-1",
        stub_id="stub-1",
    )

    request = repository.requests[0]
    assert request.archive_size_bytes == len(content)
    assert request.archive_sha256 == digest
    assert request.manifest_digest == "sha256:" + "b" * 64
    assert uploads[0]["headers"] == signed_headers
    assert result.object_key == "image-archives/image-1.rclip"
    assert result.size_bytes == len(content)
    assert result.sha256 == digest


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
    result = _context_loader(broker, _StreamedResponse(body)).extract_build_context(
        "context-object",
        tmp_path / "context",
        workspace_id="workspace-1",
        build_id="build-1",
        container_id="container-1",
        resources=ImageBuildResources(tmp_path),
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

    class _FailingHttp:
        """An internal client whose failure carries the signed URL."""

        def stream(self, *args: object, **kwargs: object) -> object:
            _ = args, kwargs
            raise RuntimeError(f"failed request with {sentinel}")

    result = RepositoryImageBuildContextLoader(
        broker,
        cast(InternalHttpClient, _FailingHttp()),
    ).extract_build_context(
        "context-object",
        tmp_path / "context",
        workspace_id="workspace-1",
        build_id="build-1",
        container_id="container-1",
        resources=ImageBuildResources(tmp_path),
    )

    assert not result.ok
    assert sentinel not in result.error_message
    assert "RuntimeError" in result.error_message


@dataclass(slots=True)
class _RecordingAddressPublisher:
    requests: list[ContainerRequestContext] = field(default_factory=list)

    def publish_worker_address(self, request: ContainerRequestContext) -> None:
        self.requests.append(request)


class _StreamedResponse:
    """The part of an httpx response the context download reads."""

    def __init__(self, body: bytes, *, content_length: int | None = None) -> None:
        self.status_code = 200
        self.body = body
        self.headers = {} if content_length is None else {"Content-Length": str(content_length)}

    def iter_bytes(self, size: int) -> Iterator[bytes]:
        for start in range(0, len(self.body), size):
            yield self.body[start : start + size]


class _StreamingHttp:
    """An internal client that serves one canned body."""

    def __init__(self, response: _StreamedResponse) -> None:
        self.response = response
        self.urls: list[str] = []

    @contextmanager
    def stream(self, method: str, url: str, **kwargs: object) -> Iterator[_StreamedResponse]:
        _ = kwargs
        assert method == "GET"
        self.urls.append(url)
        yield self.response


def _context_loader(
    broker: _BuildContextBroker,
    response: _StreamedResponse,
) -> RepositoryImageBuildContextLoader:
    return RepositoryImageBuildContextLoader(
        broker,
        cast(InternalHttpClient, _StreamingHttp(response)),
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
        resources: ImageBuildResources,
    ) -> WorkerImageArchiveBuildResult:
        self.requests.append(payload)
        self.registry_auths.append(registry_auth)
        self.build_args.append(build_args)
        _ = container_id
        private_values = list(build_args.values())
        if self.log_private_values and private_values:
            log(f"builder output: {private_values[0]}")
        return WorkerImageArchiveBuildResult(
            ok=False,
            image_id=payload.image_id,
            registry_ref="registry.example.com/workloads@sha256:" + "b" * 64,
            manifest_digest="sha256:" + "b" * 64,
            architecture="amd64",
            format_version=2,
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
        registry_ref: str = "",
        manifest_digest: str = "",
        architecture: str = "",
        format_version: int = 0,
        workspace_id: str = "",
        stub_id: str = "",
    ) -> WorkerImageArchivePublishResult:
        self.image_ids.append(image_id)
        _ = (
            archive_path,
            workspace_id,
            stub_id,
            build_id,
            container_id,
            upload_capability,
            registry_ref,
            manifest_digest,
            architecture,
            format_version,
        )
        return WorkerImageArchivePublishResult(
            ok=not self.error,
            image_id=image_id,
            object_key="image-archives/image-1.rclip" if not self.error else "",
            size_bytes=7 if not self.error else 0,
            sha256="a" * 64 if not self.error else "",
            error_message=self.error,
        )


@dataclass(slots=True)
class _UploadCredentialsResponse:
    credentials: ImageArchiveUploadCredentials | None


@dataclass(slots=True)
class _CacheOriginCredentialsResponse:
    credentials: CacheOriginCredentials | None


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

    def get_cache_origin_credentials(
        self,
        request: CacheOriginCredentialRequest,
    ) -> _CacheOriginCredentialsResponse:
        del request
        return _CacheOriginCredentialsResponse(credentials=CacheOriginCredentials())
