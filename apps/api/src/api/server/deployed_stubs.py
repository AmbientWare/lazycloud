from __future__ import annotations

from control.apps import AppReader
from control.service import ControlPlaneService, StubKind, StubRecord
from fastapi import HTTPException
from shared.deployments import DeploymentKind
from shared.errors import NotFoundError
from shared.identity import AuthTokenRecord

from api.server.services import ApiServices


def token_workspace(token: AuthTokenRecord | None) -> str:
    if token is None:
        raise HTTPException(status_code=401, detail="missing authorization principal")
    return token.workspace_id


def resolve_deployed_stub_id(
    control_plane: ControlPlaneService,
    apps: AppReader,
    stub_id: str,
    expected_kind: StubKind,
    *,
    public: bool,
    resource_name: str | None = None,
    workspace: str | None = None,
) -> StubRecord:
    name = resource_name or expected_kind.value
    try:
        stub = control_plane.get_stub(stub_id, workspace=workspace)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"{name} not found") from exc
    validate_deployed_stub(
        apps,
        stub,
        expected_kind,
        public=public,
        resource_name=name,
    )
    return stub


def resolve_deployed_stub(
    control_plane: ControlPlaneService,
    services: ApiServices,
    deployment_name: str,
    expected_kind: StubKind,
    *,
    version: int | None,
    workspace: str,
    resource_name: str | None = None,
) -> StubRecord:
    name = resource_name or expected_kind.value
    resource = services.deployment_resources.resolve_invoke_target(
        deployment_name,
        _deployment_kind(expected_kind),
        workspace=workspace,
        version=version,
    )
    validate_deployed_stub(
        services.apps,
        resource.stub,
        expected_kind,
        public=False,
        resource_name=name,
    )
    return resource.stub


def validate_deployed_stub(
    apps: AppReader,
    stub: StubRecord,
    expected_kind: StubKind,
    *,
    public: bool,
    resource_name: str,
) -> None:
    if stub.kind is not expected_kind:
        raise HTTPException(status_code=404, detail=f"{resource_name} not found")
    if public and not stub_is_public(apps, stub):
        raise HTTPException(status_code=404, detail=f"public {resource_name} not found")


def stub_is_public(apps: AppReader, stub: StubRecord) -> bool:
    if stub.public:
        return True
    if stub.app_id is None:
        return False
    try:
        app = apps.get(stub.app_id, workspace=stub.workspace_id)
        return app.workspace_id == stub.workspace_id and app.public
    except NotFoundError:
        return False


def _deployment_kind(stub_kind: StubKind) -> DeploymentKind:
    try:
        return DeploymentKind(stub_kind.value)
    except ValueError as exc:
        msg = f"deployment kind is not invokable: {stub_kind.value}"
        raise HTTPException(status_code=404, detail=msg) from exc
