from __future__ import annotations

from dataclasses import dataclass

from control.apps import AppService
from control.deployment_resources import DeploymentResourceService, client_manifest_resource
from control.deployments import DeploymentService
from control.stubs import StubService
from database.records.apps import StubKind
from pydantic import JsonValue
from shared.app_slug import app_slug_or_default, validate_app_slug
from shared.deployment_records import resolve_authorized, resolve_max_pending_tasks, resolve_retries
from shared.errors import (
    DomainError,
    InvalidInputError,
    NotFoundError,
)
from shared.http.client_manifests import (
    INVOKABLE_DEPLOYMENT_KINDS,
    ClientManifestRequest,
    ClientManifestResponse,
)
from shared.http.gateway import (
    DeployStubRequest,
    DeployStubResponse,
    GatewayUrlKind,
    GetOrCreateStubRequest,
    GetOrCreateStubResponse,
    GetUrlRequest,
    GetUrlResponse,
    ResolveDeploymentTargetRequest,
    ResolveDeploymentTargetResponse,
)

from gateway.stub_config import stub_config, stub_kind


@dataclass(frozen=True, slots=True)
class GatewayDeploymentService:
    stubs: StubService
    apps: AppService
    deployments: DeploymentService
    deployment_resources: DeploymentResourceService

    def get_or_create_stub(self, request: GetOrCreateStubRequest) -> GetOrCreateStubResponse:
        try:
            kind = stub_kind(request.stub_type)
            request = _request_with_workload_defaults(request, kind)
            config = stub_config(request)
            app_name = app_slug_or_default(request.app_name, default=request.name)
            metadata: dict[str, JsonValue] = {
                "object_id": request.object_id,
                "image_id": request.image_id,
                "force_create": request.force_create,
                "app": app_name,
            }
            config_metadata = dict(config.metadata)
            config_metadata["app"] = app_name
            config.metadata = config_metadata
            stub = self.stubs.create_stub(
                request.name,
                workspace=request.workspace,
                kind=kind,
                handler=request.handler or None,
                public=not request.authorized,
                config=config,
                metadata=metadata,
            )
        except (KeyError, ValueError) as exc:
            raise _domain_error(exc) from exc
        return GetOrCreateStubResponse(stub_id=stub.id)

    def deploy_stub(self, request: DeployStubRequest) -> DeployStubResponse:
        try:
            resource = self.deployments.deploy_prepared(
                request.stub_id, name=request.name, workspace=request.workspace
            )
            deployment = resource.deployment
            invoke_url = resource.invoke_url(request.external_url)
        except (KeyError, ValueError) as exc:
            raise _domain_error(exc) from exc
        return DeployStubResponse(
            stub_id=resource.stub.id,
            deployment_id=deployment.id,
            app_id=resource.app.id,
            version=deployment.version,
            invoke_url=invoke_url,
            name=deployment.name,
            role=deployment.spec.role,
            keep_warm_seconds=deployment.spec.resources.keep_warm,
            preemptible=deployment.spec.resources.preemptible,
        )

    def get_url(self, request: GetUrlRequest) -> GetUrlResponse:
        try:
            if request.url_type is GatewayUrlKind.Deployment and request.deployment_id:
                resource = self.deployment_resources.resolve(
                    request.deployment_id,
                    workspace=request.workspace,
                )
                url = resource.invoke_url(request.external_url, port=request.port)
            else:
                url = self.stubs.stub_url(
                    request.stub_id,
                    workspace=request.workspace,
                    deployment_id=request.deployment_id or None,
                    external_url=request.external_url,
                    port=request.port,
                    container_id=request.container_id,
                ).url
        except (KeyError, ValueError) as exc:
            raise _domain_error(exc) from exc
        return GetUrlResponse(url=url)

    def resolve_deployment_target(
        self,
        request: ResolveDeploymentTargetRequest,
    ) -> ResolveDeploymentTargetResponse:
        try:
            app_id = None
            if request.app:
                app = self.apps.get(
                    validate_app_slug(request.app),
                    workspace=request.workspace,
                )
                app_id = app.id
            result = self.deployment_resources.resolve_target(
                request.name,
                request.kind,
                workspace=request.workspace,
                version=request.deployment_version,
                app_id=app_id,
            )
        except (KeyError, ValueError) as exc:
            raise _domain_error(exc) from exc
        return ResolveDeploymentTargetResponse(
            kind=request.kind,
            stub_id=result.stub.id,
            deployment_id=result.deployment.id,
            deployment_name=result.deployment.name,
            deployment_version=result.deployment.version,
            url=result.invoke_url(
                request.external_url, pin_version=request.deployment_version is not None
            ),
        )

    def client_manifest(self, request: ClientManifestRequest) -> ClientManifestResponse:
        try:
            app = self.apps.get(request.app, workspace=request.workspace)
            deployed_resources = self.deployment_resources.list(
                workspace=request.workspace,
                app=app.name,
                kinds=INVOKABLE_DEPLOYMENT_KINDS,
                active=True,
                latest_per_resource=True,
            )
            resources = [
                client_manifest_resource(
                    deployed_resource,
                    external_url=request.external_url,
                )
                for deployed_resource in deployed_resources
            ]
        except (KeyError, ValueError) as exc:
            raise _domain_error(exc) from exc
        return ClientManifestResponse(
            app=app.name,
            workspace=request.workspace,
            resources=resources,
        )


def _request_with_workload_defaults(
    request: GetOrCreateStubRequest,
    kind: StubKind,
) -> GetOrCreateStubRequest:
    fields_set = request.model_fields_set
    updates: dict[str, bool | int] = {}
    if "authorized" not in fields_set:
        updates["authorized"] = resolve_authorized(kind.value, None)
    if "max_pending_tasks" not in fields_set:
        max_pending_tasks = resolve_max_pending_tasks(kind.value, None)
        if max_pending_tasks is not None:
            updates["max_pending_tasks"] = max_pending_tasks
    if "retries" not in fields_set and request.retry_policy is None:
        updates["retries"] = resolve_retries(kind.value, None)
    return request.model_copy(update=updates) if updates else request


def _domain_error(exc: KeyError | ValueError) -> DomainError:
    if isinstance(exc, KeyError):
        return NotFoundError(str(exc).strip("'\""))
    return InvalidInputError(str(exc))
