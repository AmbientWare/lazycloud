from __future__ import annotations

import re
from datetime import datetime

from pydantic import Field, field_validator, model_validator

from shared.capacity import MachinePool
from shared.container_requests import CONTAINER_MEMORY_BURST_FLOOR_MIB
from shared.contracts import ContractModel
from shared.enums import StringEnum

AUTHORIZATION_ERROR_MESSAGE_MAX_LENGTH = 2048


def _bounded_authorization_error_message(value: object) -> object:
    """Bound upstream failure text so a long AWS diagnostic is clipped, not lost.

    Provider denial messages carry unbounded encoded-authorization blobs; the
    durable record must keep a diagnosis rather than reject the write.
    """
    if not isinstance(value, str) or len(value) <= AUTHORIZATION_ERROR_MESSAGE_MAX_LENGTH:
        return value
    return value[: AUTHORIZATION_ERROR_MESSAGE_MAX_LENGTH - 1] + "\u2026"


_UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_OPERATION_ID_PATTERN = r"^[A-Za-z][-A-Za-z0-9]{0,127}$"
AWS_REGION_PATTERN = r"^(us-gov|us|af|ap|ca|cn|eu|il|me|mx|sa)-[a-z0-9-]+-[0-9]+$"


def _matches_aws_region(region: str) -> bool:
    return re.fullmatch(AWS_REGION_PATTERN, region) is not None


class AwsAccountConnectionPhase(StringEnum):
    AwaitingAuthorization = "awaiting_authorization"
    Validating = "validating"
    Ready = "ready"
    Degraded = "degraded"
    ReconnectPending = "reconnect_pending"
    RetiringAuthorization = "retiring_authorization"
    DisconnectDraining = "disconnect_draining"
    Revoking = "revoking"
    VerifyingRevocation = "verifying_revocation"
    ActionRequired = "action_required"


class AwsAccountConnectionAvailableAction(StringEnum):
    Authorize = "authorize"
    Validate = "validate"
    Reconnect = "reconnect"
    CancelReconnect = "cancel_reconnect"
    Remove = "remove"
    Retry = "retry"


class AwsAccountAuthorizationMode(StringEnum):
    ManagedStack = "managed_stack"
    ExistingRole = "existing_role"


class AwsAccountAuthorizationPhase(StringEnum):
    AwaitingAuthorization = "awaiting_authorization"
    Validating = "validating"
    Ready = "ready"
    Degraded = "degraded"
    Retiring = "retiring"
    Retired = "retired"


class AwsAuthorizationCleanupStatus(StringEnum):
    Pending = "pending"
    Verifying = "verifying"
    Complete = "complete"
    ActionRequired = "action_required"


class AwsAccountConnectionErrorCode(StringEnum):
    AssumeRoleDenied = "assume_role_denied"
    ExternalIdNotEnforced = "external_id_not_enforced"
    AccountMismatch = "account_mismatch"
    PermissionDrift = "permission_drift"
    StackDrift = "stack_drift"
    UpstreamUnavailable = "upstream_unavailable"


class AwsAccountComputeConfiguration(ContractModel):
    """How capacity is provisioned in one connected AWS account.

    One configuration per account rather than per workspace: the authorization it
    governs is account-wide, so two workspaces of one owner cannot be allowed to
    give the same account contradictory answers about where it may be used.
    """

    revision: int = Field(default=1, ge=1)
    default_region: str = Field(default="us-east-1", pattern=AWS_REGION_PATTERN)
    default_instance_type: str = Field(default="m7i.large", min_length=1, max_length=64)
    initial_cpu_workers: int = Field(default=1, ge=0, le=100)
    min_cpu_workers: int = Field(default=1, ge=0, le=100)
    max_cpu_instances: int = Field(default=10, ge=0, le=100)
    max_gpu_instances: int = Field(default=2, ge=0, le=100)
    min_free_cpu_millicores: int = Field(default=1_000, ge=0)
    min_free_memory_mib: int = Field(default=CONTAINER_MEMORY_BURST_FLOOR_MIB, ge=0)
    """Free memory below which the pool adds a machine.

    Stated as the smallest expansion any container is granted, so a node stops
    being counted as spare before it can no longer absorb one more default
    container growing into its allowance. A figure chosen independently of that
    allowance is a scaling policy that does not know what the burst policy
    permits, and the two drift apart silently.
    """
    allowed_regions: tuple[str, ...] = ("us-east-1",)
    allowed_instance_types: tuple[str, ...] = ()
    idle_timeout_seconds: int = Field(default=300, ge=60, le=86_400)
    root_volume_gib: int = Field(default=200, ge=50, le=2048)

    @model_validator(mode="after")
    def validate_limits(self) -> AwsAccountComputeConfiguration:
        if not self.allowed_regions:
            raise ValueError("AWS compute configuration requires at least one allowed region")
        if len(set(self.allowed_regions)) != len(self.allowed_regions):
            raise ValueError("AWS compute configuration allowed regions must be unique")
        if any(not _matches_aws_region(region) for region in self.allowed_regions):
            raise ValueError("AWS compute configuration contains an invalid region")
        if self.default_region not in self.allowed_regions:
            raise ValueError("AWS default region must be allowed")
        if not self.min_cpu_workers <= self.initial_cpu_workers <= self.max_cpu_instances:
            raise ValueError("AWS CPU worker capacity must satisfy min <= initial <= max")
        if not self.default_instance_type.strip():
            raise ValueError("AWS default instance type cannot be empty")
        if len(set(self.allowed_instance_types)) != len(self.allowed_instance_types):
            raise ValueError("AWS allowed instance types must be unique")
        if any(not instance_type.strip() for instance_type in self.allowed_instance_types):
            raise ValueError("AWS allowed instance types cannot contain empty values")
        if (
            self.allowed_instance_types
            and self.default_instance_type not in self.allowed_instance_types
        ):
            raise ValueError("AWS default instance type must be allowed")
        return self


class AwsAccountNetwork(ContractModel):
    """Where a managed pool launches its nodes in the connected account.

    Exactly two subnets, because one Auto Scaling group spans one region's zones
    and `AwsManagedPoolSpec` takes that pair. A managed-stack connection reads
    this back from its authorization stack outputs; an existing-role connection
    is given it, because nothing in that mode creates a network. Both modes carry
    it in the same place so placement reads one field rather than branching on
    how the account was authorized.
    """

    vpc_id: str = Field(min_length=1, max_length=128)
    subnet_ids: tuple[str, str]
    security_group_id: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_distinct_subnets(self) -> AwsAccountNetwork:
        if self.subnet_ids[0] == self.subnet_ids[1]:
            raise ValueError("AWS account network requires two distinct subnets")
        return self


class AwsManagedAuthorizationReference(ContractModel):
    stack_name: str = Field(min_length=1, max_length=128)
    region: str = Field(pattern=AWS_REGION_PATTERN)
    generation: int = Field(ge=1)
    stack_id: str | None = Field(default=None, min_length=1, max_length=2048)
    template_version: str = Field(min_length=1, max_length=128)
    template_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    shared_ami_ids: tuple[str, ...] = ()


class AwsAccountAuthorizationGeneration(ContractModel):
    id: str = Field(pattern=_UUID_PATTERN)
    generation: int = Field(ge=1)
    role_arn: str = Field(
        pattern=r"^arn:(aws|aws-us-gov|aws-cn):iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]{1,512}$"
    )
    authorization_mode: AwsAccountAuthorizationMode
    managed_authorization: AwsManagedAuthorizationReference | None = None
    authorization_url: str | None = Field(default=None, pattern=r"^https://[^\s]+$")
    phase: AwsAccountAuthorizationPhase
    validation_generation: int = Field(default=0, ge=0)
    last_validation_started_at: datetime | None = None
    last_validated_at: datetime | None = None
    expires_at: datetime | None = None
    error_code: AwsAccountConnectionErrorCode | None = None
    error_message: str = Field(default="", max_length=AUTHORIZATION_ERROR_MESSAGE_MAX_LENGTH)

    @field_validator("error_message", mode="before")
    @classmethod
    def bound_error_message(cls, value: object) -> object:
        return _bounded_authorization_error_message(value)

    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_authorization(self) -> AwsAccountAuthorizationGeneration:
        managed = self.authorization_mode is AwsAccountAuthorizationMode.ManagedStack
        if managed != (self.managed_authorization is not None):
            raise ValueError("managed AWS authorization requires its stack reference")
        if (
            managed
            and self.phase is AwsAccountAuthorizationPhase.AwaitingAuthorization
            and self.authorization_url is None
        ):
            raise ValueError("pending managed AWS authorization requires its customer action URL")
        if (
            self.phase
            in {
                AwsAccountAuthorizationPhase.Ready,
                AwsAccountAuthorizationPhase.Retiring,
                AwsAccountAuthorizationPhase.Retired,
            }
            and self.authorization_url is not None
        ):
            raise ValueError("active AWS authorization cannot retain its customer action URL")
        if not managed and self.authorization_url is not None:
            raise ValueError("existing-role authorization cannot carry a customer action URL")
        if self.managed_authorization is not None and (
            self.managed_authorization.generation != self.generation
        ):
            raise ValueError("managed AWS authorization generation does not match")
        if self.phase is AwsAccountAuthorizationPhase.Ready and self.last_validated_at is None:
            raise ValueError("ready AWS authorization requires successful validation")
        if (self.error_code is None) != (not self.error_message):
            raise ValueError("AWS authorization error code and message must be set together")
        return self


class AwsAccountConnection(ContractModel):
    id: str = Field(pattern=_UUID_PATTERN)
    user_id: str = Field(pattern=_UUID_PATTERN)
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    external_id: str = Field(
        min_length=32,
        max_length=256,
        pattern=r"^[A-Za-z0-9+=,.@:_/-]+$",
        repr=False,
    )
    pool: MachinePool = Field(default=MachinePool("aws"), min_length=1, max_length=240)
    """Pool every unit provisioned on this connection stamps.

    The customer's override point: units are created on demand per capability
    key, so the pool they belong to cannot live on any one of them.
    """
    compute: AwsAccountComputeConfiguration = Field(default_factory=AwsAccountComputeConfiguration)
    """Provisioning limits and defaults applied to every workspace this account backs."""
    phase: AwsAccountConnectionPhase
    active_authorization: AwsAccountAuthorizationGeneration | None = None
    pending_authorization: AwsAccountAuthorizationGeneration | None = None
    retiring_authorization: AwsAccountAuthorizationGeneration | None = None
    node_role_arn: str | None = Field(
        default=None,
        pattern=r"^arn:(aws|aws-us-gov|aws-cn):iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]{1,512}$",
    )
    node_instance_profile_arn: str | None = Field(
        default=None,
        pattern=r"^arn:(aws|aws-us-gov|aws-cn):iam::[0-9]{12}:instance-profile/[A-Za-z0-9+=,.@_/-]{1,512}$",
    )
    network: AwsAccountNetwork | None = None
    """Where this account's managed pools launch, once the network is known.

    Absent until an authorization is validated: a managed-stack connection has no
    network before its stack reports outputs, and an existing-role connection has
    none before the operator supplies one. A pool requested against a connection
    without it is refused rather than launched somewhere chosen by default.
    """
    drain_total_pools: int = Field(default=0, ge=0)
    drain_remaining_pools: int = Field(default=0, ge=0)
    customer_action_url: str | None = Field(default=None, pattern=r"^https://[^\s]+$")
    customer_action_label: str = Field(default="", max_length=128)
    revision: int = Field(default=1, ge=1)
    next_reconcile_at: datetime | None = None
    claim_token: str | None = Field(default=None, pattern=_UUID_PATTERN, repr=False)
    claim_expires_at: datetime | None = None
    reconcile_attempt_count: int = Field(default=0, ge=0)
    bucket_access_reconcile_pending: bool = False
    provider_operation_id: str | None = Field(
        default=None,
        pattern=_OPERATION_ID_PATTERN,
    )
    provider_operation_started_at: datetime | None = None
    last_error: str = Field(default="", max_length=2048)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_account_ownership(self) -> AwsAccountConnection:
        authorizations = tuple(
            item
            for item in (
                self.active_authorization,
                self.pending_authorization,
                self.retiring_authorization,
            )
            if item is not None
        )
        if any(
            item.role_arn.split(":", maxsplit=5)[4] != self.account_id for item in authorizations
        ):
            raise ValueError("AWS connection role ARN must belong to account_id")
        for resource_arn in (self.node_role_arn, self.node_instance_profile_arn):
            if (
                resource_arn is not None
                and resource_arn.split(":", maxsplit=5)[4] != self.account_id
            ):
                raise ValueError("AWS connection resource ARN must belong to account_id")
        generations = [item.generation for item in authorizations]
        if len(generations) != len(set(generations)):
            raise ValueError("AWS authorization generations must be unique")
        if self.drain_remaining_pools > self.drain_total_pools:
            raise ValueError("remaining AWS pool drain count cannot exceed total")
        if (self.node_role_arn is None) != (self.node_instance_profile_arn is None):
            raise ValueError("AWS node role and instance profile must be set together")
        if (self.claim_token is None) != (self.claim_expires_at is None):
            raise ValueError("AWS reconciliation claim token and expiry must be set together")
        if self.customer_action_url is not None and not self.customer_action_label:
            raise ValueError("AWS customer action URL requires a label")
        if self.phase is AwsAccountConnectionPhase.Ready and (
            self.active_authorization is None
            or self.active_authorization.phase is not AwsAccountAuthorizationPhase.Ready
            or self.pending_authorization is not None
            or self.retiring_authorization is not None
            or self.node_role_arn is None
        ):
            raise ValueError("ready AWS connection requires one ready active authorization")
        if self.phase is AwsAccountConnectionPhase.ReconnectPending and (
            self.active_authorization is None or self.pending_authorization is None
        ):
            raise ValueError("AWS reconnect requires active and pending authorization")
        if self.phase is AwsAccountConnectionPhase.RetiringAuthorization and (
            self.active_authorization is None or self.retiring_authorization is None
        ):
            raise ValueError("AWS authorization retirement requires active and predecessor")
        if self.phase in {
            AwsAccountConnectionPhase.DisconnectDraining,
            AwsAccountConnectionPhase.Revoking,
            AwsAccountConnectionPhase.VerifyingRevocation,
        } and (self.pending_authorization is not None or self.retiring_authorization is not None):
            raise ValueError("AWS disconnect cannot overlap authorization replacement")
        if (
            self.phase
            in {
                AwsAccountConnectionPhase.Revoking,
                AwsAccountConnectionPhase.VerifyingRevocation,
                AwsAccountConnectionPhase.RetiringAuthorization,
            }
            and self.provider_operation_id is None
        ):
            raise ValueError("AWS provider cleanup phase requires a stable operation id")
        if (self.provider_operation_id is None) != (self.provider_operation_started_at is None):
            raise ValueError("AWS provider operation id and start time must be set together")
        if self.phase is AwsAccountConnectionPhase.ActionRequired and not self.last_error:
            raise ValueError("AWS action-required phase requires an explanation")
        return self

    @property
    def role_arn(self) -> str:
        authorization = self.active_authorization or self.pending_authorization
        if authorization is None:
            raise ValueError("AWS connection has no authorization")
        return authorization.role_arn

    @property
    def authorization_mode(self) -> AwsAccountAuthorizationMode:
        authorization = self.active_authorization or self.pending_authorization
        if authorization is None:
            raise ValueError("AWS connection has no authorization")
        return authorization.authorization_mode

    @property
    def managed_authorization(self) -> AwsManagedAuthorizationReference | None:
        authorization = self.active_authorization or self.pending_authorization
        return authorization.managed_authorization if authorization is not None else None

    @property
    def hosts_workloads(self) -> bool:
        """Whether this connection is ready to run workloads.

        A readiness fact about the account, not a statement about where any
        workload is scheduled: what a workload runs on is the pool it names.
        """
        accepts = self.phase in {
            AwsAccountConnectionPhase.Ready,
            AwsAccountConnectionPhase.ReconnectPending,
            AwsAccountConnectionPhase.RetiringAuthorization,
        }
        if self.phase is AwsAccountConnectionPhase.ActionRequired:
            accepts = self.retiring_authorization is not None
        return self._active_is_ready and accepts

    @property
    def can_manage_existing_capacity(self) -> bool:
        manages = self.phase in {
            AwsAccountConnectionPhase.Ready,
            AwsAccountConnectionPhase.ReconnectPending,
            AwsAccountConnectionPhase.RetiringAuthorization,
            AwsAccountConnectionPhase.DisconnectDraining,
        }
        if self.phase is AwsAccountConnectionPhase.ActionRequired:
            manages = self.retiring_authorization is not None
        return self._active_is_ready and manages

    @property
    def available_actions(self) -> tuple[AwsAccountConnectionAvailableAction, ...]:
        if self.phase is AwsAccountConnectionPhase.AwaitingAuthorization:
            return (
                AwsAccountConnectionAvailableAction.Authorize,
                AwsAccountConnectionAvailableAction.Validate,
                AwsAccountConnectionAvailableAction.Remove,
            )
        if self.phase is AwsAccountConnectionPhase.Validating:
            return (AwsAccountConnectionAvailableAction.Remove,)
        if self.phase is AwsAccountConnectionPhase.Ready:
            return (
                AwsAccountConnectionAvailableAction.Reconnect,
                AwsAccountConnectionAvailableAction.Remove,
            )
        if self.phase is AwsAccountConnectionPhase.Degraded:
            actions = [AwsAccountConnectionAvailableAction.Validate]
            if self.active_authorization is not None:
                actions.insert(0, AwsAccountConnectionAvailableAction.Reconnect)
            actions.append(AwsAccountConnectionAvailableAction.Remove)
            return tuple(actions)
        if self.phase is AwsAccountConnectionPhase.ReconnectPending:
            return (
                AwsAccountConnectionAvailableAction.Authorize,
                AwsAccountConnectionAvailableAction.Validate,
                AwsAccountConnectionAvailableAction.CancelReconnect,
                AwsAccountConnectionAvailableAction.Remove,
            )
        if self.phase is AwsAccountConnectionPhase.RetiringAuthorization:
            return (AwsAccountConnectionAvailableAction.Remove,)
        if self.phase is AwsAccountConnectionPhase.ActionRequired:
            actions = [AwsAccountConnectionAvailableAction.Retry]
            if self.retiring_authorization is not None:
                actions.append(AwsAccountConnectionAvailableAction.Remove)
            return tuple(actions)
        return ()

    @property
    def _active_is_ready(self) -> bool:
        return (
            self.active_authorization is not None
            and self.active_authorization.phase is AwsAccountAuthorizationPhase.Ready
        )


class AwsAuthorizationCleanupTombstone(ContractModel):
    id: str = Field(pattern=_UUID_PATTERN)
    user_id: str = Field(pattern=_UUID_PATTERN)
    connection_id: str = Field(pattern=_UUID_PATTERN)
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    external_id: str = Field(
        min_length=32,
        max_length=256,
        pattern=r"^[A-Za-z0-9+=,.@:_/-]+$",
        repr=False,
    )
    authorization: AwsAccountAuthorizationGeneration
    node_role_arn: str | None = Field(
        default=None,
        pattern=r"^arn:(aws|aws-us-gov|aws-cn):iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]{1,512}$",
    )
    node_instance_profile_arn: str | None = Field(
        default=None,
        pattern=r"^arn:(aws|aws-us-gov|aws-cn):iam::[0-9]{12}:instance-profile/[A-Za-z0-9+=,.@_/-]{1,512}$",
    )
    remove_node_identity: bool
    status: AwsAuthorizationCleanupStatus = AwsAuthorizationCleanupStatus.Pending
    provider_operation_id: str = Field(pattern=_OPERATION_ID_PATTERN)
    revision: int = Field(default=1, ge=1)
    next_reconcile_at: datetime
    expires_at: datetime
    claim_token: str | None = Field(default=None, pattern=_UUID_PATTERN, repr=False)
    claim_expires_at: datetime | None = None
    reconcile_attempt_count: int = Field(default=0, ge=0)
    last_error: str = Field(default="", max_length=2048)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_cleanup(self) -> AwsAuthorizationCleanupTombstone:
        if self.authorization.role_arn.split(":", maxsplit=5)[4] != self.account_id:
            raise ValueError("AWS cleanup authorization must belong to account_id")
        if (self.node_role_arn is None) != (self.node_instance_profile_arn is None):
            raise ValueError("AWS cleanup node role and instance profile must be set together")
        if self.remove_node_identity and self.node_role_arn is None:
            raise ValueError("AWS node-identity cleanup requires its role and instance profile")
        if (self.claim_token is None) != (self.claim_expires_at is None):
            raise ValueError("AWS cleanup claim token and expiry must be set together")
        if self.expires_at <= self.created_at:
            raise ValueError("AWS cleanup tombstone expiry must be after creation")
        if self.status is AwsAuthorizationCleanupStatus.ActionRequired and not self.last_error:
            raise ValueError("AWS action-required cleanup requires an explanation")
        return self


class AwsAccountAuthorizationPlan(ContractModel):
    role_arn: str = Field(
        pattern=r"^arn:(aws|aws-us-gov|aws-cn):iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]{1,512}$"
    )
    authorization_url: str | None = Field(default=None, pattern=r"^https://[^\s]+$")
    authorization_mode: AwsAccountAuthorizationMode
    managed_authorization: AwsManagedAuthorizationReference | None = None
    node_role_arn: str = Field(
        pattern=r"^arn:(aws|aws-us-gov|aws-cn):iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]{1,512}$"
    )
    node_instance_profile_arn: str = Field(
        pattern=r"^arn:(aws|aws-us-gov|aws-cn):iam::[0-9]{12}:instance-profile/[A-Za-z0-9+=,.@_/-]{1,512}$"
    )
    network: AwsAccountNetwork | None = None
    """Known at plan time only for an existing role, where the operator supplies it.

    A managed stack has not been deployed yet at this point, so its network
    arrives later, when validation reads the stack outputs.
    """

    @model_validator(mode="after")
    def validate_authorization(self) -> AwsAccountAuthorizationPlan:
        managed = self.authorization_mode is AwsAccountAuthorizationMode.ManagedStack
        if managed != (self.managed_authorization is not None):
            raise ValueError("managed AWS authorization plan requires its stack reference")
        if managed != (self.authorization_url is not None):
            raise ValueError("managed AWS authorization plan requires an AWS console action")
        return self


class AwsAccountValidationResult(ContractModel):
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    role_arn: str = Field(
        pattern=r"^arn:(aws|aws-us-gov|aws-cn):iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]{1,512}$"
    )
    managed_authorization: AwsManagedAuthorizationReference | None = None
    node_role_arn: str = Field(
        pattern=r"^arn:(aws|aws-us-gov|aws-cn):iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]{1,512}$"
    )
    node_instance_profile_arn: str = Field(
        pattern=r"^arn:(aws|aws-us-gov|aws-cn):iam::[0-9]{12}:instance-profile/[A-Za-z0-9+=,.@_/-]{1,512}$"
    )
    network: AwsAccountNetwork | None = None
    validated_at: datetime


__all__ = [
    "AWS_REGION_PATTERN",
    "AwsAccountAuthorizationGeneration",
    "AwsAccountAuthorizationMode",
    "AwsAccountAuthorizationPhase",
    "AwsAccountAuthorizationPlan",
    "AwsAccountComputeConfiguration",
    "AwsAccountConnection",
    "AwsAccountConnectionAvailableAction",
    "AwsAccountConnectionErrorCode",
    "AwsAccountConnectionPhase",
    "AwsAccountNetwork",
    "AwsAccountValidationResult",
    "AwsAuthorizationCleanupStatus",
    "AwsAuthorizationCleanupTombstone",
    "AwsManagedAuthorizationReference",
]
