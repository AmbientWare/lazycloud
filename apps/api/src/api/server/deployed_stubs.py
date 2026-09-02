from __future__ import annotations

from collections.abc import Callable

from control.service import ControlPlaneService, StubKind, StubRecord
from database.types import DatabaseSession
from fastapi import HTTPException
from shared.deployments import DeploymentKind
from shared.errors import NotFoundError

from api.server.services import ApiServices


def resolve_deployed_stub_id(
    control_plane: ControlPlaneService,
    services: ApiServices,
    stub_id: str,
    expected_kind: StubKind,
    *,
    public: bool,
    resource_name: str | None = None,
    workspace: str | None = None,
) -> StubRecord:
    resolve = _stub_by_id(
        control_plane,
        services,
        stub_id,
        expected_kind,
        public=public,
        resource_name=resource_name,
        workspace=workspace,
    )
    with services.context.database.session() as session:
        return resolve(session)


async def resolve_deployed_stub_id_async(
    control_plane: ControlPlaneService,
    services: ApiServices,
    stub_id: str,
    expected_kind: StubKind,
    *,
    public: bool,
    resource_name: str | None = None,
    workspace: str | None = None,
) -> StubRecord:
    resolve = _stub_by_id(
        control_plane,
        services,
        stub_id,
        expected_kind,
        public=public,
        resource_name=resource_name,
        workspace=workspace,
    )
    return await services.require_async_io().database.run_transaction(resolve)


def resolve_deployed_stub(
    services: ApiServices,
    deployment_name: str,
    expected_kind: StubKind,
    *,
    version: int | None,
    workspace: str,
    resource_name: str | None = None,
) -> StubRecord:
    resolve = _stub_by_deployment(
        services,
        deployment_name,
        expected_kind,
        version=version,
        workspace=workspace,
        resource_name=resource_name,
    )
    with services.context.database.session() as session:
        return resolve(session)


async def resolve_deployed_stub_async(
    services: ApiServices,
    deployment_name: str,
    expected_kind: StubKind,
    *,
    version: int | None,
    workspace: str,
    resource_name: str | None = None,
) -> StubRecord:
    resolve = _stub_by_deployment(
        services,
        deployment_name,
        expected_kind,
        version=version,
        workspace=workspace,
        resource_name=resource_name,
    )
    return await services.require_async_io().database.run_transaction(resolve)


def _stub_by_id(
    control_plane: ControlPlaneService,
    services: ApiServices,
    stub_id: str,
    expected_kind: StubKind,
    *,
    public: bool,
    resource_name: str | None,
    workspace: str | None,
) -> Callable[[DatabaseSession], StubRecord]:
    name = resource_name or expected_kind.value

    def resolve(session: DatabaseSession) -> StubRecord:
        try:
            stub = control_plane.get_stub_in_session(session, stub_id, workspace=workspace)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"{name} not found") from exc
        _validate(services, session, stub, expected_kind, public=public, resource_name=name)
        return stub

    return resolve


def _stub_by_deployment(
    services: ApiServices,
    deployment_name: str,
    expected_kind: StubKind,
    *,
    version: int | None,
    workspace: str,
    resource_name: str | None,
) -> Callable[[DatabaseSession], StubRecord]:
    name = resource_name or expected_kind.value

    def resolve(session: DatabaseSession) -> StubRecord:
        resource = services.deployment_resources.resolve_invoke_target_in_session(
            session,
            deployment_name,
            _deployment_kind(expected_kind),
            workspace=workspace,
            version=version,
        )
        _validate(services, session, resource.stub, expected_kind, public=False, resource_name=name)
        return resource.stub

    return resolve


def _validate(
    services: ApiServices,
    session: DatabaseSession,
    stub: StubRecord,
    expected_kind: StubKind,
    *,
    public: bool,
    resource_name: str,
) -> None:
    if stub.kind is not expected_kind:
        raise HTTPException(status_code=404, detail=f"{resource_name} not found")
    if public and not stub_is_public_in_session(services, session, stub):
        raise HTTPException(status_code=404, detail=f"public {resource_name} not found")


def stub_is_public_in_session(
    services: ApiServices,
    session: DatabaseSession,
    stub: StubRecord,
) -> bool:
    if stub.public:
        return True
    if stub.app_id is None:
        return False
    try:
        app = services.apps.get_in_session(session, stub.app_id, workspace=stub.workspace_id)
    except NotFoundError:
        return False
    return app.workspace_id == stub.workspace_id and app.public


def _deployment_kind(stub_kind: StubKind) -> DeploymentKind:
    try:
        return DeploymentKind(stub_kind.value)
    except ValueError as exc:
        msg = f"deployment kind is not invokable: {stub_kind.value}"
        raise HTTPException(status_code=404, detail=msg) from exc
