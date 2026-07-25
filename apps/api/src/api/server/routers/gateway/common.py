from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status
from identity.authz import AuthzRequirement, AuthzResourceKind
from shared.identity import AuthScope, AuthTokenRecord, TokenKind

from api.server.dependencies import (
    AuthorizationCredentials,
    authorize_services,
    current_services,
)
from api.server.services import ApiServices

gateway_write_requirement = AuthzRequirement(
    action=AuthScope.Write,
    resource_kind=AuthzResourceKind.Gateway,
    allowed_token_kinds=[
        TokenKind.Admin,
        TokenKind.WorkspacePrimary,
        TokenKind.Workspace,
        TokenKind.WorkspaceRestricted,
        TokenKind.Worker,
        TokenKind.WorkerPrivate,
    ],
)


def require_gateway_write_token(
    services: Annotated[ApiServices, Depends(current_services)],
    credentials: AuthorizationCredentials = None,
) -> AuthTokenRecord:
    token = authorize_services(
        services,
        credentials,
        AuthScope.Write,
        requirement=gateway_write_requirement,
    )
    if token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing authorization principal")
    return token


type gateway_write_token = Annotated[
    AuthTokenRecord,
    Depends(require_gateway_write_token),
]
