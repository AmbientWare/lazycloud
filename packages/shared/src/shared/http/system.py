from __future__ import annotations

from datetime import datetime

from pydantic import Field

from shared.enums import StringEnum
from shared.http.base import HttpModel
from shared.identity import AuthScope, TokenKind, TokenStatus


class HealthStatus(StringEnum):
    Ok = "ok"
    NotOk = "not ok"


class HealthCheckResult(HttpModel):
    ok: bool
    error: str = ""


class HealthResponse(HttpModel):
    ok: bool
    status: HealthStatus
    checks: dict[str, HealthCheckResult] = Field(default_factory=dict)


class AuthTokenResponse(HttpModel):
    id: str
    name: str
    prefix: str
    kind: TokenKind = TokenKind.Workspace
    # Exactly one is set: a credential names the person who holds it or the single
    # workspace it was minted for.
    user_id: str = ""
    workspace_id: str = ""
    status: TokenStatus = TokenStatus.Active
    scopes: list[str] = Field(default_factory=lambda: ["*"])
    reusable: bool = True
    disabled_by_admin: bool = False
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class TokenCreateRequest(HttpModel):
    """Everything a person chooses when minting a credential for their account.

    No workspace and no kind: a token names the account, reaches every workspace that
    account belongs to, and is resolved per request from membership. The kinds that do
    name a workspace are minted by the platform for workers and machines, never here.
    """

    name: str = "default"
    expires_in_seconds: int | None = None


class TokenAdminUpdateRequest(HttpModel):
    disabled: bool


class TokenListResponse(HttpModel):
    data: list[AuthTokenResponse] = Field(default_factory=list)
    next: str = ""


class TokenAdminUpdateResponse(HttpModel):
    tokens: list[AuthTokenResponse] = Field(default_factory=list)


class TokenCreateResponse(HttpModel):
    token: str
    record: AuthTokenResponse


class WorkspaceSigningKeyResponse(HttpModel):
    signing_key: str


class AuthorizeRequest(HttpModel):
    action: AuthScope = AuthScope.Read
    resource_kind: str = "control-plane"
    resource_id: str | None = None
    workspace_id: str | None = None
    allowed_token_kinds: list[TokenKind] | None = None
    strict_workspace: bool = False
    require_admin: bool = False
    require_worker: bool = False
    require_machine: bool = False


class AuthzPrincipalResponse(HttpModel):
    token_id: str
    token_name: str
    token_kind: TokenKind
    workspace_id: str
    scopes: list[str] = Field(default_factory=list)
    reusable: bool = True
    disabled_by_admin: bool = False
    status: TokenStatus = TokenStatus.Active


class AuthzRequirementResponse(HttpModel):
    action: AuthScope
    resource_kind: str
    resource_id: str | None = None
    workspace_id: str | None = None
    allowed_token_kinds: list[TokenKind] | None = None
    strict_workspace: bool = False
    require_admin: bool = False
    require_worker: bool = False
    require_machine: bool = False


class AuthzDecisionResponse(HttpModel):
    effect: str
    reason: str
    message: str
    requirement: AuthzRequirementResponse
    principal: AuthzPrincipalResponse | None = None
    evaluated_at: datetime
    allowed: bool


class AuthzPolicyInputResponse(HttpModel):
    principal: AuthzPrincipalResponse | None = None
    action: AuthScope
    resource_kind: str
    resource_id: str | None = None
    workspace_id: str | None = None


class AuthorizeResponse(HttpModel):
    decision: AuthzDecisionResponse
    policy_input: AuthzPolicyInputResponse


__all__ = [
    "AuthTokenResponse",
    "AuthorizeRequest",
    "AuthorizeResponse",
    "AuthzDecisionResponse",
    "AuthzPolicyInputResponse",
    "HealthCheckResult",
    "HealthResponse",
    "HealthStatus",
    "TokenAdminUpdateRequest",
    "TokenAdminUpdateResponse",
    "TokenCreateRequest",
    "TokenCreateResponse",
    "TokenListResponse",
    "WorkspaceSigningKeyResponse",
]
