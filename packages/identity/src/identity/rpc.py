from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping, Sequence
from enum import StrEnum

from pydantic import field_validator
from shared.contracts import ContractModel
from shared.identity import AuthTokenRecord, TokenKind, TokenStatus

from identity.authz import AuthzPrincipal

GATEWAY_SERVICE = "remote.runtime.v1.gateway.GatewayService"
WORKER_REPOSITORY_SERVICE = "remote.runtime.v1.worker_repository.WorkerRepositoryService"
GRPC_HEALTH_CHECK_METHOD = "/grpc.health.v1.Health/Check"
GRPC_REFLECTION_METHOD = "/grpc.reflection.v1.ServerReflection/ServerReflectionInfo"


class RpcStatusCode(StrEnum):
    Ok = "ok"
    Unauthenticated = "unauthenticated"
    PermissionDenied = "permission-denied"
    Unavailable = "unavailable"
    ResourceExhausted = "resource-exhausted"
    Unknown = "unknown"


class RpcAuthDecisionReason(StrEnum):
    Authenticated = "authenticated"
    UnauthenticatedMethod = "unauthenticated-method"
    MissingToken = "missing-token"
    InvalidToken = "invalid-token"
    InactiveToken = "inactive-token"
    DisabledToken = "disabled-token"
    RestrictedToken = "restricted-token"


class RpcRetryDecisionReason(StrEnum):
    Success = "success"
    RetryableStatus = "retryable-status"
    NonRetryableStatus = "non-retryable-status"
    MaxRetriesReached = "max-retries-reached"


class RpcMethodAuthPlan(ContractModel):
    method: str
    auth_required: bool
    reason: str


class RpcAuthConfig(ContractModel):
    debug_reflection: bool = False
    unauthenticated_methods: tuple[str, ...] = ()
    allow_restricted_tokens: bool = False


class RpcAuthDecision(ContractModel):
    method: str
    allowed: bool
    status: RpcStatusCode
    reason: RpcAuthDecisionReason
    auth_required: bool
    principal: AuthzPrincipal | None = None


class RpcOutgoingAuthPlan(ContractModel):
    metadata: dict[str, tuple[str, ...]]


class RpcRetryPlan(ContractModel):
    max_retries: int
    initial_delay_seconds: float
    retryable_statuses: tuple[RpcStatusCode, ...]
    delays_seconds: tuple[float, ...]

    @field_validator("max_retries")
    @classmethod
    def _non_negative_retries(cls, value: int) -> int:
        return max(value, 0)


class RpcRetryAttemptDecision(ContractModel):
    status: RpcStatusCode
    attempt_index: int
    attempt_number: int
    retry: bool
    exhausted: bool
    reason: RpcRetryDecisionReason
    delay_seconds: float | None = None
    error_message: str = ""


class PayloadSignature(ContractModel):
    key: str
    timestamp: int


def rpc_method_name(service: str, method: str) -> str:
    return f"/{service}/{method}"


def gateway_method(method: str) -> str:
    return rpc_method_name(GATEWAY_SERVICE, method)


def worker_repository_method(method: str) -> str:
    return rpc_method_name(WORKER_REPOSITORY_SERVICE, method)


def default_unauthenticated_rpc_methods(*, debug_reflection: bool = False) -> tuple[str, ...]:
    methods = [
        gateway_method("Authorize"),
        gateway_method("JoinAgent"),
        gateway_method("ListAgentRoutes"),
        gateway_method("RequestAgentTransportCredential"),
        gateway_method("StreamAgent"),
        gateway_method("StreamAgentTelemetry"),
        gateway_method("UpdateAgentRouteStatus"),
        GRPC_HEALTH_CHECK_METHOD,
    ]
    if debug_reflection:
        methods.append(GRPC_REFLECTION_METHOD)
    return tuple(dict.fromkeys(methods))


def is_rpc_auth_required(method: str, config: RpcAuthConfig | None = None) -> bool:
    current = config or RpcAuthConfig()
    allowed = set(default_unauthenticated_rpc_methods(debug_reflection=current.debug_reflection))
    allowed.update(current.unauthenticated_methods)
    return method not in allowed


def plan_rpc_method_auth(method: str, config: RpcAuthConfig | None = None) -> RpcMethodAuthPlan:
    required = is_rpc_auth_required(method, config)
    return RpcMethodAuthPlan(
        method=method,
        auth_required=required,
        reason="auth required" if required else "method self-authenticates or is public",
    )


def extract_bearer_token(
    metadata: Mapping[str, str | Sequence[str]],
    *,
    query_params: Mapping[str, str] | None = None,
) -> str:
    for key in ("authorization", "Authorization"):
        raw_value = metadata.get(key)
        value = _first_metadata_value(raw_value)
        if value:
            return value.removeprefix("Bearer ").strip()
    if query_params:
        return query_params.get("auth_token", "").strip()
    return ""


def outgoing_auth_metadata(token: str) -> RpcOutgoingAuthPlan:
    return RpcOutgoingAuthPlan(metadata={"authorization": (f"Bearer {token}",)})


def plan_rpc_auth_decision(
    method: str,
    metadata: Mapping[str, str | Sequence[str]],
    *,
    token_record: AuthTokenRecord | None = None,
    token_valid: bool = True,
    query_params: Mapping[str, str] | None = None,
    config: RpcAuthConfig | None = None,
) -> RpcAuthDecision:
    current = config or RpcAuthConfig()
    auth_required = is_rpc_auth_required(method, current)
    token = extract_bearer_token(metadata, query_params=query_params)
    if not token and not auth_required:
        return RpcAuthDecision(
            method=method,
            allowed=True,
            status=RpcStatusCode.Ok,
            reason=RpcAuthDecisionReason.UnauthenticatedMethod,
            auth_required=False,
        )
    if not token:
        return _rpc_auth_denied(
            method,
            auth_required,
            RpcAuthDecisionReason.MissingToken,
            RpcStatusCode.Unauthenticated,
        )
    if not token_valid or token_record is None:
        if not auth_required:
            return RpcAuthDecision(
                method=method,
                allowed=True,
                status=RpcStatusCode.Ok,
                reason=RpcAuthDecisionReason.UnauthenticatedMethod,
                auth_required=False,
            )
        return _rpc_auth_denied(
            method,
            auth_required,
            RpcAuthDecisionReason.InvalidToken,
            RpcStatusCode.Unauthenticated,
        )
    if token_record.status is not TokenStatus.Active:
        return _rpc_auth_denied(
            method,
            auth_required,
            RpcAuthDecisionReason.InactiveToken,
            RpcStatusCode.Unauthenticated,
        )
    if token_record.disabled_by_admin:
        return _rpc_auth_denied(
            method,
            auth_required,
            RpcAuthDecisionReason.DisabledToken,
            RpcStatusCode.Unauthenticated,
        )
    if token_record.kind is TokenKind.WorkspaceRestricted and not current.allow_restricted_tokens:
        return _rpc_auth_denied(
            method,
            auth_required,
            RpcAuthDecisionReason.RestrictedToken,
            RpcStatusCode.PermissionDenied,
        )
    return RpcAuthDecision(
        method=method,
        allowed=True,
        status=RpcStatusCode.Ok,
        reason=RpcAuthDecisionReason.Authenticated,
        auth_required=auth_required,
        principal=AuthzPrincipal.from_token(token_record),
    )


def should_retry_rpc_status(status: RpcStatusCode) -> bool:
    return status in {RpcStatusCode.Unavailable, RpcStatusCode.ResourceExhausted}


def plan_rpc_retry_attempt(
    status: RpcStatusCode,
    *,
    attempt_index: int,
    retry_plan: RpcRetryPlan,
) -> RpcRetryAttemptDecision:
    current_attempt = max(attempt_index, 0)
    attempt_number = current_attempt + 1
    if status is RpcStatusCode.Ok:
        return RpcRetryAttemptDecision(
            status=status,
            attempt_index=current_attempt,
            attempt_number=attempt_number,
            retry=False,
            exhausted=False,
            reason=RpcRetryDecisionReason.Success,
        )
    if status not in retry_plan.retryable_statuses:
        return RpcRetryAttemptDecision(
            status=status,
            attempt_index=current_attempt,
            attempt_number=attempt_number,
            retry=False,
            exhausted=False,
            reason=RpcRetryDecisionReason.NonRetryableStatus,
            error_message=status.value,
        )
    if retry_plan.max_retries <= 0 or current_attempt >= retry_plan.max_retries - 1:
        return RpcRetryAttemptDecision(
            status=status,
            attempt_index=current_attempt,
            attempt_number=attempt_number,
            retry=False,
            exhausted=True,
            reason=RpcRetryDecisionReason.MaxRetriesReached,
            error_message="max retries reached",
        )
    delay = (
        retry_plan.delays_seconds[current_attempt]
        if current_attempt < len(retry_plan.delays_seconds)
        else 0.0
    )
    return RpcRetryAttemptDecision(
        status=status,
        attempt_index=current_attempt,
        attempt_number=attempt_number,
        retry=True,
        exhausted=False,
        reason=RpcRetryDecisionReason.RetryableStatus,
        delay_seconds=delay,
    )


def plan_rpc_retry(
    *,
    max_retries: int,
    initial_delay_seconds: float,
    retryable_statuses: tuple[RpcStatusCode, ...] = (
        RpcStatusCode.Unavailable,
        RpcStatusCode.ResourceExhausted,
    ),
) -> RpcRetryPlan:
    bounded_retries = max(max_retries, 0)
    delays: list[float] = []
    delay = max(initial_delay_seconds, 0)
    for _ in range(bounded_retries):
        delays.append(delay)
        delay *= 2
    return RpcRetryPlan(
        max_retries=bounded_retries,
        initial_delay_seconds=max(initial_delay_seconds, 0),
        retryable_statuses=retryable_statuses,
        delays_seconds=tuple(delays),
    )


def sign_payload(
    payload: bytes,
    secret_key: str,
    *,
    timestamp: int,
) -> PayloadSignature:
    encoded_payload = base64.b64encode(payload).decode("ascii")
    data_to_sign = f"{encoded_payload}:{timestamp}".encode()
    digest = hmac.new(secret_key.encode("utf-8"), data_to_sign, hashlib.sha256).hexdigest()
    return PayloadSignature(key=digest, timestamp=timestamp)


def verify_payload_signature(
    payload: bytes,
    secret_key: str,
    signature: PayloadSignature,
) -> bool:
    expected = sign_payload(payload, secret_key, timestamp=signature.timestamp)
    return hmac.compare_digest(expected.key, signature.key)


def _rpc_auth_denied(
    method: str,
    auth_required: bool,
    reason: RpcAuthDecisionReason,
    status: RpcStatusCode,
) -> RpcAuthDecision:
    return RpcAuthDecision(
        method=method,
        allowed=False,
        status=status,
        reason=reason,
        auth_required=auth_required,
    )


def _first_metadata_value(value: str | Sequence[str] | None) -> str:
    if isinstance(value, str):
        return value.strip()
    if value:
        first = value[0]
        return first.strip() if isinstance(first, str) else str(first).strip()
    return ""
