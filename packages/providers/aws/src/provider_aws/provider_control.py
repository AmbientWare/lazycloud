from __future__ import annotations

from enum import StrEnum


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
