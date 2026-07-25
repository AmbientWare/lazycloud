from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Protocol

from control.service import ControlPlaneService
from fastapi import Depends, HTTPException, Security, WebSocket, WebSocketException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from identity.auth import AuthError, AuthorizationDeniedError
from identity.authz import AuthzRequirement, decide_authorization, workspace_requirement
from shared.errors import NotFoundError
from shared.identity import AuthScope, AuthTokenRecord, WorkspaceStatus
from starlette.requests import HTTPConnection

from api.server.services import ApiServices

_bearer = HTTPBearer(auto_error=False)
AuthorizationCredentials = Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)]
WorkspaceScopeDependency = Callable[..., str]
AuthScopeDependency = Callable[..., None]
OptionalTokenDependency = Callable[..., AuthTokenRecord | None]
RequiredTokenDependency = Callable[..., AuthTokenRecord]
WebSocketAuthDependency = Callable[..., Awaitable[None]]


class _ApiServicesState(Protocol):
    api_services: ApiServices


class _ApiServicesApplication(Protocol):
    @property
    def state(self) -> _ApiServicesState: ...


class _ApiServicesConnection(Protocol):
    @property
    def app(self) -> _ApiServicesApplication: ...


def api_services(connection: HTTPConnection) -> ApiServices:
    return _api_services_from_connection(connection)


def websocket_api_services(websocket: WebSocket) -> ApiServices:
    return _api_services_from_connection(websocket)


def _api_services_from_connection(connection: _ApiServicesConnection) -> ApiServices:
    services = connection.app.state.api_services
    if not isinstance(services, ApiServices):
        raise RuntimeError("FastAPI app is missing API services")
    return services


def current_services(services: Annotated[ApiServices, Depends(api_services)]) -> ApiServices:
    return services


def canonical_workspace_id(services: ApiServices, workspace: str = "default") -> str:
    try:
        record = ControlPlaneService(services.context).get_workspace(workspace)
        if record.status is not WorkspaceStatus.Active:
            raise NotFoundError(f"workspace not found: {workspace}")
        return record.id
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


def current_workspace_id(
    services: Annotated[ApiServices, Depends(current_services)],
    workspace: str = "default",
) -> str:
    return canonical_workspace_id(services, workspace)


def require_workspace_scope(
    scope: AuthScope,
    *,
    strict: bool = False,
) -> WorkspaceScopeDependency:
    def dependency(
        services: Annotated[ApiServices, Depends(current_services)],
        credentials: AuthorizationCredentials = None,
        workspace: str | None = None,
    ) -> str:
        token = authorize_services(services, credentials, scope)
        if token is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing authorization principal")
        return authorize_token_workspace(
            services,
            token,
            workspace or token.workspace_id,
            scope,
            strict=strict,
        )

    return dependency


def authorize_token_workspace(
    services: ApiServices,
    token: AuthTokenRecord,
    workspace: str,
    action: AuthScope,
    *,
    strict: bool = False,
) -> str:
    canonical = canonical_workspace_id(services, workspace)
    decision = decide_authorization(
        token,
        workspace_requirement(canonical, action=action, strict=strict),
    )
    if not decision.allowed:
        # The token authenticated but is not allowed to act on this workspace.
        raise HTTPException(status.HTTP_403_FORBIDDEN, decision.message)
    return canonical


def current_websocket_services(
    services: Annotated[ApiServices, Depends(websocket_api_services)],
) -> ApiServices:
    return services


def authorization_header(credentials: HTTPAuthorizationCredentials | None) -> str | None:
    if credentials is None:
        return None
    if credentials.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid authorization scheme")
    return f"Bearer {credentials.credentials}"


def websocket_authorization_header(websocket: WebSocket) -> str | None:
    return websocket.headers.get("authorization")


def authorize_services(
    services: ApiServices,
    credentials: HTTPAuthorizationCredentials | None,
    scope: AuthScope,
    *,
    requirement: AuthzRequirement | None = None,
    allow_if_no_tokens: bool = False,
) -> AuthTokenRecord | None:
    try:
        return services.auth.authorize_header(
            authorization_header(credentials),
            requirement or AuthzRequirement(action=scope),
            allow_if_no_tokens=allow_if_no_tokens,
        )
    except AuthorizationDeniedError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc


def require_auth_scope(
    scope: AuthScope,
    *,
    allow_if_no_tokens: bool = False,
) -> AuthScopeDependency:
    def dependency(
        services: Annotated[ApiServices, Depends(current_services)],
        credentials: AuthorizationCredentials = None,
    ) -> None:
        _ = authorize_services(
            services,
            credentials,
            scope,
            allow_if_no_tokens=allow_if_no_tokens,
        )

    return dependency


def require_app_scope(
    scope: AuthScope,
    *,
    allow_if_no_tokens: bool = False,
) -> AuthScopeDependency:
    def dependency(
        services: Annotated[ApiServices, Depends(current_services)],
        credentials: AuthorizationCredentials = None,
    ) -> None:
        _ = authorize_services(
            services,
            credentials,
            scope,
            allow_if_no_tokens=allow_if_no_tokens,
        )

    return dependency


def require_app_token(
    scope: AuthScope,
    *,
    allow_if_no_tokens: bool = False,
) -> OptionalTokenDependency:
    def dependency(
        services: Annotated[ApiServices, Depends(current_services)],
        credentials: AuthorizationCredentials = None,
    ) -> AuthTokenRecord | None:
        return authorize_services(
            services,
            credentials,
            scope,
            allow_if_no_tokens=allow_if_no_tokens,
        )

    return dependency


def require_workspace_token(
    scope: AuthScope,
) -> RequiredTokenDependency:
    def dependency(
        services: Annotated[ApiServices, Depends(current_services)],
        credentials: AuthorizationCredentials = None,
    ) -> AuthTokenRecord:
        token = authorize_services(services, credentials, scope)
        if token is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing authorization principal")
        return token

    return dependency


def require_app_requirement(
    requirement: AuthzRequirement,
    *,
    allow_if_no_tokens: bool = False,
) -> AuthScopeDependency:
    def dependency(
        services: Annotated[ApiServices, Depends(current_services)],
        credentials: AuthorizationCredentials = None,
    ) -> None:
        _ = authorize_services(
            services,
            credentials,
            requirement.action,
            requirement=requirement,
            allow_if_no_tokens=allow_if_no_tokens,
        )

    return dependency


def require_app_websocket_scope(
    scope: AuthScope,
    *,
    allow_if_no_tokens: bool = False,
) -> WebSocketAuthDependency:
    async def dependency(
        websocket: WebSocket,
        services: Annotated[ApiServices, Depends(current_websocket_services)],
    ) -> None:
        try:
            services.auth.authorize_header(
                websocket_authorization_header(websocket),
                AuthzRequirement(action=scope),
                allow_if_no_tokens=allow_if_no_tokens,
            )
        except AuthError as exc:
            raise WebSocketException(
                code=status.WS_1008_POLICY_VIOLATION,
                reason=str(exc),
            ) from exc

    return dependency


def require_websocket_scope(
    scope: AuthScope,
    *,
    allow_if_no_tokens: bool = False,
) -> WebSocketAuthDependency:
    async def dependency(
        websocket: WebSocket,
        services: Annotated[ApiServices, Depends(current_websocket_services)],
    ) -> None:
        try:
            services.auth.authorize_header(
                websocket_authorization_header(websocket),
                AuthzRequirement(action=scope),
                allow_if_no_tokens=allow_if_no_tokens,
            )
        except AuthError as exc:
            raise WebSocketException(
                code=status.WS_1008_POLICY_VIOLATION,
                reason=str(exc),
            ) from exc

    return dependency
