from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from identity.authz import admin_requirement, machine_requirement
from shared.identity import AuthScope, AuthTokenRecord

from api.server.dependencies import (
    require_app_requirement,
    require_app_scope,
    require_app_token,
    require_machine_workspace,
    require_workspace_scope,
    require_workspace_token,
)

type read_access = Annotated[None, Depends(require_app_scope(AuthScope.Read))]
type write_access = Annotated[None, Depends(require_app_scope(AuthScope.Write))]
type read_workspace = Annotated[str, Depends(require_workspace_scope(AuthScope.Read))]
type write_workspace = Annotated[str, Depends(require_workspace_scope(AuthScope.Write))]
type read_token = Annotated[
    AuthTokenRecord,
    Depends(require_workspace_token(AuthScope.Read)),
]
type write_token = Annotated[
    AuthTokenRecord,
    Depends(require_workspace_token(AuthScope.Write)),
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
type machine_access = Annotated[
    None,
    Depends(require_app_requirement(machine_requirement())),
]
type machine_workspace = Annotated[
    str,
    Depends(require_machine_workspace(machine_requirement())),
]

__all__ = [
    "admin_access",
    "machine_access",
    "machine_workspace",
    "read_access",
    "read_app_token",
    "read_token",
    "read_workspace",
    "write_access",
    "write_app_token",
    "write_token",
    "write_workspace",
]
