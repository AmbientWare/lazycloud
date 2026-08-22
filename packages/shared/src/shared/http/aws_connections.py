from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from shared.aws_connections import (
    AWS_REGION_PATTERN,
    AwsAccountAuthorizationMode,
    AwsAccountAuthorizationPhase,
    AwsAccountComputeConfiguration,
    AwsAccountConnectionAvailableAction,
    AwsAccountConnectionErrorCode,
    AwsAccountConnectionPhase,
    AwsAccountNetwork,
)
from shared.http.base import HttpModel

_AWS_ROLE_ARN_PATTERN = (
    r"^arn:(aws|aws-us-gov|aws-cn):iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]{1,512}$"
)


class AwsConnectionCreateRequest(HttpModel):
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    role_arn: str | None = Field(default=None, pattern=_AWS_ROLE_ARN_PATTERN)
    network: AwsAccountNetwork | None = None
    # The external ID a role that already exists enforces.
    #
    # Only with a role, because the two modes differ in who the value belongs
    # to. Where this platform creates the role, it chooses the ID and the far
    # side is told. Where the role is already there, its trust is already
    # written, and the ID is a fact about it rather than something to mint: an
    # ID chosen here would have to be added to a trust policy by hand before the
    # first assume could work.
    #
    # Supplying it grants nothing. The ID is a confused-deputy guard, and it
    # guards this platform: naming a role somebody else owns fails at the assume,
    # because their trust does not name this principal.
    external_id: str | None = Field(default=None, min_length=16, max_length=1224)

    @model_validator(mode="after")
    def validate_role_account(self) -> AwsConnectionCreateRequest:
        if self.role_arn is not None and self.role_arn.split(":", maxsplit=5)[4] != self.account_id:
            raise ValueError("AWS role ARN must belong to account_id")
        if self.network is not None and self.role_arn is None:
            raise ValueError("AWS network may only be supplied with an existing role")
        if self.external_id is not None and self.role_arn is None:
            raise ValueError("AWS external ID may only be supplied with an existing role")
        return self


class AwsConnectionReconnectRequest(HttpModel):
    role_arn: str | None = Field(default=None, pattern=_AWS_ROLE_ARN_PATTERN)


class AwsComputeConfigurationUpdateRequest(HttpModel):
    expected_revision: int = Field(ge=1)
    compute: AwsAccountComputeConfiguration


class AwsManagedAuthorizationResponse(HttpModel):
    stack_name: str = Field(min_length=1, max_length=128)
    region: str = Field(pattern=AWS_REGION_PATTERN)
    generation: int = Field(ge=1)
    stack_id: str | None = Field(default=None, min_length=1, max_length=2048)
    template_version: str = Field(min_length=1, max_length=128)
    template_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AwsAuthorizationGenerationResponse(HttpModel):
    generation: int = Field(ge=1)
    authorization_mode: AwsAccountAuthorizationMode
    managed_authorization: AwsManagedAuthorizationResponse | None = None
    phase: AwsAccountAuthorizationPhase
    last_validation_started_at: datetime | None = None
    last_validated_at: datetime | None = None
    error_code: AwsAccountConnectionErrorCode | None = None
    error_message: str | None = Field(default=None, max_length=512)
    created_at: datetime
    updated_at: datetime


class AwsConnectionCustomerAction(HttpModel):
    url: str | None = Field(default=None, pattern=r"^https://[^\s]+$")
    label: str = Field(min_length=1, max_length=128)


class AwsConnectionResponse(HttpModel):
    id: str
    account_id: str
    phase: AwsAccountConnectionPhase
    revision: int = Field(ge=1)
    compute: AwsAccountComputeConfiguration
    hosts_workloads: bool
    can_manage_existing_capacity: bool
    available_actions: tuple[AwsAccountConnectionAvailableAction, ...] = ()
    detail: str = Field(max_length=512)
    customer_action: AwsConnectionCustomerAction | None = None
    next_retry_at: datetime | None = None
    active_authorization: AwsAuthorizationGenerationResponse | None = None
    pending_authorization: AwsAuthorizationGenerationResponse | None = None
    retiring_authorization: AwsAuthorizationGenerationResponse | None = None
    created_at: datetime
    updated_at: datetime


class AwsConnectionCurrentResponse(HttpModel):
    connection: AwsConnectionResponse | None = None


class AwsConnectionAuthorization(HttpModel):
    url: str | None = Field(default=None, pattern=r"^https://[^\s]+$")
    external_id: str | None = Field(default=None, min_length=32, max_length=256, repr=False)


class AwsConnectionAuthorizationResponse(HttpModel):
    connection: AwsConnectionResponse
    authorization: AwsConnectionAuthorization


__all__ = [
    "AwsAuthorizationGenerationResponse",
    "AwsComputeConfigurationUpdateRequest",
    "AwsConnectionAuthorization",
    "AwsConnectionAuthorizationResponse",
    "AwsConnectionCreateRequest",
    "AwsConnectionCurrentResponse",
    "AwsConnectionCustomerAction",
    "AwsConnectionReconnectRequest",
    "AwsConnectionResponse",
    "AwsManagedAuthorizationResponse",
]
