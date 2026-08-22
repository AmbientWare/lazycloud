from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from foundation.process import ManagedCommandResult, ManagedCommandState
from shared.http.errors import ErrorResponse, HttpApiError
from shared.http.gateway import (
    DeployStubRequest,
    DeployStubResponse,
    GetOrCreateStubRequest,
    GetOrCreateStubResponse,
    GetUrlRequest,
    GetUrlResponse,
    ResolveDeploymentTargetRequest,
    ResolveDeploymentTargetResponse,
)
from storage_client.s3 import S3ObjectInfo


def http_api_error(detail: str, *, status_code: int = 400) -> HttpApiError:
    return HttpApiError(
        detail,
        status_code=status_code,
        error=ErrorResponse(detail=detail),
    )


@dataclass
class FakeDeploymentClient:
    stub_id: str = "stub-prepared"
    deployment_id: str | None = None
    version: int = 1
    invoke_url_template: str = "{external_url}/stub/{stub_id}"
    stub_id_from_type: bool = False
    fail_prepare: bool = False
    fail_deploy: bool = False
    fail_resolve: bool = False
    fail: bool = False
    stub_requests: list[GetOrCreateStubRequest] = field(default_factory=list)
    deploy_requests: list[DeployStubRequest] = field(default_factory=list)
    resolve_requests: list[ResolveDeploymentTargetRequest] = field(default_factory=list)

    @property
    def requests(self) -> list[GetOrCreateStubRequest]:
        return self.stub_requests

    def get_or_create_stub(self, request: GetOrCreateStubRequest) -> GetOrCreateStubResponse:
        self.stub_requests.append(request)
        if self.fail_prepare or self.fail:
            raise http_api_error("prepare failed")
        stub_id = f"stub-{request.stub_type}" if self.stub_id_from_type else self.stub_id
        return GetOrCreateStubResponse(stub_id=stub_id)

    def deploy_stub(self, request: DeployStubRequest) -> DeployStubResponse:
        self.deploy_requests.append(request)
        if self.fail_deploy:
            raise http_api_error("deploy failed")
        return DeployStubResponse(
            stub_id=request.stub_id,
            deployment_id=self.deployment_id or f"dep-{request.stub_id}",
            version=self.version,
            invoke_url=self.invoke_url_template.format(
                external_url=request.external_url,
                stub_id=request.stub_id,
            ),
        )

    def get_url(self, request: GetUrlRequest) -> GetUrlResponse:
        if self.fail:
            raise http_api_error("url failed")
        return GetUrlResponse(
            url=self.invoke_url_template.format(
                external_url=request.external_url,
                stub_id=request.stub_id,
            )
        )

    def resolve_deployment_target(
        self,
        request: ResolveDeploymentTargetRequest,
    ) -> ResolveDeploymentTargetResponse:
        self.resolve_requests.append(request)
        if self.fail_resolve or self.fail:
            raise http_api_error("target not found", status_code=404)
        return ResolveDeploymentTargetResponse(
            kind=request.kind,
            stub_id=self.stub_id,
            deployment_id=self.deployment_id or f"dep-{self.stub_id}",
            deployment_name=request.name,
            deployment_version=request.deployment_version or self.version,
            url=self.invoke_url_template.format(
                external_url=request.external_url,
                stub_id=self.stub_id,
            ),
        )


@dataclass(frozen=True)
class FakeUploadedObject:
    object_id: str


@dataclass
class FakeUploadClient:
    object_id: str
    uploads: list[dict[str, object]] = field(default_factory=list)

    def upload_bytes(
        self,
        data: bytes,
        *,
        name: str,
        bucket: str = "default",
        overwrite: bool = False,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        progress: Callable[[int], None] | None = None,
    ) -> FakeUploadedObject:
        if progress is not None:
            progress(len(data))
        self.uploads.append(
            {
                "data": data,
                "name": name,
                "bucket": bucket,
                "overwrite": overwrite,
                "content_type": content_type,
                "metadata": metadata or {},
            }
        )
        return FakeUploadedObject(object_id=self.object_id)


@dataclass
class FakeManagedCommand:
    args: list[str]

    def poll(self) -> ManagedCommandResult | None:
        return None

    def output(self) -> str:
        return ""

    def terminate(self, *, timeout_seconds: float = 5) -> ManagedCommandResult:
        del timeout_seconds
        return ManagedCommandResult(
            args=self.args,
            pid=1,
            exit_code=0,
            output="",
            state=ManagedCommandState.Terminated,
        )


@dataclass
class FakeObjectClient:
    objects: dict[tuple[str, str], bytes] = field(default_factory=dict)
    file_uploads: list[tuple[str, str, str, str, dict[str, str]]] = field(default_factory=list)
    file_downloads: list[tuple[str, str, str]] = field(default_factory=list)
    downloads: list[tuple[str, str, str]] = field(default_factory=list)

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        bucket: str | None = None,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> S3ObjectInfo:
        del content_type, metadata
        target_bucket = bucket or "default"
        self.objects[(target_bucket, key)] = data
        return S3ObjectInfo(bucket=target_bucket, key=key, size=len(data))

    def put_file(
        self,
        key: str,
        source: str | Path,
        *,
        bucket: str | None = None,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> S3ObjectInfo:
        target_bucket = bucket or "default"
        source_path = Path(source).expanduser().resolve()
        self.file_uploads.append(
            (str(source_path), target_bucket, key, content_type, metadata or {})
        )
        self.objects[(target_bucket, key)] = source_path.read_bytes()
        return S3ObjectInfo(
            bucket=target_bucket, key=key, size=len(self.objects[(target_bucket, key)])
        )

    def read_bytes(self, key: str, *, bucket: str | None = None) -> bytes:
        return self.objects[(bucket or "default", key)]

    def download_file(
        self,
        key: str,
        target: str | Path,
        *,
        bucket: str | None = None,
    ) -> S3ObjectInfo:
        target_bucket = bucket or "default"
        target_path = Path(target).expanduser().resolve()
        target_path.write_bytes(self.objects[(target_bucket, key)])
        self.file_downloads.append((str(target_path), target_bucket, key))
        self.downloads.append((target_bucket, key, str(target_path)))
        return S3ObjectInfo(bucket=target_bucket, key=key, size=target_path.stat().st_size)

    def head(self, key: str, *, bucket: str | None = None) -> S3ObjectInfo:
        target_bucket = bucket or "default"
        data = self.objects[(target_bucket, key)]
        return S3ObjectInfo(bucket=target_bucket, key=key, size=len(data))

    def exists(self, key: str, *, bucket: str | None = None) -> bool:
        return (bucket or "default", key) in self.objects

    def generate_presigned_get_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
    ) -> str:
        return f"memory://{bucket or 'default'}/{key}?expires={expires_seconds}"

    def generate_presigned_put_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
        content_length: int = 0,
        content_type: str = "application/octet-stream",
    ) -> str:
        del content_length, content_type
        return f"memory://{bucket or 'default'}/{key}?expires={expires_seconds}"

    def delete(self, key: str, *, bucket: str | None = None) -> None:
        self.objects.pop((bucket or "default", key), None)
