from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, status
from shared.containers import ContainerRecord
from shared.errors import NotFoundError
from shared.identity import AuthScope, TokenKind

from api.server.dependencies import (
    AuthorizationCredentials,
    authorize_services,
    current_services,
)
from api.server.services import ApiServices

ContainerScopeDependency = Callable[..., ContainerRecord]


def require_container_scope(scope: AuthScope) -> ContainerScopeDependency:
    def dependency(
        container_id: str,
        services: Annotated[ApiServices, Depends(current_services)],
        credentials: AuthorizationCredentials = None,
    ) -> ContainerRecord:
        token = authorize_services(services, credentials, scope)
        if token is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing authorization principal")
        try:
            container = services.containers.get(container_id)
        except NotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "container not found") from exc
        if token.kind is not TokenKind.Admin and container.workspace_id != token.workspace_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "container not found")
        return container

    return dependency


type read_container = Annotated[
    ContainerRecord,
    Depends(require_container_scope(AuthScope.Read)),
]
type write_container = Annotated[
    ContainerRecord,
    Depends(require_container_scope(AuthScope.Write)),
]
