from __future__ import annotations

from dataclasses import dataclass

from control.apps import AppService
from control.deployment_resources import DeploymentResourceService, client_manifest_resource
from control.deployments import DeploymentService
from control.stubs import StubService
from database.records.apps import StubKind
from operations.management import ManagementService
from pydantic import JsonValue
from shared.app_slug import app_slug_or_default, validate_app_slug
from shared.deployment_records import resolve_authorized, resolve_max_pending_tasks, resolve_retries
from shared.deployments import DeploymentKind
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

from gateway.stub_config import deployment_spec_from_stub, stub_config, stub_kind


@dataclass(frozen=True, slots=True)
class GatewayDeploymentService:
    stubs: StubService
    apps: AppService
    deployments: DeploymentService
    deployment_resources: DeploymentResourceService
    management: ManagementService

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
            stub = self.stubs.get_stub(request.stub_id, workspace=request.workspace)
            workspace = request.workspace or stub.workspace_id
            deployment = self.deployments.deploy(
                deployment_spec_from_stub(stub, name=request.name or stub.name),
                workspace=workspace,
            )
            resource = self.deployment_resources.get_by_deployment_id(
                deployment.id,
                workspace=workspace,
            )
            if resource is None:
                msg = f"deployment resource not found after deploy: {deployment.id}"
                raise ValueError(msg)
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
                url = self.management.deployment_url(
                    request.deployment_id,
                    workspace=request.workspace,
                    external_url=request.external_url,
                    port=request.port,
                ).url
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
            stub_type = _deployment_kind_to_stub_kind(request.kind)
            app_id = None
            if request.app:
                app = self.apps.get(
                    validate_app_slug(request.app),
                    workspace=request.workspace,
                )
                app_id = app.id
            result = self.management.deployment_url_by_name(
                request.workspace,
                stub_type,
                request.name,
                request.deployment_version,
                app_id=app_id,
                external_url=request.external_url,
            )
            if result.stub is None:
                raise NotFoundError(f"deployment target has no stub: {request.name}")
        except (KeyError, ValueError) as exc:
            raise _domain_error(exc) from exc
        return ResolveDeploymentTargetResponse(
            kind=request.kind,
            stub_id=result.stub.id,
            deployment_id=result.deployment.id,
            deployment_name=result.deployment.name,
            deployment_version=result.deployment.version,
            url=result.url,
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


def _deployment_kind_to_stub_kind(kind: DeploymentKind) -> StubKind:
    try:
        return StubKind(kind.value)
    except ValueError as exc:
        msg = f"deployment kind is not invokable: {kind}"
        raise ValueError(msg) from exc


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
