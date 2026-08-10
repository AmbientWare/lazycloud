from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, status
from shared.containers import ContainerRecord
from shared.errors import NotFoundError
from shared.identity import AuthScope

from api.server.dependencies import current_services, require_workspace_scope
from api.server.services import ApiServices

ContainerScopeDependency = Callable[..., ContainerRecord]


def require_container_scope(scope: AuthScope) -> ContainerScopeDependency:
    """A container reached through the workspace the request names.

    The workspace comes from the shared dependency rather than from the credential,
    which names a person on an account token and so answers nothing here, and which
    would give administrator standing a second definition beside the one that
    dependency already applies.
    """

    # Depends() in a default rather than in Annotated: this module postpones
    # annotations, and an annotation naming the enclosing `scope` cannot be resolved
    # from module scope, which silently demotes the parameter to a query field.
    def dependency(
        container_id: str,
        services: Annotated[ApiServices, Depends(current_services)],
        workspace_id: str = Depends(require_workspace_scope(scope)),
    ) -> ContainerRecord:
        try:
            container = services.containers.get(container_id)
        except NotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "container not found") from exc
        if container.workspace_id != workspace_id:
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
