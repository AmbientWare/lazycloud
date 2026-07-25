from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, JsonValue, computed_field
from shared.contracts import ContractModel
from shared.identity import AuthScope, AuthTokenRecord, TokenKind, TokenStatus
from shared.timestamps import utc_now


class AuthzResourceKind(StrEnum):
    Any = "*"
    ControlPlane = "control-plane"
    Workspace = "workspace"
    Token = "token"
    Gateway = "gateway"
    Worker = "worker"
    Machine = "machine"
    Cache = "cache"
    Registry = "registry"
    Object = "object"
    Event = "event"


class PolicyEffect(StrEnum):
    Allow = "allow"
    Deny = "deny"


class AuthzDecisionReason(StrEnum):
    Allowed = "allowed"
    MissingPrincipal = "missing-principal"
    InactiveToken = "inactive-token"
    DisabledToken = "disabled-token"
    MissingScope = "missing-scope"
    WrongWorkspace = "wrong-workspace"
    RestrictedToken = "restricted-token"
    WrongTokenKind = "wrong-token-kind"


WORKER_TOKEN_KINDS = frozenset({TokenKind.Worker, TokenKind.WorkerPrivate})
MACHINE_TOKEN_KINDS = frozenset({TokenKind.Machine})


class AuthzPrincipal(ContractModel):
    token_id: str
    token_name: str
    token_kind: TokenKind
    workspace_id: str
    scopes: list[str] = Field(default_factory=list)
    reusable: bool = True
    disabled_by_admin: bool = False
    status: TokenStatus = TokenStatus.Active

    @classmethod
    def from_token(cls, token: AuthTokenRecord) -> AuthzPrincipal:
        return cls(
            token_id=token.id,
            token_name=token.name,
            token_kind=token.kind,
            workspace_id=token.workspace_id,
            scopes=list(token.scopes),
            reusable=token.reusable,
            disabled_by_admin=token.disabled_by_admin,
            status=token.status,
        )


class AuthzRequirement(ContractModel):
    action: AuthScope = AuthScope.Read
    resource_kind: AuthzResourceKind = AuthzResourceKind.ControlPlane
    resource_id: str | None = None
    workspace_id: str | None = None
    allowed_token_kinds: list[TokenKind] | None = None
    strict_workspace: bool = False
    require_admin: bool = False
    require_worker: bool = False
    require_machine: bool = False
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class AuthzPolicyInput(ContractModel):
    principal: AuthzPrincipal | None = None
    action: AuthScope
    resource_kind: AuthzResourceKind
    resource_id: str | None = None
    workspace_id: str | None = None
    context: dict[str, JsonValue] = Field(default_factory=dict)


class AuthzDecision(ContractModel):
    effect: PolicyEffect
    reason: AuthzDecisionReason
    message: str
    requirement: AuthzRequirement
    principal: AuthzPrincipal | None = None
    evaluated_at: datetime = Field(default_factory=utc_now)

    @computed_field
    @property
    def allowed(self) -> bool:
        return self.effect == PolicyEffect.Allow

    def policy_input(self) -> AuthzPolicyInput:
        return build_policy_input(self.principal, self.requirement)


def token_has_scope(token: AuthTokenRecord, scope: AuthScope | str | None) -> bool:
    if scope is None:
        return True
    scope_value = scope.value if isinstance(scope, AuthScope) else scope
    return "*" in token.scopes or scope_value in token.scopes


def workspace_requirement(
    workspace_id: str,
    *,
    action: AuthScope = AuthScope.Read,
    strict: bool = False,
    resource_id: str | None = None,
) -> AuthzRequirement:
    return AuthzRequirement(
        action=action,
        resource_kind=AuthzResourceKind.Workspace,
        resource_id=resource_id or workspace_id,
        workspace_id=workspace_id,
        strict_workspace=strict,
    )


def admin_requirement(
    *,
    action: AuthScope = AuthScope.Admin,
    resource_kind: AuthzResourceKind = AuthzResourceKind.ControlPlane,
    resource_id: str | None = None,
) -> AuthzRequirement:
    return AuthzRequirement(
        action=action,
        resource_kind=resource_kind,
        resource_id=resource_id,
        require_admin=True,
    )


def worker_requirement(
    *,
    action: AuthScope = AuthScope.Worker,
    workspace_id: str | None = None,
    private_only: bool = False,
) -> AuthzRequirement:
    return AuthzRequirement(
        action=action,
        resource_kind=AuthzResourceKind.Worker,
        workspace_id=workspace_id,
        require_worker=True,
        allowed_token_kinds=[TokenKind.WorkerPrivate] if private_only else None,
    )


def machine_requirement(
    *,
    action: AuthScope = AuthScope.Machine,
    workspace_id: str | None = None,
) -> AuthzRequirement:
    return AuthzRequirement(
        action=action,
        resource_kind=AuthzResourceKind.Machine,
        workspace_id=workspace_id,
        allowed_token_kinds=[TokenKind.Machine, TokenKind.Worker, TokenKind.WorkerPrivate],
    )


def build_policy_input(
    principal: AuthzPrincipal | AuthTokenRecord | None,
    requirement: AuthzRequirement,
) -> AuthzPolicyInput:
    normalized_principal = (
        AuthzPrincipal.from_token(principal)
        if isinstance(principal, AuthTokenRecord)
        else principal
    )
    return AuthzPolicyInput(
        principal=normalized_principal,
        action=requirement.action,
        resource_kind=requirement.resource_kind,
        resource_id=requirement.resource_id,
        workspace_id=requirement.workspace_id,
        context=dict(requirement.metadata),
    )


def decide_authorization(
    token: AuthTokenRecord | None,
    requirement: AuthzRequirement,
) -> AuthzDecision:
    if token is None:
        return _deny(
            AuthzDecisionReason.MissingPrincipal,
            "missing authorization principal",
            requirement,
            None,
        )

    principal = AuthzPrincipal.from_token(token)
    if token.status != TokenStatus.Active:
        return _deny(
            AuthzDecisionReason.InactiveToken, "token is not active", requirement, principal
        )
    if token.disabled_by_admin:
        return _deny(
            AuthzDecisionReason.DisabledToken,
            "token has been disabled by an administrator",
            requirement,
            principal,
        )
    if requirement.require_admin and token.kind != TokenKind.Admin:
        return _deny(
            AuthzDecisionReason.WrongTokenKind, "admin token required", requirement, principal
        )
    if requirement.require_worker and token.kind not in WORKER_TOKEN_KINDS:
        return _deny(
            AuthzDecisionReason.WrongTokenKind, "worker token required", requirement, principal
        )
    if requirement.require_machine and token.kind not in MACHINE_TOKEN_KINDS:
        return _deny(
            AuthzDecisionReason.WrongTokenKind, "machine token required", requirement, principal
        )
    if (
        requirement.allowed_token_kinds is not None
        and token.kind not in requirement.allowed_token_kinds
    ):
        return _deny(
            AuthzDecisionReason.WrongTokenKind,
            "token kind is not allowed for this resource",
            requirement,
            principal,
        )
    if (
        requirement.workspace_id is not None
        and token.kind != TokenKind.Admin
        and token.workspace_id != requirement.workspace_id
    ):
        return _deny(
            AuthzDecisionReason.WrongWorkspace,
            "token is not authorized for this workspace",
            requirement,
            principal,
        )
    if requirement.strict_workspace and token.kind == TokenKind.WorkspaceRestricted:
        return _deny(
            AuthzDecisionReason.RestrictedToken,
            "restricted workspace tokens cannot access this resource",
            requirement,
            principal,
        )
    if token.kind != TokenKind.Admin and not token_has_scope(token, requirement.action):
        return _deny(
            AuthzDecisionReason.MissingScope,
            f"token is missing scope: {requirement.action.value}",
            requirement,
            principal,
        )

    return AuthzDecision(
        effect=PolicyEffect.Allow,
        reason=AuthzDecisionReason.Allowed,
        message="authorized",
        requirement=requirement,
        principal=principal,
    )


def _deny(
    reason: AuthzDecisionReason,
    message: str,
    requirement: AuthzRequirement,
    principal: AuthzPrincipal | None,
) -> AuthzDecision:
    return AuthzDecision(
        effect=PolicyEffect.Deny,
        reason=reason,
        message=message,
        requirement=requirement,
        principal=principal,
    )
