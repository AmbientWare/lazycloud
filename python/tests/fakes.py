from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from lazycloud.source_sync import (
    SOURCE_PACKAGE_BUCKET,
    SOURCE_PACKAGE_CONTENT_TYPE,
    SourcePackageArchive,
)
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
from shared.http.objects import PutObjectResponse


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
    def upload_source(
        self,
        archive: SourcePackageArchive,
        *,
        name: str,
        progress: Callable[[int], None] | None = None,
    ) -> PutObjectResponse:
        result = self.upload_bytes(
            archive.path.read_bytes(),
            name=name,
            bucket=SOURCE_PACKAGE_BUCKET,
            content_type=SOURCE_PACKAGE_CONTENT_TYPE,
            progress=progress,
        )
        return PutObjectResponse(object_id=result.object_id)

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
