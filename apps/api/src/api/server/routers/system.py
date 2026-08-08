from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from control.service import ControlPlaneService
from database.records.identity import DeviceAuthorizationRecord
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse
from identity.auth import AuthError, AuthorizationDeniedError
from identity.authz import AuthzRequirement, AuthzResourceKind
from identity.device_auth import DeviceAuthorizationService
from shared.errors import ConflictError
from shared.http.device_auth import (
    DEVICE_AUTHORIZATION_VERIFICATION_PATH,
    DeviceCodeCreateRequest,
    DeviceCodeCreateResponse,
    DeviceCodeResponse,
    DeviceCodeTokenRequest,
    DeviceCodeTokenResponse,
)
from shared.http.system import (
    AuthorizeRequest,
    AuthorizeResponse,
    AuthTokenResponse,
    AuthzDecisionResponse,
    AuthzPolicyInputResponse,
    HealthCheckResult,
    HealthResponse,
    HealthStatus,
    TokenAdminUpdateRequest,
    TokenAdminUpdateResponse,
    TokenCreateRequest,
    TokenCreateResponse,
    TokenListResponse,
    WorkspaceSigningKeyResponse,
)
from shared.identity import AuthScope, AuthTokenRecord, TokenKind
from shared.usage import usage_to_prometheus

from api.server.auth import (
    admin_access,
    read_token,
    read_workspace,
    write_token,
    write_workspace,
)
from api.server.dependencies import (
    AuthorizationCredentials,
    api_services,
    authorization_header,
    authorize_token_workspace,
    current_services,
    require_user_principal,
)
from api.server.services import ApiServices

router = APIRouter()
USAGE_METRICS_RECORD_LIMIT = 5000


def _public_token(record: AuthTokenRecord) -> AuthTokenResponse:
    return AuthTokenResponse.model_validate(
        record.model_dump(mode="json", exclude={"token_hash", "worker_id"})
    )


def _authorize_token_issuance(issuer: AuthTokenRecord, requested_kind: TokenKind) -> None:
    if requested_kind is not TokenKind.Workspace and issuer.kind is not TokenKind.Admin:
        raise AuthorizationDeniedError("admin token required to issue non-workspace tokens")


def _reject_self_token_mutation(
    token: AuthTokenRecord,
    token_id: str,
    *,
    action: str,
) -> None:
    if token_id == token.id:
        raise ConflictError(f"cannot {action} the authenticating token")


@router.get("/health", response_model=HealthResponse)
def health(
    response: Response,
    services: Annotated[ApiServices, Depends(api_services)],
) -> HealthResponse:
    checks = {
        "database": _health_check(services.context.database.ping),
        "redis": _health_check(services.redis().ping),
    }
    ok = all(check.ok for check in checks.values())
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        ok=ok,
        status=HealthStatus.Ok if ok else HealthStatus.NotOk,
        checks=checks,
    )


def _health_check(check: Callable[[], bool]) -> HealthCheckResult:
    try:
        ok = check()
    except Exception as exc:
        return HealthCheckResult(ok=False, error=type(exc).__name__)
    if not ok:
        return HealthCheckResult(ok=False, error="unhealthy")
    return HealthCheckResult(ok=True)


@router.get("/metrics", response_class=PlainTextResponse)
def metrics(
    services: ApiServices = Depends(current_services),
    *,
    _auth: admin_access,
) -> PlainTextResponse:
    body = services.metrics.prometheus_text()
    usage_records = services.usage.list()[:USAGE_METRICS_RECORD_LIMIT]
    if usage_records:
        body += usage_to_prometheus(usage_records)
    elif services.compute.usage_exporter is not None:
        body += services.compute.usage_exporter.prometheus_text()
    return PlainTextResponse(body)


@router.get("/api/v1/tokens/all", response_model=TokenListResponse, operation_id="list_tokens")
def list_tokens(
    services: ApiServices = Depends(current_services),
    *,
    _auth: admin_access,
) -> TokenListResponse:
    tokens = services.auth.list_tokens()
    return TokenListResponse(tokens=[_public_token(item) for item in tokens])


@router.patch(
    "/api/v1/tokens/admin/{workspace_id}",
    response_model=TokenAdminUpdateResponse,
    operation_id="admin_update_workspace_tokens",
)
def api_v1_admin_update_workspace_tokens(
    workspace_id: str,
    request: TokenAdminUpdateRequest,
    services: ApiServices = Depends(current_services),
    *,
    _auth: admin_access,
) -> TokenAdminUpdateResponse:
    records = services.auth.set_workspace_tokens_admin_disabled(
        workspace_id,
        disabled=request.disabled,
    )
    return TokenAdminUpdateResponse(tokens=[_public_token(item) for item in records])


@router.get(
    "/api/v1/tokens/signing-key",
    response_model=WorkspaceSigningKeyResponse,
    operation_id="get_workspace_signing_key",
)
def api_v1_workspace_signing_key(
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> WorkspaceSigningKeyResponse:
    return WorkspaceSigningKeyResponse(
        signing_key=ControlPlaneService(services.context).workspace_signing_key(workspace_id)
    )


@router.get(
    "/api/v1/tokens",
    response_model=TokenListResponse,
    operation_id="list_workspace_tokens",
)
def api_v1_list_workspace_tokens(
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> TokenListResponse:
    records = services.auth.list_workspace_tokens(workspace_id)
    return TokenListResponse(tokens=[_public_token(item) for item in records])


@router.post(
    "/api/v1/tokens",
    response_model=TokenCreateResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_workspace_token",
)
def api_v1_create_workspace_token(
    request: TokenCreateRequest,
    *,
    workspace_id: write_workspace,
    token: write_token,
    services: ApiServices = Depends(current_services),
) -> TokenCreateResponse:
    _authorize_token_issuance(token, request.kind)
    authorized_workspace_id = authorize_token_workspace(
        services,
        token,
        request.workspace_id or workspace_id,
        AuthScope.Write,
    )
    raw_token, record = services.auth.create_token(
        request.name,
        scopes=request.scopes,
        expires_in_seconds=request.expires_in_seconds,
        kind=request.kind,
        workspace_id=authorized_workspace_id,
        reusable=request.reusable,
        audit_actor=token,
    )
    return TokenCreateResponse(token=raw_token, record=_public_token(record))


@router.post(
    "/api/v1/tokens/{token_id}/revoke",
    response_model=AuthTokenResponse,
    operation_id="revoke_workspace_token",
)
def api_v1_revoke_workspace_token(
    token_id: str,
    workspace_id: write_workspace,
    token: write_token,
    services: ApiServices = Depends(current_services),
) -> AuthTokenResponse:
    _reject_self_token_mutation(token, token_id, action="revoke")
    record = services.auth.revoke_workspace_token(
        workspace_id,
        token_id,
        audit_actor=token,
    )
    return _public_token(record)


@router.post(
    "/api/v1/tokens/{token_id}/toggle",
    response_model=AuthTokenResponse,
    operation_id="toggle_workspace_token",
)
def api_v1_toggle_workspace_token(
    token_id: str,
    workspace_id: write_workspace,
    token: write_token,
    services: ApiServices = Depends(current_services),
) -> AuthTokenResponse:
    _reject_self_token_mutation(token, token_id, action="toggle")
    record = services.auth.toggle_workspace_token(
        workspace_id,
        token_id,
        audit_actor=token,
    )
    return _public_token(record)


@router.delete(
    "/api/v1/tokens/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_workspace_token",
)
def api_v1_delete_workspace_token(
    token_id: str,
    workspace_id: write_workspace,
    token: write_token,
    services: ApiServices = Depends(current_services),
) -> None:
    _reject_self_token_mutation(token, token_id, action="delete")
    services.auth.delete_workspace_token(
        workspace_id,
        token_id,
        audit_actor=token,
    )


def _device_code_response(record: DeviceAuthorizationRecord) -> DeviceCodeResponse:
    return DeviceCodeResponse(
        user_code=record.user_code,
        client_name=record.client_name,
        status=record.status,
        created_at=record.created_at,
        expires_at=record.expires_at,
    )


@router.post(
    "/auth/device",
    response_model=DeviceCodeCreateResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="start_device_authorization",
)
def start_device_authorization(
    request: DeviceCodeCreateRequest,
    http_request: Request,
    services: ApiServices = Depends(current_services),
) -> DeviceCodeCreateResponse:
    started = DeviceAuthorizationService(services.context).start(client_name=request.client_name)
    base_url = str(http_request.base_url).rstrip("/")
    verification_uri = f"{base_url}/{DEVICE_AUTHORIZATION_VERIFICATION_PATH}"
    return DeviceCodeCreateResponse(
        device_code=started.device_code,
        user_code=started.record.user_code,
        verification_uri=verification_uri,
        verification_uri_complete=f"{verification_uri}?code={started.record.user_code}",
        expires_in_seconds=started.expires_in_seconds,
        poll_interval_seconds=started.poll_interval_seconds,
    )


@router.post(
    "/auth/device/token",
    response_model=DeviceCodeTokenResponse,
    operation_id="claim_device_authorization",
)
def claim_device_authorization(
    request: DeviceCodeTokenRequest,
    services: ApiServices = Depends(current_services),
) -> DeviceCodeTokenResponse:
    claim = DeviceAuthorizationService(services.context).claim(request.device_code)
    return DeviceCodeTokenResponse(
        status=claim.status,
        token=claim.token,
        username=claim.username,
    )


@router.get(
    "/api/v1/device-codes/{user_code}",
    response_model=DeviceCodeResponse,
    operation_id="get_device_code",
)
def api_v1_get_device_code(
    user_code: str,
    services: ApiServices = Depends(current_services),
    *,
    _token: read_token,
) -> DeviceCodeResponse:
    return _device_code_response(DeviceAuthorizationService(services.context).get(user_code))


@router.post(
    "/api/v1/device-codes/{user_code}/approve",
    response_model=DeviceCodeResponse,
    operation_id="approve_device_code",
)
def api_v1_approve_device_code(
    user_code: str,
    services: ApiServices = Depends(current_services),
    *,
    token: write_token,
) -> DeviceCodeResponse:
    """Approve a waiting CLI for the signed-in account.

    No workspace is chosen here: the credential the CLI claims reaches every
    workspace the approving person belongs to, and the CLI picks its active one.
    """
    user_id = require_user_principal(services, token)
    record = DeviceAuthorizationService(services.context).approve(user_code, user_id=user_id)
    return _device_code_response(record)


@router.post(
    "/api/v1/device-codes/{user_code}/deny",
    response_model=DeviceCodeResponse,
    operation_id="deny_device_code",
)
def api_v1_deny_device_code(
    user_code: str,
    services: ApiServices = Depends(current_services),
    *,
    _token: write_token,
) -> DeviceCodeResponse:
    return _device_code_response(DeviceAuthorizationService(services.context).deny(user_code))


@router.post(
    "/auth/authorize",
    response_model=AuthorizeResponse,
    operation_id="authorize_request",
)
def authorize(
    request: AuthorizeRequest,
    services: ApiServices = Depends(current_services),
    authorization: AuthorizationCredentials = None,
) -> AuthorizeResponse:
    try:
        decision = services.auth.decide_header(
            authorization_header(authorization),
            _authorization_requirement(request),
            allow_if_no_tokens=False,
        )
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    return AuthorizeResponse(
        decision=AuthzDecisionResponse.model_validate(
            decision.model_dump(mode="json", exclude={"requirement": {"metadata"}})
        ),
        policy_input=AuthzPolicyInputResponse.model_validate(
            decision.policy_input().model_dump(mode="json", exclude={"context"})
        ),
    )


def _authorization_requirement(request: AuthorizeRequest) -> AuthzRequirement:
    return AuthzRequirement(
        action=request.action,
        resource_kind=AuthzResourceKind(request.resource_kind),
        resource_id=request.resource_id,
        workspace_id=request.workspace_id,
        allowed_token_kinds=request.allowed_token_kinds,
        strict_workspace=request.strict_workspace,
        require_admin=request.require_admin,
        require_worker=request.require_worker,
        require_machine=request.require_machine,
    )
