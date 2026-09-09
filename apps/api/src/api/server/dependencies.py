from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Protocol

from control.service import ControlPlaneService
from database.repositories.identity import WorkspaceMemberRepository
from fastapi import Depends, HTTPException, Security, WebSocket, WebSocketException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from identity.auth import AuthError, AuthorizationDeniedError, AuthorizedPrincipal
from identity.authz import AuthzRequirement, decide_authorization, workspace_requirement
from shared.errors import NotFoundError
from shared.identity import (
    AuthScope,
    AuthTokenRecord,
    PlatformRole,
    WorkspaceMemberRecord,
    WorkspaceRole,
)
from starlette.requests import HTTPConnection

from api.server.services import ApiServices

_bearer = HTTPBearer(auto_error=False)
AuthorizationCredentials = Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)]
DEFAULT_WORKSPACE_NAME = "default"
"""Workspace a request acts on when it names none and the credential names none."""

WorkspaceScopeDependency = Callable[..., Awaitable[str]]
AuthScopeDependency = Callable[..., Awaitable[None]]
OptionalTokenDependency = Callable[..., Awaitable[AuthTokenRecord | None]]
RequiredTokenDependency = Callable[..., Awaitable[AuthTokenRecord]]
RequiredPrincipalDependency = Callable[..., Awaitable[AuthorizedPrincipal]]


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
        return ControlPlaneService(services.context).get_workspace(workspace).id
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


async def current_workspace_id(
    services: Annotated[ApiServices, Depends(current_services)],
    workspace: str = "default",
) -> str:
    try:
        record = await services.require_async_io().database.run_transaction(
            lambda session: services.context.workspace(session, workspace)
        )
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return record.id


async def _unnamed_workspace(services: ApiServices, token: AuthTokenRecord) -> str:
    """The workspace a request meant when it named none.

    Every account here is given its own, so a workspace literally called
    "default" belongs to whichever account claimed that name and to nobody
    else. The answer is the workspace the account owns rather than its only
    membership: joining somebody else's must not change which one is theirs.
    """
    if not token.names_user or not token.user_id:
        return DEFAULT_WORKSPACE_NAME
    owned = await services.require_async_io().database.run_transaction(
        lambda session: WorkspaceMemberRepository(session).owned_workspace(token.user_id)
    )
    return DEFAULT_WORKSPACE_NAME if owned is None else owned.id


def require_workspace_scope(
    scope: AuthScope,
    *,
    strict: bool = False,
) -> WorkspaceScopeDependency:
    async def dependency(
        services: Annotated[ApiServices, Depends(current_services)],
        credentials: AuthorizationCredentials = None,
        workspace: str | None = None,
    ) -> str:
        principal = await require_principal(services, credentials, scope)
        token = principal.token
        return await authorize_token_workspace_async(
            services,
            token,
            workspace or token.workspace_id or await _unnamed_workspace(services, token),
            scope,
            platform_role=principal.platform_role,
            strict=strict,
        )

    return dependency


def require_transfer_scope(scope: AuthScope) -> WorkspaceScopeDependency:
    async def dependency(
        services: Annotated[ApiServices, Depends(current_services)],
        workspace_id: str = Depends(require_workspace_scope(scope)),
    ) -> str:
        await services.require_async_io().database.run_transaction(
            lambda session: services.payment_admission.assert_may_take_on_billed_work(
                session,
                workspace_id=workspace_id,
            )
        )
        return workspace_id

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

    A user-principal token reaches a workspace only through a membership row.
    Resolving it here, rather than in each route that remembered to ask, keeps
    the rule identical on every path. `authorize_token_workspace_async` is the
    same decision for handlers on the event loop.
    """
    canonical = canonical_workspace_id(services, workspace)
    membership = (
        services.users.membership(workspace_id=canonical, user_id=token.user_id)
        if token.names_user and token.user_id
        else None
    )
    return _decided_workspace(
        token,
        canonical,
        membership,
        action,
        platform_role=platform_role,
        strict=strict,
        required_role=required_role,
    )


async def authorize_token_workspace_async(
    services: ApiServices,
    token: AuthTokenRecord,
    workspace: str,
    action: AuthScope,
    *,
    platform_role: PlatformRole,
    strict: bool = False,
    required_role: WorkspaceRole = WorkspaceRole.Member,
) -> str:
    try:
        canonical, membership = await services.auth.workspace_access_async(
            services.require_async_io().database,
            token,
            workspace,
        )
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return _decided_workspace(
        token,
        canonical,
        membership,
        action,
        platform_role=platform_role,
        strict=strict,
        required_role=required_role,
    )


def _decided_workspace(
    token: AuthTokenRecord,
    canonical: str,
    membership: WorkspaceMemberRecord | None,
    action: AuthScope,
    *,
    platform_role: PlatformRole,
    strict: bool,
    required_role: WorkspaceRole,
) -> str:
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


async def authorize_websocket(
    services: ApiServices,
    websocket: WebSocket,
) -> AuthorizedPrincipal:
    try:
        async_io = services.require_async_io()
        principal = await services.auth.authorize_principal_async(
            async_io.database,
            async_io.auth_invalidation,
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


async def websocket_workspace(
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
        return await authorize_token_workspace_async(
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


async def authorize_websocket_workspace(services: ApiServices, websocket: WebSocket) -> str:
    return await websocket_workspace(
        services,
        websocket,
        await authorize_websocket(services, websocket),
        AuthScope.Write,
    )


async def authorize_principal(
    services: ApiServices,
    credentials: HTTPAuthorizationCredentials | None,
    scope: AuthScope,
    *,
    requirement: AuthzRequirement | None = None,
    allow_if_no_tokens: bool = False,
) -> AuthorizedPrincipal | None:
    try:
        async_io = services.require_async_io()
        return await services.auth.authorize_principal_async(
            async_io.database,
            async_io.auth_invalidation,
            authorization_header(credentials),
            requirement or AuthzRequirement(action=scope),
            allow_if_no_tokens=allow_if_no_tokens,
        )
    except AuthorizationDeniedError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc


async def require_principal(
    services: ApiServices,
    credentials: HTTPAuthorizationCredentials | None,
    scope: AuthScope,
) -> AuthorizedPrincipal:
    principal = await authorize_principal(services, credentials, scope)
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing authorization principal")
    return principal


async def authorize_services(
    services: ApiServices,
    credentials: HTTPAuthorizationCredentials | None,
    scope: AuthScope,
    *,
    requirement: AuthzRequirement | None = None,
    allow_if_no_tokens: bool = False,
) -> AuthTokenRecord | None:
    principal = await authorize_principal(
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
    async def dependency(
        services: Annotated[ApiServices, Depends(current_services)],
        credentials: AuthorizationCredentials = None,
    ) -> None:
        _ = await authorize_services(
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
    async def dependency(
        services: Annotated[ApiServices, Depends(current_services)],
        credentials: AuthorizationCredentials = None,
    ) -> AuthTokenRecord | None:
        return await authorize_services(
            services,
            credentials,
            scope,
            allow_if_no_tokens=allow_if_no_tokens,
        )

    return dependency


def require_workspace_token(
    scope: AuthScope,
) -> RequiredTokenDependency:
    async def dependency(
        services: Annotated[ApiServices, Depends(current_services)],
        credentials: AuthorizationCredentials = None,
    ) -> AuthTokenRecord:
        return (await require_principal(services, credentials, scope)).token

    return dependency


def require_workspace_principal(
    scope: AuthScope,
) -> RequiredPrincipalDependency:
    """For a route that authorizes a workspace named in its path rather than its query."""

    async def dependency(
        services: Annotated[ApiServices, Depends(current_services)],
        credentials: AuthorizationCredentials = None,
    ) -> AuthorizedPrincipal:
        return await require_principal(services, credentials, scope)

    return dependency


def require_user_scope(scope: AuthScope) -> WorkspaceScopeDependency:
    """Authorize the acting account for a resource a person owns."""

    async def dependency(
        services: Annotated[ApiServices, Depends(current_services)],
        credentials: AuthorizationCredentials = None,
    ) -> str:
        principal = await require_principal(services, credentials, scope)
        return require_user_principal(principal.token)

    return dependency


def require_app_requirement(
    requirement: AuthzRequirement,
    *,
    allow_if_no_tokens: bool = False,
) -> AuthScopeDependency:
    async def dependency(
        services: Annotated[ApiServices, Depends(current_services)],
        credentials: AuthorizationCredentials = None,
    ) -> None:
        _ = await authorize_services(
            services,
            credentials,
            requirement.action,
            requirement=requirement,
            allow_if_no_tokens=allow_if_no_tokens,
        )

    return dependency
