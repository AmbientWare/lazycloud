from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from identity.auth import AuthorizedPrincipal
from identity.authz import admin_requirement
from shared.identity import AuthScope, AuthTokenRecord

from api.server.dependencies import (
    require_app_requirement,
    require_app_scope,
    require_app_token,
    require_transfer_scope,
    require_user_scope,
    require_workspace_principal,
    require_workspace_scope,
    require_workspace_token,
)

type read_access = Annotated[None, Depends(require_app_scope(AuthScope.Read))]
type write_access = Annotated[None, Depends(require_app_scope(AuthScope.Write))]
type read_workspace = Annotated[str, Depends(require_workspace_scope(AuthScope.Read))]
type write_workspace = Annotated[str, Depends(require_workspace_scope(AuthScope.Write))]
type read_transfer = Annotated[str, Depends(require_transfer_scope(AuthScope.Read))]
type write_transfer = Annotated[str, Depends(require_transfer_scope(AuthScope.Write))]
type read_user = Annotated[str, Depends(require_user_scope(AuthScope.Read))]
type write_user = Annotated[str, Depends(require_user_scope(AuthScope.Write))]
type read_token = Annotated[
    AuthTokenRecord,
    Depends(require_workspace_token(AuthScope.Read)),
]
type write_token = Annotated[
    AuthTokenRecord,
    Depends(require_workspace_token(AuthScope.Write)),
]
type read_principal = Annotated[
    AuthorizedPrincipal,
    Depends(require_workspace_principal(AuthScope.Read)),
]
type write_principal = Annotated[
    AuthorizedPrincipal,
    Depends(require_workspace_principal(AuthScope.Write)),
]
type read_app_token = Annotated[
    AuthTokenRecord | None,
    Depends(require_app_token(AuthScope.Read)),
]
type write_app_token = Annotated[
    AuthTokenRecord | None,
    Depends(require_app_token(AuthScope.Write)),
]
type admin_access = Annotated[
    None,
    Depends(require_app_requirement(admin_requirement())),
]

__all__ = [
    "admin_access",
    "read_access",
    "read_app_token",
    "read_principal",
    "read_token",
    "read_transfer",
    "read_user",
    "read_workspace",
    "write_access",
    "write_app_token",
    "write_principal",
    "write_token",
    "write_transfer",
    "write_user",
    "write_workspace",
]
