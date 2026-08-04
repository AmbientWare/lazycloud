from __future__ import annotations

from enum import StrEnum

from botocore.exceptions import BotoCoreError


class AwsProviderControlErrorCode(StrEnum):
    ControlRoleUnavailable = "control_role_unavailable"
    PermissionDenied = "permission_denied"
    ResourceNotFound = "resource_not_found"
    InvalidResponse = "invalid_response"
    UpstreamUnavailable = "upstream_unavailable"


class AwsProviderControlError(RuntimeError):
    def __init__(
        self,
        code: AwsProviderControlErrorCode,
        *,
        operation: str,
        detail: str,
    ) -> None:
        self.code = code
        self.operation = operation
        self.detail = detail
        super().__init__(f"AWS provider {operation} failed: {detail}")


def upstream_error(exc: BotoCoreError, *, operation: str) -> AwsProviderControlError:
    return AwsProviderControlError(
        AwsProviderControlErrorCode.UpstreamUnavailable,
        operation=operation,
        detail=str(exc),
    )


def invalid_response_error(operation: str, detail: str) -> AwsProviderControlError:
    return AwsProviderControlError(
        AwsProviderControlErrorCode.InvalidResponse,
        operation=operation,
        detail=detail,
    )
