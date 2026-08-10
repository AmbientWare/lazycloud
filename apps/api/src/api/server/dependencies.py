from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Protocol

from control.service import ControlPlaneService
from fastapi import Depends, HTTPException, Security, WebSocket, WebSocketException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from identity.auth import AuthError, AuthorizationDeniedError, AuthorizedPrincipal
from identity.authz import AuthzRequirement, decide_authorization, workspace_requirement
from shared.errors import NotFoundError
from shared.identity import (
    AuthScope,
    AuthTokenRecord,
    PlatformRole,
    WorkspaceRole,
    WorkspaceStatus,
)
from starlette.requests import HTTPConnection

from api.server.services import ApiServices

_bearer = HTTPBearer(auto_error=False)
AuthorizationCredentials = Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)]
DEFAULT_WORKSPACE_NAME = "default"
"""Workspace a request acts on when it names none and the credential names none."""

WorkspaceScopeDependency = Callable[..., str]
AuthScopeDependency = Callable[..., None]
OptionalTokenDependency = Callable[..., AuthTokenRecord | None]
RequiredTokenDependency = Callable[..., AuthTokenRecord]
RequiredPrincipalDependency = Callable[..., AuthorizedPrincipal]
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
        principal = require_principal(services, credentials, scope)
        token = principal.token
        # A workspace-scoped credential names its own; a person's does not, so an
        # unnamed workspace resolves to "default" and is then checked against their
        # membership. Guessing among the workspaces they hold would sometimes act on
        # the wrong one silently, where this refuses with a reason.
        return authorize_token_workspace(
            services,
            token,
            workspace or token.workspace_id or DEFAULT_WORKSPACE_NAME,
            scope,
            platform_role=principal.platform_role,
            strict=strict,
        )

    return dependency


def authorize_token_workspace(
    services: ApiServices,
    token: AuthTokenRecord,
    workspace: str,
    action: AuthScope,
    *,
    platform_role: PlatformRole,
    strict: bool = False,
    required_role: WorkspaceRole = WorkspaceRole.Member,
) -> str:
    """Resolve and authorize the workspace a request acts on.

    The one place membership is read. A user-principal token reaches a workspace only
    through a membership row, so resolving it here—rather than in each route that
    remembered to ask—is what keeps the rule identical on every path.
    """
    canonical = canonical_workspace_id(services, workspace)
    membership = (
        services.users.membership(workspace_id=canonical, user_id=token.user_id)
        if token.names_user and token.user_id
        else None
    )
    decision = decide_authorization(
        token,
        workspace_requirement(
            canonical,
            action=action,
            strict=strict,
            membership=membership,
            required_role=required_role,
        ),
        platform_role=platform_role,
    )
    if not decision.allowed:
        # The token authenticated but is not allowed to act on this workspace.
        raise HTTPException(status.HTTP_403_FORBIDDEN, decision.message)
    return canonical


def require_user_principal(token: AuthTokenRecord) -> str:
    """The account a request acts as, for resources a person owns rather than a workspace.

    A workspace-scoped automation token deliberately fails here: connecting a cloud
    account or claiming a domain is an account-level act, and a credential minted for
    one workspace carries no authority over the account that owns it.
    """
    if not token.names_user or not token.user_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "this action requires a user credential; sign in or use a user token",
        )
    return token.user_id


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


def authorize_websocket(services: ApiServices, websocket: WebSocket) -> AuthorizedPrincipal:
    try:
        principal = services.auth.authorize_principal(
            websocket_authorization_header(websocket),
            AuthzRequirement(action=AuthScope.Write),
        )
        if principal is None:
            raise AuthError("missing authorization principal")
        return principal
    except AuthError as exc:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason=str(exc),
        ) from exc


def websocket_workspace(
    services: ApiServices,
    websocket: WebSocket,
    principal: AuthorizedPrincipal,
    scope: AuthScope,
) -> str:
    """The workspace an already-authorized socket acts in.

    A socket carries no dependency-injected workspace, so it reads the one the query
    string names and puts it through the same check an HTTP request gets. Reading it
    off the credential instead would answer nothing for an account credential, which
    names a person rather than a workspace.
    """
    named = websocket.query_params.get("workspace", "")
    token = principal.token
    try:
        return authorize_token_workspace(
            services,
            token,
            named or token.workspace_id or DEFAULT_WORKSPACE_NAME,
            scope,
            platform_role=principal.platform_role,
        )
    except HTTPException as exc:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason=str(exc.detail),
        ) from exc


def authorize_websocket_workspace(services: ApiServices, websocket: WebSocket) -> str:
    return websocket_workspace(
        services,
        websocket,
        authorize_websocket(services, websocket),
        AuthScope.Write,
    )


def authorize_principal(
    services: ApiServices,
    credentials: HTTPAuthorizationCredentials | None,
    scope: AuthScope,
    *,
    requirement: AuthzRequirement | None = None,
    allow_if_no_tokens: bool = False,
) -> AuthorizedPrincipal | None:
    try:
        return services.auth.authorize_principal(
            authorization_header(credentials),
            requirement or AuthzRequirement(action=scope),
            allow_if_no_tokens=allow_if_no_tokens,
        )
    except AuthorizationDeniedError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc


def require_principal(
    services: ApiServices,
    credentials: HTTPAuthorizationCredentials | None,
    scope: AuthScope,
) -> AuthorizedPrincipal:
    principal = authorize_principal(services, credentials, scope)
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing authorization principal")
    return principal


def authorize_services(
    services: ApiServices,
    credentials: HTTPAuthorizationCredentials | None,
    scope: AuthScope,
    *,
    requirement: AuthzRequirement | None = None,
    allow_if_no_tokens: bool = False,
) -> AuthTokenRecord | None:
    principal = authorize_principal(
        services,
        credentials,
        scope,
        requirement=requirement,
        allow_if_no_tokens=allow_if_no_tokens,
    )
    return principal.token if principal is not None else None


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
        return require_principal(services, credentials, scope).token

    return dependency


def require_workspace_principal(
    scope: AuthScope,
) -> RequiredPrincipalDependency:
    """For a route that authorizes a workspace named in its path rather than its query."""

    def dependency(
        services: Annotated[ApiServices, Depends(current_services)],
        credentials: AuthorizationCredentials = None,
    ) -> AuthorizedPrincipal:
        return require_principal(services, credentials, scope)

    return dependency


def require_user_scope(scope: AuthScope) -> WorkspaceScopeDependency:
    """Authorize the acting account for a resource a person owns."""

    def dependency(
        services: Annotated[ApiServices, Depends(current_services)],
        credentials: AuthorizationCredentials = None,
    ) -> str:
        return require_user_principal(require_principal(services, credentials, scope).token)

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
