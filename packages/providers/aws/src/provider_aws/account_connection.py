from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Protocol, Self, TypeGuard, overload
from urllib.parse import quote, urlencode, urlparse

from boto3.session import Session
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from .account_connection_policy import validate_aws_account_connection_template_policy
from .boto3_clients import has_operations, is_boto3_client_factory
from .instance_catalog import aws_console_host, aws_partition_for_region
from .provider_control import (
    AwsProviderControlError,
    AwsProviderControlErrorCode,
    invalid_response_error,
    upstream_error,
)

AWS_ACCOUNT_CONNECTION_TEMPLATE_VERSION = "2026-07-24.v11"

_ACCOUNT_ID_PATTERN = re.compile(r"^[0-9]{12}$")
_ARN_PATTERN = re.compile(
    r"^arn:(aws|aws-us-gov|aws-cn):iam::([0-9]{12}):(role|instance-profile)/([A-Za-z0-9+=,.@_/-]+)$"
)
_EXTERNAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9+=,.@:_/-]{32,256}$")
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9+=,.@_-]{1,64}$")
_PRINCIPAL_PATTERN = re.compile(
    r"^arn:(aws|aws-us-gov|aws-cn):iam::[0-9]{12}:(root|role/[A-Za-z0-9+=,.@_/-]+)$"
)
_REGION_PATTERN = re.compile(r"^(us-gov|us|af|ap|ca|cn|eu|il|me|mx|sa)-[a-z0-9-]+-[0-9]+$")
_CLIENT_REQUEST_TOKEN_PATTERN = re.compile(r"^[A-Za-z][-A-Za-z0-9]{0,127}$")
_S3_TEMPLATE_HOST_PATTERN = re.compile(
    r"^(?:s3[.-][a-z0-9-]+|[a-z0-9][a-z0-9.-]*\.s3[.-][a-z0-9-]+)"
    r"\.amazonaws\.com(?:\.cn)?$"
)
_S3_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_S3_BUCKET_POLICY_NAME = "mounted-bucket-access"
_IAM_INLINE_POLICY_MAX_BYTES = 10_240


class AwsAccountConnectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AwsAccountConnectionTemplateIdentity(AwsAccountConnectionModel):
    version: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: int = Field(gt=0)


class AwsAccountConnectionTemplatePublication(AwsAccountConnectionModel):
    url: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        url = value.strip()
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or not parsed.path.strip("/")
            or parsed.username
            or parsed.password
            or parsed.fragment
            or not _S3_TEMPLATE_HOST_PATTERN.fullmatch((parsed.hostname or "").lower())
        ):
            raise ValueError("connection template URL must be an HTTPS S3 object URL")
        return url


class AwsManagedNodeIdentity(AwsAccountConnectionModel):
    role_name: str = Field(pattern=_NAME_PATTERN.pattern)
    role_arn: str
    instance_profile_name: str = Field(pattern=r"^[A-Za-z0-9+=,.@_-]{1,128}$")
    instance_profile_arn: str

    @model_validator(mode="after")
    def validate_names(self) -> AwsManagedNodeIdentity:
        if not self.role_arn.endswith(f":role/{self.role_name}"):
            raise ValueError("managed node role ARN does not match its name")
        if not self.instance_profile_arn.endswith(
            f":instance-profile/{self.instance_profile_name}"
        ):
            raise ValueError("managed node profile ARN does not match its name")
        return self


class AwsExistingAccountAuthorization(AwsAccountConnectionModel):
    account_id: str = Field(pattern=_ACCOUNT_ID_PATTERN.pattern)
    region: str = Field(pattern=_REGION_PATTERN.pattern)
    role_arn: str
    external_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    node_identity: AwsManagedNodeIdentity

    @model_validator(mode="after")
    def validate_scope(self) -> AwsExistingAccountAuthorization:
        match = _ARN_PATTERN.fullmatch(self.role_arn)
        if match is None or match.group(3) != "role":
            raise ValueError("existing authorization role ARN is invalid")
        partition, account_id, _, _ = match.groups()
        if partition != aws_partition_for_region(self.region) or account_id != self.account_id:
            raise ValueError("existing authorization role ARN is outside the account scope")
        _validate_node_identity_scope(
            self.node_identity,
            account_id=self.account_id,
            region=self.region,
        )
        return self


class AwsPendingAccountAuthorization(AwsAccountConnectionModel):
    account_id: str = Field(pattern=_ACCOUNT_ID_PATTERN.pattern)
    region: str = Field(pattern=_REGION_PATTERN.pattern)
    generation: int = Field(ge=1)
    stack_name: str = Field(min_length=1, max_length=128)
    role_name: str = Field(pattern=_NAME_PATTERN.pattern)
    role_arn: str
    external_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    node_identity: AwsManagedNodeIdentity

    @field_validator("role_arn")
    @classmethod
    def validate_role_arn(cls, value: str) -> str:
        arn = value.strip()
        match = _ARN_PATTERN.fullmatch(arn)
        if match is None or match.group(3) != "role":
            raise ValueError("authorization role ARN is invalid")
        return arn

    @model_validator(mode="after")
    def validate_scope(self) -> AwsPendingAccountAuthorization:
        match = _ARN_PATTERN.fullmatch(self.role_arn)
        if match is None:
            raise ValueError("authorization role ARN is invalid")
        partition, account_id, _, role_name = match.groups()
        if partition != aws_partition_for_region(self.region) or account_id != self.account_id:
            raise ValueError("authorization role ARN is outside the account scope")
        if role_name != self.role_name:
            raise ValueError("authorization role ARN does not match role_name")
        _validate_node_identity_scope(
            self.node_identity,
            account_id=self.account_id,
            region=self.region,
        )
        return self


class AwsActiveAccountAuthorization(AwsPendingAccountAuthorization):
    stack_id: str
    validated_at: datetime

    @model_validator(mode="after")
    def validate_stack_id(self) -> AwsActiveAccountAuthorization:
        prefix = (
            f"arn:{aws_partition_for_region(self.region)}:cloudformation:{self.region}:"
            f"{self.account_id}:stack/{self.stack_name}/"
        )
        if not self.stack_id.startswith(prefix) or len(self.stack_id) <= len(prefix):
            raise ValueError("authorization stack ID is outside the generation scope")
        return self


class AwsAccountAuthorizationValidationInput(AwsAccountConnectionModel):
    pending: AwsPendingAccountAuthorization
    external_id: SecretStr = Field(min_length=32, max_length=256, repr=False)

    @model_validator(mode="after")
    def validate_external_id(self) -> AwsAccountAuthorizationValidationInput:
        raw = self.external_id.get_secret_value()
        if not _EXTERNAL_ID_PATTERN.fullmatch(raw):
            raise ValueError("external ID must contain 32-256 AWS-safe characters")
        if hashlib.sha256(raw.encode()).hexdigest() != self.pending.external_id_sha256:
            raise ValueError("external ID does not match the authorization generation")
        return self


class AwsExistingAccountAuthorizationValidationInput(AwsAccountConnectionModel):
    authorization: AwsExistingAccountAuthorization
    external_id: SecretStr = Field(min_length=32, max_length=256, repr=False)

    @model_validator(mode="after")
    def validate_external_id(self) -> AwsExistingAccountAuthorizationValidationInput:
        _validate_external_id(self.authorization, self.external_id)
        return self


class AwsAccountAuthorizationValidation(AwsAccountConnectionModel):
    authorization: AwsActiveAccountAuthorization
    caller_arn: str
    node_identity: AwsManagedNodeIdentity
    vpc_id: str = Field(min_length=1)
    subnet_ids: tuple[str, ...] = Field(min_length=2)
    security_group_id: str = Field(min_length=1)


class AwsExistingAccountAuthorizationValidation(AwsAccountConnectionModel):
    authorization: AwsExistingAccountAuthorization
    caller_arn: str
    node_identity: AwsManagedNodeIdentity


class AwsAccountAuthorizationValidationErrorCode(StrEnum):
    ExternalIdNotEnforced = "external_id_not_enforced"


class AwsAccountAuthorizationValidationError(RuntimeError):
    def __init__(
        self,
        code: AwsAccountAuthorizationValidationErrorCode,
        message: str,
    ) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class AwsAccountAuthorizationCleanupStatus(StrEnum):
    Pending = "pending"
    Verifying = "verifying"
    Complete = "complete"
    ActionRequired = "action_required"


class AwsAccountAuthorizationCleanupResult(AwsAccountConnectionModel):
    status: AwsAccountAuthorizationCleanupStatus
    role_assumable: bool
    stack_exists: bool | None
    stack_status: str = ""
    error_message: str = ""
    console_url: str | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> AwsAccountAuthorizationCleanupResult:
        action_required = self.status is AwsAccountAuthorizationCleanupStatus.ActionRequired
        if action_required != bool(self.error_message):
            raise ValueError("action-required cleanup must include exactly one error message")
        if self.console_url is not None and not action_required:
            raise ValueError("cleanup console URL is only valid when customer action is required")
        if self.status is AwsAccountAuthorizationCleanupStatus.Complete and self.stack_exists:
            raise ValueError("complete cleanup cannot retain a managed stack")
        return self


class _AwsManagedNodeIdentityCleanupResult(AwsAccountConnectionModel):
    complete: bool
    role_exists: bool
    instance_profile_exists: bool


class AwsAccountConnectionAuthorizationRequest(AwsAccountConnectionModel):
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    region: str = Field(pattern=_REGION_PATTERN.pattern)
    platform_principal_arn: str
    external_id: SecretStr = Field(min_length=32, max_length=256, repr=False)
    generation: int = Field(ge=1)
    connection_role_name: str = Field(pattern=_NAME_PATTERN.pattern)
    node_identity: AwsManagedNodeIdentity
    predecessor: AwsActiveAccountAuthorization | None = None

    @field_validator("platform_principal_arn")
    @classmethod
    def validate_platform_principal(cls, value: str) -> str:
        principal = value.strip()
        if not _PRINCIPAL_PATTERN.fullmatch(principal):
            raise ValueError("invalid platform principal ARN")
        return principal

    @field_validator("external_id")
    @classmethod
    def validate_external_id(cls, value: SecretStr) -> SecretStr:
        if not _EXTERNAL_ID_PATTERN.fullmatch(value.get_secret_value()):
            raise ValueError("external ID must contain 32-256 AWS-safe characters")
        return value

    @model_validator(mode="after")
    def validate_generation(self) -> AwsAccountConnectionAuthorizationRequest:
        if self.predecessor is None and self.generation != 1:
            raise ValueError("initial authorization must use generation 1")
        if self.predecessor is not None:
            if self.generation != self.predecessor.generation + 1:
                raise ValueError("replacement must use the next authorization generation")
            if self.account_id != self.predecessor.account_id:
                raise ValueError("replacement account does not match the predecessor")
            if self.region != self.predecessor.region:
                raise ValueError("replacement region does not match the predecessor")
            digest = hashlib.sha256(self.external_id.get_secret_value().encode()).hexdigest()
            if digest != self.predecessor.external_id_sha256:
                raise ValueError("replacement must retain the connection external ID")
            if self.node_identity != self.predecessor.node_identity:
                raise ValueError("replacement must retain the managed node identity")
        return self


class AwsAccountConnectionAuthorizationPlan(AwsAccountConnectionModel):
    active: AwsActiveAccountAuthorization | None = None
    pending: AwsPendingAccountAuthorization
    template_version: str
    template_sha256: str
    authorization_url: str

    @model_validator(mode="after")
    def validate_generations(self) -> AwsAccountConnectionAuthorizationPlan:
        if self.active is None and self.pending.generation != 1:
            raise ValueError("initial authorization plan must create generation 1")
        if self.active is not None:
            if self.pending.generation != self.active.generation + 1:
                raise ValueError("replacement plan must create the next generation")
            if self.pending.account_id != self.active.account_id:
                raise ValueError("replacement plan account does not match active authorization")
            if self.pending.region != self.active.region:
                raise ValueError("replacement plan region does not match active authorization")
            if self.pending.external_id_sha256 != self.active.external_id_sha256:
                raise ValueError("replacement plan must retain the connection external ID")
            if self.pending.node_identity != self.active.node_identity:
                raise ValueError("replacement plan must retain the managed node identity")
            if self.pending.stack_name == self.active.stack_name:
                raise ValueError("replacement plan must create a distinct stack")
            if self.pending.role_arn == self.active.role_arn:
                raise ValueError("replacement plan must create a distinct role")
        return self


class AwsAccountConnectionTarget(AwsAccountConnectionModel):
    account_id: str = Field(pattern=_ACCOUNT_ID_PATTERN.pattern)
    region: str = Field(pattern=_REGION_PATTERN.pattern)
    role_arn: str
    external_id: SecretStr = Field(min_length=32, max_length=256, repr=False)
    node_role_arn: str
    node_instance_profile_arn: str
    vpc_id: str | None = Field(default=None, min_length=1)
    subnet_ids: tuple[str, ...] = ()
    security_group_id: str | None = Field(default=None, min_length=1)

    @field_validator("role_arn", "node_role_arn", "node_instance_profile_arn")
    @classmethod
    def validate_iam_arn(cls, value: str) -> str:
        arn = value.strip()
        if not _ARN_PATTERN.fullmatch(arn):
            raise ValueError("invalid AWS IAM ARN")
        return arn

    @field_validator("external_id")
    @classmethod
    def validate_target_external_id(cls, value: SecretStr) -> SecretStr:
        if not _EXTERNAL_ID_PATTERN.fullmatch(value.get_secret_value()):
            raise ValueError("external ID must contain 32-256 AWS-safe characters")
        return value

    def validated_scope(self) -> Self:
        expected_partition = aws_partition_for_region(self.region)
        expected_kinds = {
            self.role_arn: "role",
            self.node_role_arn: "role",
            self.node_instance_profile_arn: "instance-profile",
        }
        for arn, expected_kind in expected_kinds.items():
            match = _ARN_PATTERN.fullmatch(arn)
            if match is None:
                raise ValueError("invalid AWS IAM ARN")
            partition, account_id, kind, _ = match.groups()
            if partition != expected_partition or account_id != self.account_id:
                raise ValueError("AWS connection resource ARN is outside the account scope")
            if kind != expected_kind:
                raise ValueError(f"AWS connection ARN must identify an IAM {expected_kind}")
        return self


class AwsNodeBucketAccessGrant(AwsAccountConnectionModel):
    bucket: str
    prefix: str = ""
    read_only: bool = False

    @field_validator("bucket")
    @classmethod
    def validate_bucket(cls, value: str) -> str:
        bucket = value.strip()
        if (
            not _S3_BUCKET_PATTERN.fullmatch(bucket)
            or ".." in bucket
            or re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,3}){3}", bucket)
        ):
            raise ValueError("invalid S3 bucket name")
        return bucket

    @field_validator("prefix")
    @classmethod
    def normalize_prefix(cls, value: str) -> str:
        prefix = value.strip().lstrip("/")
        if ".." in prefix.split("/"):
            raise ValueError("bucket prefix cannot contain '..' path segments")
        return prefix.rstrip("/")


class AwsAccountConnectionValidation(AwsAccountConnectionModel):
    account_id: str
    partition: str
    caller_arn: str
    role_arn: str
    node_role_arn: str
    node_instance_profile_arn: str
    validated_at: datetime


class AwsConnectionStsClient(Protocol):
    def assume_role(
        self,
        *,
        RoleArn: str,
        RoleSessionName: str,
        DurationSeconds: int,
        ExternalId: str | None = None,
    ) -> Mapping[str, object]: ...

    def get_caller_identity(self) -> Mapping[str, object]: ...


class AwsConnectionIamClient(Protocol):
    def get_instance_profile(self, *, InstanceProfileName: str) -> Mapping[str, object]: ...

    def get_role(self, *, RoleName: str) -> Mapping[str, object]: ...

    def list_role_policies(self, *, RoleName: str) -> Mapping[str, object]: ...

    def delete_role_policy(self, *, RoleName: str, PolicyName: str) -> Mapping[str, object]: ...

    def put_role_policy(
        self,
        *,
        RoleName: str,
        PolicyName: str,
        PolicyDocument: str,
    ) -> Mapping[str, object]: ...

    def delete_role(self, *, RoleName: str) -> Mapping[str, object]: ...

    def create_role(
        self,
        *,
        RoleName: str,
        AssumeRolePolicyDocument: str,
        Tags: list[dict[str, str]],
    ) -> Mapping[str, object]: ...

    def create_instance_profile(
        self,
        *,
        InstanceProfileName: str,
        Tags: list[dict[str, str]],
    ) -> Mapping[str, object]: ...

    def add_role_to_instance_profile(
        self,
        *,
        InstanceProfileName: str,
        RoleName: str,
    ) -> Mapping[str, object]: ...

    def remove_role_from_instance_profile(
        self,
        *,
        InstanceProfileName: str,
        RoleName: str,
    ) -> Mapping[str, object]: ...

    def delete_instance_profile(
        self,
        *,
        InstanceProfileName: str,
    ) -> Mapping[str, object]: ...


class AwsConnectionEc2Client(Protocol):
    def describe_regions(self, *, RegionNames: list[str]) -> Mapping[str, object]: ...

    def describe_volumes(
        self,
        *,
        Filters: list[dict[str, object]],
        MaxResults: int,
    ) -> Mapping[str, object]: ...


class AwsConnectionCloudFormationClient(Protocol):
    def describe_stacks(self, *, StackName: str) -> Mapping[str, object]: ...

    def delete_stack(
        self,
        *,
        StackName: str,
        ClientRequestToken: str,
    ) -> Mapping[str, object]: ...


class AwsConnectionSession(Protocol):
    @overload
    def client(self, service_name: Literal["sts"]) -> AwsConnectionStsClient: ...

    @overload
    def client(self, service_name: Literal["iam"]) -> AwsConnectionIamClient: ...

    @overload
    def client(self, service_name: Literal["ec2"]) -> AwsConnectionEc2Client: ...

    @overload
    def client(
        self, service_name: Literal["cloudformation"]
    ) -> AwsConnectionCloudFormationClient: ...


class AwsConnectionSessionFactory(Protocol):
    def __call__(
        self,
        *,
        region_name: str,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        aws_session_token: str | None = None,
    ) -> AwsConnectionSession: ...


@dataclass(frozen=True, slots=True)
class _Boto3ConnectionSession:
    session: Session

    @overload
    def client(self, service_name: Literal["sts"]) -> AwsConnectionStsClient: ...

    @overload
    def client(self, service_name: Literal["iam"]) -> AwsConnectionIamClient: ...

    @overload
    def client(self, service_name: Literal["ec2"]) -> AwsConnectionEc2Client: ...

    @overload
    def client(
        self, service_name: Literal["cloudformation"]
    ) -> AwsConnectionCloudFormationClient: ...

    def client(
        self,
        service_name: Literal["sts", "iam", "ec2", "cloudformation"],
    ) -> (
        AwsConnectionStsClient
        | AwsConnectionIamClient
        | AwsConnectionEc2Client
        | AwsConnectionCloudFormationClient
    ):
        source: object = self.session
        if not is_boto3_client_factory(source):
            raise RuntimeError("boto3 session lacks the client factory operation")
        if service_name == "sts":
            candidate = source.client("sts")
            if _is_sts_client(candidate):
                return candidate
        elif service_name == "iam":
            candidate = source.client("iam")
            if _is_iam_client(candidate):
                return candidate
        elif service_name == "ec2":
            candidate = source.client("ec2")
            if _is_ec2_client(candidate):
                return candidate
        else:
            candidate = source.client("cloudformation")
            if _is_cloudformation_client(candidate):
                return candidate
        raise RuntimeError(f"boto3 {service_name} client lacks required operations")


def _is_sts_client(value: object) -> TypeGuard[AwsConnectionStsClient]:
    return has_operations(value, ("assume_role", "get_caller_identity"))


def _is_iam_client(value: object) -> TypeGuard[AwsConnectionIamClient]:
    return has_operations(
        value,
        (
            "add_role_to_instance_profile",
            "create_instance_profile",
            "create_role",
            "delete_instance_profile",
            "delete_role",
            "delete_role_policy",
            "get_instance_profile",
            "get_role",
            "list_role_policies",
            "put_role_policy",
            "remove_role_from_instance_profile",
        ),
    )


def _is_ec2_client(value: object) -> TypeGuard[AwsConnectionEc2Client]:
    return has_operations(value, ("describe_regions",))


def _is_cloudformation_client(
    value: object,
) -> TypeGuard[AwsConnectionCloudFormationClient]:
    return has_operations(value, ("delete_stack", "describe_stacks"))


class _AwsResponseModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _TemporaryCredentials(_AwsResponseModel):
    access_key_id: SecretStr = Field(alias="AccessKeyId")
    secret_access_key: SecretStr = Field(alias="SecretAccessKey")
    session_token: SecretStr = Field(alias="SessionToken")


class _AssumeRoleResponse(_AwsResponseModel):
    credentials: _TemporaryCredentials = Field(alias="Credentials")


class _CallerIdentity(_AwsResponseModel):
    account_id: str = Field(alias="Account")
    arn: str = Field(alias="Arn")


class _IamRole(_AwsResponseModel):
    arn: str = Field(alias="Arn")


class _InstanceProfile(_AwsResponseModel):
    arn: str = Field(alias="Arn")
    roles: tuple[_IamRole, ...] = Field(alias="Roles")


class _GetInstanceProfileResponse(_AwsResponseModel):
    instance_profile: _InstanceProfile = Field(alias="InstanceProfile")


class _Region(_AwsResponseModel):
    name: str = Field(alias="RegionName")


class _DescribeRegionsResponse(_AwsResponseModel):
    regions: tuple[_Region, ...] = Field(alias="Regions")


class _Volume(_AwsResponseModel):
    volume_id: str = Field(alias="VolumeId")


class _DescribeVolumesResponse(_AwsResponseModel):
    volumes: tuple[_Volume, ...] = Field(default=(), alias="Volumes")


class _StackOutput(_AwsResponseModel):
    key: str = Field(alias="OutputKey")
    value: str = Field(alias="OutputValue")


class _Stack(_AwsResponseModel):
    stack_id: str = Field(alias="StackId")
    stack_name: str = Field(alias="StackName")
    status: str = Field(alias="StackStatus")
    outputs: tuple[_StackOutput, ...] = Field(default=(), alias="Outputs")


class _DescribeStacksResponse(_AwsResponseModel):
    stacks: tuple[_Stack, ...] = Field(alias="Stacks")


_READY_AUTHORIZATION_STACK_STATUSES = frozenset({"CREATE_COMPLETE", "UPDATE_COMPLETE"})


class _GetRoleResponse(_AwsResponseModel):
    role: _IamRole = Field(alias="Role")


class _ListRolePoliciesResponse(_AwsResponseModel):
    policy_names: tuple[str, ...] = Field(default=(), alias="PolicyNames")
    truncated: bool = Field(default=False, alias="IsTruncated")


@dataclass(frozen=True, slots=True)
class Boto3AwsAccountConnectionValidator:
    session_factory: AwsConnectionSessionFactory

    @classmethod
    def from_default_chain(cls) -> Self:
        return cls(_default_session)

    def validate(self, target: AwsAccountConnectionTarget) -> AwsAccountConnectionValidation:
        target.validated_scope()
        session = _assumed_connection_session(target, self.session_factory)
        try:
            caller = _validated(
                _CallerIdentity,
                session.client("sts").get_caller_identity(),
                operation="validate account connection identity",
            )
            profile_name = target.node_instance_profile_arn.rsplit("/", maxsplit=1)[-1]
            profile = _validated(
                _GetInstanceProfileResponse,
                session.client("iam").get_instance_profile(InstanceProfileName=profile_name),
                operation="validate shared node instance profile",
            ).instance_profile
            regions = _validate_ec2_inventory_access(session.client("ec2"), target.region)
        except ClientError as exc:
            raise _client_error(exc, operation="validate account connection") from exc
        except BotoCoreError as exc:
            raise upstream_error(exc, operation="validate account connection") from exc
        if caller.account_id != target.account_id:
            raise invalid_response_error(
                "validate account connection identity", "assumed role returned another account"
            )
        if profile.arn != target.node_instance_profile_arn:
            raise invalid_response_error(
                "validate shared node instance profile",
                "AWS returned an instance profile outside the connection scope",
            )
        if tuple(role.arn for role in profile.roles) != (target.node_role_arn,):
            raise invalid_response_error(
                "validate shared node instance profile",
                "shared node instance profile must contain exactly the configured node role",
            )
        if tuple(region.name for region in regions) != (target.region,):
            raise invalid_response_error(
                "validate AWS region access", "AWS region is unavailable to the connection"
            )
        partition = caller.arn.split(":", maxsplit=2)[1]
        return AwsAccountConnectionValidation(
            account_id=caller.account_id,
            partition=partition,
            caller_arn=caller.arn,
            role_arn=target.role_arn,
            node_role_arn=target.node_role_arn,
            node_instance_profile_arn=profile.arn,
            validated_at=datetime.now(UTC),
        )

    def validate_authorization(
        self,
        validation_input: AwsAccountAuthorizationValidationInput,
    ) -> AwsAccountAuthorizationValidation:
        pending = validation_input.pending
        _assert_external_id_enforced(
            pending,
            external_id=validation_input.external_id,
            session_factory=self.session_factory,
        )
        session = _assumed_authorization_session(validation_input, self.session_factory)
        try:
            caller = _validated(
                _CallerIdentity,
                session.client("sts").get_caller_identity(),
                operation="validate authorization identity",
            )
            regions = _validate_ec2_inventory_access(session.client("ec2"), pending.region)
            stacks = _validated(
                _DescribeStacksResponse,
                session.client("cloudformation").describe_stacks(StackName=pending.stack_name),
                operation="validate authorization stack",
            ).stacks
        except ClientError as exc:
            raise _client_error(exc, operation="validate account authorization") from exc
        except BotoCoreError as exc:
            raise upstream_error(exc, operation="validate account authorization") from exc
        if caller.account_id != pending.account_id:
            raise invalid_response_error(
                "validate authorization identity", "assumed role returned another account"
            )
        if tuple(region.name for region in regions) != (pending.region,):
            raise invalid_response_error(
                "validate authorization region access",
                "AWS region is unavailable to the authorization",
            )
        if len(stacks) != 1 or stacks[0].stack_name != pending.stack_name:
            raise invalid_response_error(
                "validate authorization stack",
                "AWS returned a stack outside the pending generation scope",
            )
        stack = stacks[0]
        if stack.status not in _READY_AUTHORIZATION_STACK_STATUSES:
            raise invalid_response_error(
                "validate authorization stack",
                f"authorization stack is not ready ({stack.status})",
            )
        outputs = {output.key: output.value for output in stack.outputs}
        if outputs.get("ConnectionRoleArn") != pending.role_arn:
            raise invalid_response_error(
                "validate authorization stack",
                "authorization stack role does not match the pending generation",
            )
        vpc_id = outputs.get("VpcId", "")
        subnet_ids = tuple(value for value in outputs.get("SubnetIds", "").split(",") if value)
        security_group_id = outputs.get("SecurityGroupId", "")
        if not vpc_id or len(subnet_ids) < 2 or not security_group_id:
            raise invalid_response_error(
                "validate authorization stack",
                "authorization stack did not return the managed network outputs",
            )
        node_identity = _ensure_node_identity(
            session.client("iam"),
            pending.node_identity,
        )
        active = AwsActiveAccountAuthorization(
            account_id=pending.account_id,
            region=pending.region,
            generation=pending.generation,
            stack_name=pending.stack_name,
            role_name=pending.role_name,
            role_arn=pending.role_arn,
            external_id_sha256=pending.external_id_sha256,
            node_identity=pending.node_identity,
            stack_id=stack.stack_id,
            validated_at=datetime.now(UTC),
        )
        return AwsAccountAuthorizationValidation(
            authorization=active,
            caller_arn=caller.arn,
            node_identity=node_identity,
            vpc_id=vpc_id,
            subnet_ids=subnet_ids,
            security_group_id=security_group_id,
        )

    def validate_existing_authorization(
        self,
        validation_input: AwsExistingAccountAuthorizationValidationInput,
    ) -> AwsExistingAccountAuthorizationValidation:
        authorization = validation_input.authorization
        _assert_external_id_enforced(
            authorization,
            external_id=validation_input.external_id,
            session_factory=self.session_factory,
        )
        session = _assumed_active_authorization_session(
            authorization,
            external_id=validation_input.external_id,
            session_factory=self.session_factory,
        )
        try:
            caller = _validated(
                _CallerIdentity,
                session.client("sts").get_caller_identity(),
                operation="validate existing authorization identity",
            )
            regions = _validate_ec2_inventory_access(
                session.client("ec2"),
                authorization.region,
            )
        except ClientError as exc:
            raise _client_error(exc, operation="validate existing account authorization") from exc
        except BotoCoreError as exc:
            raise upstream_error(exc, operation="validate existing account authorization") from exc
        if caller.account_id != authorization.account_id:
            raise invalid_response_error(
                "validate existing authorization identity",
                "assumed role returned another account",
            )
        if tuple(region.name for region in regions) != (authorization.region,):
            raise invalid_response_error(
                "validate existing authorization region access",
                "AWS region is unavailable to the authorization",
            )
        node_identity = _ensure_node_identity(
            session.client("iam"),
            authorization.node_identity,
        )
        return AwsExistingAccountAuthorizationValidation(
            authorization=authorization,
            caller_arn=caller.arn,
            node_identity=node_identity,
        )


def _validate_ec2_inventory_access(
    client: AwsConnectionEc2Client,
    region: str,
) -> tuple[_Region, ...]:
    regions = _validated(
        _DescribeRegionsResponse,
        client.describe_regions(RegionNames=[region]),
        operation="validate AWS region access",
    ).regions
    _validated(
        _DescribeVolumesResponse,
        client.describe_volumes(
            Filters=[
                {
                    "Name": "tag:cloud-pool:managed-by",
                    "Values": ["control-plane"],
                }
            ],
            MaxResults=5,
        ),
        operation="validate managed volume inventory access",
    )
    return regions


@dataclass(frozen=True, slots=True)
class Boto3AwsNodeBucketAccessControl:
    session_factory: AwsConnectionSessionFactory

    @classmethod
    def from_default_chain(cls) -> Self:
        return cls(_default_session)

    def reconcile(
        self,
        target: AwsAccountConnectionTarget,
        grants: Sequence[AwsNodeBucketAccessGrant],
    ) -> None:
        target.validated_scope()
        role_name = target.node_role_arn.rsplit("/", maxsplit=1)[-1]
        iam = _assumed_connection_session(target, self.session_factory).client("iam")
        consolidated = _consolidate_bucket_access_grants(grants)
        try:
            if not consolidated:
                iam.delete_role_policy(
                    RoleName=role_name,
                    PolicyName=_S3_BUCKET_POLICY_NAME,
                )
                return
            policy = aws_node_bucket_access_policy(
                consolidated,
                partition=target.node_role_arn.split(":", maxsplit=2)[1],
            )
            document = json.dumps(policy, separators=(",", ":"), sort_keys=True)
            if len(document.encode()) > _IAM_INLINE_POLICY_MAX_BYTES:
                raise ValueError(
                    "connected AWS bucket access exceeds the IAM inline policy size limit"
                )
            iam.put_role_policy(
                RoleName=role_name,
                PolicyName=_S3_BUCKET_POLICY_NAME,
                PolicyDocument=document,
            )
        except ClientError as exc:
            if not consolidated and _iam_role_not_found(exc):
                return
            raise _client_error(exc, operation="reconcile connected bucket access") from exc
        except BotoCoreError as exc:
            raise upstream_error(exc, operation="reconcile connected bucket access") from exc


@dataclass(frozen=True, slots=True)
class Boto3AwsAccountAuthorizationControl:
    session_factory: AwsConnectionSessionFactory

    @classmethod
    def from_default_chain(cls) -> Self:
        return cls(_default_session)

    def reconcile_authorization_cleanup(
        self,
        authorization: AwsPendingAccountAuthorization | AwsExistingAccountAuthorization,
        *,
        external_id: SecretStr,
        operation_id: str,
        remove_node_identity: bool,
    ) -> AwsAccountAuthorizationCleanupResult:
        if not _CLIENT_REQUEST_TOKEN_PATTERN.fullmatch(operation_id):
            raise ValueError("AWS cleanup operation ID is not a valid ClientRequestToken")
        session = _assumed_authorization_session_if_available(
            authorization,
            external_id=external_id,
            session_factory=self.session_factory,
        )
        if session is None:
            managed_was_validated = isinstance(authorization, AwsActiveAccountAuthorization)
            return AwsAccountAuthorizationCleanupResult(
                status=(
                    AwsAccountAuthorizationCleanupStatus.Complete
                    if managed_was_validated
                    or isinstance(authorization, AwsExistingAccountAuthorization)
                    else AwsAccountAuthorizationCleanupStatus.Verifying
                ),
                role_assumable=False,
                stack_exists=(
                    False
                    if isinstance(
                        authorization,
                        (AwsActiveAccountAuthorization, AwsExistingAccountAuthorization),
                    )
                    else None
                ),
            )

        if remove_node_identity:
            node_cleanup = _delete_node_identity(
                session.client("iam"),
                authorization.node_identity,
            )
            if not node_cleanup.complete:
                return AwsAccountAuthorizationCleanupResult(
                    status=AwsAccountAuthorizationCleanupStatus.Pending,
                    role_assumable=True,
                    stack_exists=None,
                )

        if isinstance(authorization, AwsExistingAccountAuthorization):
            if not remove_node_identity:
                return AwsAccountAuthorizationCleanupResult(
                    status=AwsAccountAuthorizationCleanupStatus.Complete,
                    role_assumable=True,
                    stack_exists=False,
                )
            return AwsAccountAuthorizationCleanupResult(
                status=AwsAccountAuthorizationCleanupStatus.ActionRequired,
                role_assumable=True,
                stack_exists=False,
                error_message="Revoke or delete the existing AWS authorization role.",
            )

        stack_target = (
            authorization.stack_id
            if isinstance(authorization, AwsActiveAccountAuthorization)
            else authorization.stack_name
        )
        cloudformation = session.client("cloudformation")
        stack = _describe_authorization_stack_if_present(
            cloudformation,
            authorization=authorization,
            stack_target=stack_target,
        )
        if stack is None:
            return AwsAccountAuthorizationCleanupResult(
                status=AwsAccountAuthorizationCleanupStatus.Verifying,
                role_assumable=True,
                stack_exists=False,
            )
        if stack.status == "DELETE_FAILED":
            return AwsAccountAuthorizationCleanupResult(
                status=AwsAccountAuthorizationCleanupStatus.ActionRequired,
                role_assumable=True,
                stack_exists=True,
                stack_status=stack.status,
                error_message="AWS could not delete the managed authorization stack.",
                console_url=_authorization_stack_console_url(
                    region=authorization.region,
                    stack_id=stack.stack_id,
                ),
            )
        if stack.status == "DELETE_IN_PROGRESS":
            return AwsAccountAuthorizationCleanupResult(
                status=AwsAccountAuthorizationCleanupStatus.Pending,
                role_assumable=True,
                stack_exists=True,
                stack_status=stack.status,
            )
        try:
            cloudformation.delete_stack(
                StackName=stack.stack_id,
                ClientRequestToken=operation_id,
            )
        except ClientError as exc:
            if _cloudformation_stack_not_found(exc):
                return AwsAccountAuthorizationCleanupResult(
                    status=AwsAccountAuthorizationCleanupStatus.Verifying,
                    role_assumable=True,
                    stack_exists=False,
                )
            error = _client_error(exc, operation="delete managed authorization stack")
            if error.code is AwsProviderControlErrorCode.PermissionDenied:
                return AwsAccountAuthorizationCleanupResult(
                    status=AwsAccountAuthorizationCleanupStatus.ActionRequired,
                    role_assumable=True,
                    stack_exists=True,
                    stack_status=stack.status,
                    error_message=error.detail,
                    console_url=_authorization_stack_console_url(
                        region=authorization.region,
                        stack_id=stack.stack_id,
                    ),
                )
            raise error from exc
        except BotoCoreError as exc:
            raise upstream_error(exc, operation="delete managed authorization stack") from exc
        return AwsAccountAuthorizationCleanupResult(
            status=AwsAccountAuthorizationCleanupStatus.Pending,
            role_assumable=True,
            stack_exists=True,
            stack_status="DELETE_IN_PROGRESS",
        )


@dataclass(frozen=True, slots=True)
class AwsAccountConnectionPlanner:
    platform_principal_arn: str
    publication: AwsAccountConnectionTemplatePublication
    region: str = "us-east-1"

    def plan_initial(
        self,
        *,
        user_id: str,
        connection_id: str,
        account_id: str,
        external_id: SecretStr,
    ) -> AwsAccountConnectionAuthorizationPlan:
        return self._plan(
            user_id=user_id,
            connection_id=connection_id,
            account_id=account_id,
            external_id=external_id,
            generation=1,
            active=None,
        )

    def plan_replacement(
        self,
        *,
        user_id: str,
        connection_id: str,
        account_id: str,
        external_id: SecretStr,
        active: AwsActiveAccountAuthorization,
    ) -> AwsAccountConnectionAuthorizationPlan:
        return self._plan(
            user_id=user_id,
            connection_id=connection_id,
            account_id=account_id,
            external_id=external_id,
            generation=active.generation + 1,
            active=active,
        )

    def plan_existing_role(
        self,
        *,
        user_id: str,
        connection_id: str,
        account_id: str,
        role_arn: str,
        external_id: SecretStr,
    ) -> AwsExistingAccountAuthorization:
        owner = user_id.strip()
        connection = connection_id.strip()
        if not owner or not connection:
            raise ValueError("user ID and connection ID are required")
        suffix = _connection_resource_suffix(owner, connection)
        raw_external_id = external_id.get_secret_value()
        if not _EXTERNAL_ID_PATTERN.fullmatch(raw_external_id):
            raise ValueError("external ID must contain 32-256 AWS-safe characters")
        return AwsExistingAccountAuthorization(
            account_id=account_id,
            region=self.region,
            role_arn=role_arn,
            external_id_sha256=hashlib.sha256(raw_external_id.encode()).hexdigest(),
            node_identity=_managed_node_identity(
                account_id=account_id,
                region=self.region,
                suffix=suffix,
            ),
        )

    def _plan(
        self,
        *,
        user_id: str,
        connection_id: str,
        account_id: str,
        external_id: SecretStr,
        generation: int,
        active: AwsActiveAccountAuthorization | None,
    ) -> AwsAccountConnectionAuthorizationPlan:
        owner = user_id.strip()
        connection = connection_id.strip()
        if not owner or not connection:
            raise ValueError("user ID and connection ID are required")
        if not _ACCOUNT_ID_PATTERN.fullmatch(account_id):
            raise ValueError("AWS account ID must contain exactly 12 digits")
        suffix = _connection_resource_suffix(owner, connection)
        role_name = f"compute-connection-{suffix}-g{generation}"
        node_identity = _managed_node_identity(
            account_id=account_id,
            region=self.region,
            suffix=suffix,
        )
        return plan_aws_account_connection_authorization(
            AwsAccountConnectionAuthorizationRequest(
                account_id=account_id,
                region=self.region,
                platform_principal_arn=self.platform_principal_arn,
                external_id=external_id,
                generation=generation,
                connection_role_name=role_name,
                node_identity=node_identity,
                predecessor=active,
            ),
            publication=self.publication,
        )


def aws_account_connection_template_bytes() -> bytes:
    template = _connection_template()
    return (json.dumps(template, sort_keys=True, separators=(",", ":")) + "\n").encode()


def require_resolvable_aws_credentials() -> None:
    """Refuse a connected deployment whose credentials resolve to nothing.

    Without this a stack with no AWS configuration mounted starts, reports
    healthy, and cannot validate a single connection — surfacing much later as a
    connection stuck in `awaiting_authorization`, which names nothing about
    credentials. Resolution is deferred, so a role chain costs no call here.
    """
    try:
        credentials = Session().get_credentials()
    except BotoCoreError as exc:
        raise ValueError(f"connected AWS credentials are unavailable: {exc}") from exc
    if credentials is None:
        raise ValueError(
            "connected AWS is enabled but no credentials resolve; mount the role-chain "
            "configuration directory with LAZYCLOUD_COMPOSE_AWS_CONFIG_DIR"
        )


def aws_account_connection_template_identity() -> AwsAccountConnectionTemplateIdentity:
    payload = aws_account_connection_template_bytes()
    validate_aws_account_connection_template_policy(payload)
    return AwsAccountConnectionTemplateIdentity(
        version=AWS_ACCOUNT_CONNECTION_TEMPLATE_VERSION,
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_length=len(payload),
    )


def plan_aws_account_connection_authorization(
    request: AwsAccountConnectionAuthorizationRequest,
    *,
    publication: AwsAccountConnectionTemplatePublication,
) -> AwsAccountConnectionAuthorizationPlan:
    identity = aws_account_connection_template_identity()
    if publication.sha256 != identity.sha256:
        raise ValueError("published connection template digest does not match the bundled template")
    partition = aws_partition_for_region(request.region)
    principal_partition = request.platform_principal_arn.split(":", maxsplit=2)[1]
    if principal_partition != partition:
        raise ValueError("platform principal partition does not match the selected region")
    prefix = f"arn:{partition}:iam::{request.account_id}"
    parameters = {
        "ConnectionRoleName": request.connection_role_name,
        "ExternalId": request.external_id.get_secret_value(),
        "NodeInstanceProfileName": request.node_identity.instance_profile_name,
        "NodeRoleName": request.node_identity.role_name,
        "PlatformPrincipalArn": request.platform_principal_arn,
        "TargetAccountId": request.account_id,
    }
    stack_name = request.connection_role_name
    query = [("stackName", stack_name), ("templateURL", publication.url)]
    query.extend((f"param_{key}", value) for key, value in sorted(parameters.items()))
    authorization_url = (
        f"https://{aws_console_host(request.region)}/cloudformation/home?"
        f"{urlencode({'region': request.region}, quote_via=quote, safe='')}"
        f"#/stacks/create/review?{urlencode(query, quote_via=quote, safe='')}"
    )
    pending = AwsPendingAccountAuthorization(
        account_id=request.account_id,
        region=request.region,
        generation=request.generation,
        stack_name=stack_name,
        role_name=request.connection_role_name,
        role_arn=f"{prefix}:role/{request.connection_role_name}",
        external_id_sha256=hashlib.sha256(
            request.external_id.get_secret_value().encode()
        ).hexdigest(),
        node_identity=request.node_identity,
    )
    return AwsAccountConnectionAuthorizationPlan(
        active=request.predecessor,
        pending=pending,
        template_version=identity.version,
        template_sha256=identity.sha256,
        authorization_url=authorization_url,
    )


def _connection_template() -> dict[str, object]:
    ec2_managed_tag = {"StringEquals": {"ec2:ResourceTag/cloud-pool:managed-by": "control-plane"}}
    request_tag = {"StringEquals": {"aws:RequestTag/cloud-pool:managed-by": "control-plane"}}
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "One-time authorization for customer-owned compute management.",
        "Parameters": {
            "TargetAccountId": {"Type": "String", "AllowedPattern": "^[0-9]{12}$"},
            "PlatformPrincipalArn": {"Type": "String"},
            "ExternalId": {"Type": "String", "MinLength": 32},
            "ConnectionRoleName": {"Type": "String"},
            "NodeRoleName": {"Type": "String"},
            "NodeInstanceProfileName": {"Type": "String"},
        },
        "Rules": {
            "TargetAccountMustMatch": {
                "Assertions": [
                    {
                        "Assert": {
                            "Fn::Equals": [{"Ref": "TargetAccountId"}, {"Ref": "AWS::AccountId"}]
                        },
                        "AssertDescription": "Open this authorization in the selected AWS account.",
                    }
                ]
            }
        },
        "Resources": {
            "Vpc": {
                "Type": "AWS::EC2::VPC",
                "Properties": {
                    "CidrBlock": "10.86.0.0/16",
                    "EnableDnsSupport": True,
                    "EnableDnsHostnames": True,
                    "Tags": _network_resource_tags("vpc"),
                },
            },
            "InternetGateway": {
                "Type": "AWS::EC2::InternetGateway",
                "Properties": {"Tags": _network_resource_tags("internet-gateway")},
            },
            "VpcGatewayAttachment": {
                "Type": "AWS::EC2::VPCGatewayAttachment",
                "Properties": {
                    "VpcId": {"Ref": "Vpc"},
                    "InternetGatewayId": {"Ref": "InternetGateway"},
                },
            },
            "RouteTable": {
                "Type": "AWS::EC2::RouteTable",
                "Properties": {
                    "VpcId": {"Ref": "Vpc"},
                    "Tags": _network_resource_tags("route-table"),
                },
            },
            "DefaultRoute": {
                "Type": "AWS::EC2::Route",
                "DependsOn": "VpcGatewayAttachment",
                "Properties": {
                    "RouteTableId": {"Ref": "RouteTable"},
                    "DestinationCidrBlock": "0.0.0.0/0",
                    "GatewayId": {"Ref": "InternetGateway"},
                },
            },
            "SubnetA": _public_subnet_resource("subnet-a", cidr="10.86.1.0/24", zone_index=0),
            "SubnetB": _public_subnet_resource("subnet-b", cidr="10.86.2.0/24", zone_index=1),
            "SubnetARouteTableAssociation": {
                "Type": "AWS::EC2::SubnetRouteTableAssociation",
                "Properties": {
                    "SubnetId": {"Ref": "SubnetA"},
                    "RouteTableId": {"Ref": "RouteTable"},
                },
            },
            "SubnetBRouteTableAssociation": {
                "Type": "AWS::EC2::SubnetRouteTableAssociation",
                "Properties": {
                    "SubnetId": {"Ref": "SubnetB"},
                    "RouteTableId": {"Ref": "RouteTable"},
                },
            },
            "NodeSecurityGroup": {
                "Type": "AWS::EC2::SecurityGroup",
                "Properties": {
                    "GroupDescription": "Customer-owned compute nodes",
                    "VpcId": {"Ref": "Vpc"},
                    "SecurityGroupEgress": [
                        {
                            "IpProtocol": "-1",
                            "CidrIp": "0.0.0.0/0",
                            "Description": "Outbound node traffic",
                        }
                    ],
                    "Tags": _network_resource_tags("security-group"),
                },
            },
            "ConnectionRole": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "RoleName": {"Ref": "ConnectionRoleName"},
                    "Tags": [
                        {
                            "Key": "cloud-pool:managed-by",
                            "Value": "control-plane",
                        }
                    ],
                    "AssumeRolePolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Principal": {"AWS": {"Ref": "PlatformPrincipalArn"}},
                                "Action": "sts:AssumeRole",
                                "Condition": {
                                    "StringEquals": {"sts:ExternalId": {"Ref": "ExternalId"}}
                                },
                            }
                        ],
                    },
                    "Policies": [
                        {
                            "PolicyName": "managed-compute-control",
                            "PolicyDocument": {
                                "Version": "2012-10-17",
                                "Statement": [
                                    {
                                        "Sid": "Inventory",
                                        "Effect": "Allow",
                                        "Action": [
                                            "autoscaling:DescribeAutoScalingGroups",
                                            "ec2:DescribeAvailabilityZones",
                                            "ec2:DescribeInstances",
                                            "ec2:DescribeInternetGateways",
                                            "ec2:DescribeLaunchTemplates",
                                            "ec2:DescribeLaunchTemplateVersions",
                                            "ec2:DescribeRegions",
                                            "ec2:DescribeRouteTables",
                                            "ec2:DescribeSecurityGroups",
                                            "ec2:DescribeSubnets",
                                            "ec2:DescribeVolumes",
                                            "ec2:DescribeVpcs",
                                            "sts:GetCallerIdentity",
                                        ],
                                        "Resource": "*",
                                    },
                                    {
                                        "Sid": "ManageCurrentAuthorization",
                                        "Effect": "Allow",
                                        "Action": [
                                            "cloudformation:DeleteStack",
                                            "cloudformation:DescribeStackEvents",
                                            "cloudformation:DescribeStacks",
                                        ],
                                        "Resource": {"Ref": "AWS::StackId"},
                                    },
                                    {
                                        "Sid": "CreateTaggedLaunchTemplates",
                                        "Effect": "Allow",
                                        "Action": "ec2:CreateLaunchTemplate",
                                        "Resource": "*",
                                        "Condition": request_tag,
                                    },
                                    {
                                        "Sid": "TagOnlyDuringOwnedCreate",
                                        "Effect": "Allow",
                                        "Action": "ec2:CreateTags",
                                        "Resource": "*",
                                        "Condition": {
                                            "StringEquals": {
                                                "aws:RequestTag/cloud-pool:managed-by": (
                                                    "control-plane"
                                                ),
                                                "ec2:CreateAction": "CreateLaunchTemplate",
                                            }
                                        },
                                    },
                                    {
                                        "Sid": "CreateTaggedAutoScalingGroups",
                                        "Effect": "Allow",
                                        "Action": "autoscaling:CreateAutoScalingGroup",
                                        "Resource": "*",
                                        "Condition": request_tag,
                                    },
                                    {
                                        "Sid": "ManageTaggedLaunchTemplates",
                                        "Effect": "Allow",
                                        "Action": [
                                            "ec2:CreateLaunchTemplateVersion",
                                            "ec2:DeleteLaunchTemplate",
                                            "ec2:ModifyLaunchTemplate",
                                        ],
                                        "Resource": "*",
                                        "Condition": ec2_managed_tag,
                                    },
                                    {
                                        "Sid": "ManageTaggedAutoScalingGroups",
                                        "Effect": "Allow",
                                        "Action": [
                                            "autoscaling:DeleteAutoScalingGroup",
                                            "autoscaling:SetDesiredCapacity",
                                            "autoscaling:TerminateInstanceInAutoScalingGroup",
                                            "autoscaling:UpdateAutoScalingGroup",
                                        ],
                                        "Resource": "*",
                                        "Condition": {
                                            "StringEquals": {
                                                "autoscaling:ResourceTag/"
                                                "cloud-pool:managed-by": "control-plane"
                                            }
                                        },
                                    },
                                    {
                                        "Sid": "RunTaggedInstanceResources",
                                        "Effect": "Allow",
                                        "Action": "ec2:RunInstances",
                                        "Resource": [
                                            {
                                                "Fn::Sub": (
                                                    "arn:${AWS::Partition}:ec2:${AWS::Region}:"
                                                    "${AWS::AccountId}:instance/*"
                                                )
                                            },
                                            {
                                                "Fn::Sub": (
                                                    "arn:${AWS::Partition}:ec2:${AWS::Region}:"
                                                    "${AWS::AccountId}:volume/*"
                                                )
                                            },
                                        ],
                                        "Condition": request_tag,
                                    },
                                    {
                                        "Sid": "UseManagedInstanceLaunchResources",
                                        "Effect": "Allow",
                                        "Action": "ec2:RunInstances",
                                        "Resource": [
                                            {
                                                "Fn::Sub": (
                                                    "arn:${AWS::Partition}:ec2:${AWS::Region}:"
                                                    "${AWS::AccountId}:launch-template/*"
                                                )
                                            },
                                            {
                                                "Fn::Sub": (
                                                    "arn:${AWS::Partition}:ec2:${AWS::Region}:"
                                                    "${AWS::AccountId}:security-group/*"
                                                )
                                            },
                                            {
                                                "Fn::Sub": (
                                                    "arn:${AWS::Partition}:ec2:${AWS::Region}:"
                                                    "${AWS::AccountId}:subnet/*"
                                                )
                                            },
                                        ],
                                        "Condition": ec2_managed_tag,
                                    },
                                    {
                                        "Sid": "UseRegionalImagesForInstances",
                                        "Effect": "Allow",
                                        "Action": "ec2:RunInstances",
                                        "Resource": {
                                            "Fn::Sub": (
                                                "arn:${AWS::Partition}:ec2:${AWS::Region}:*:image/*"
                                            )
                                        },
                                    },
                                    {
                                        "Sid": "CreateInstanceNetworkInterfaces",
                                        "Effect": "Allow",
                                        "Action": "ec2:RunInstances",
                                        "Resource": {
                                            "Fn::Sub": (
                                                "arn:${AWS::Partition}:ec2:${AWS::Region}:"
                                                "${AWS::AccountId}:network-interface/*"
                                            )
                                        },
                                    },
                                    {
                                        "Sid": "TagManagedInstancesOnLaunch",
                                        "Effect": "Allow",
                                        "Action": "ec2:CreateTags",
                                        "Resource": [
                                            {
                                                "Fn::Sub": (
                                                    "arn:${AWS::Partition}:ec2:${AWS::Region}:"
                                                    "${AWS::AccountId}:instance/*"
                                                )
                                            },
                                            {
                                                "Fn::Sub": (
                                                    "arn:${AWS::Partition}:ec2:${AWS::Region}:"
                                                    "${AWS::AccountId}:volume/*"
                                                )
                                            },
                                        ],
                                        "Condition": {
                                            "StringEquals": {
                                                "aws:RequestTag/cloud-pool:managed-by": (
                                                    "control-plane"
                                                ),
                                                "ec2:CreateAction": "RunInstances",
                                            }
                                        },
                                    },
                                    {
                                        "Sid": "CreateAutoScalingServiceRole",
                                        "Effect": "Allow",
                                        "Action": "iam:CreateServiceLinkedRole",
                                        "Resource": {
                                            "Fn::Sub": (
                                                "arn:${AWS::Partition}:iam::*:role/aws-service-role/"
                                                "autoscaling.amazonaws.com/"
                                                "AWSServiceRoleForAutoScaling"
                                            )
                                        },
                                        "Condition": {
                                            "StringEquals": {
                                                "iam:AWSServiceName": "autoscaling.amazonaws.com"
                                            }
                                        },
                                    },
                                    {
                                        "Sid": "CreateManagedNodeIdentity",
                                        "Effect": "Allow",
                                        "Action": [
                                            "iam:CreateInstanceProfile",
                                            "iam:CreateRole",
                                        ],
                                        "Resource": [
                                            {
                                                "Fn::Sub": (
                                                    "arn:${AWS::Partition}:iam::"
                                                    "${AWS::AccountId}:instance-profile/"
                                                    "${NodeInstanceProfileName}"
                                                )
                                            },
                                            {
                                                "Fn::Sub": (
                                                    "arn:${AWS::Partition}:iam::"
                                                    "${AWS::AccountId}:role/${NodeRoleName}"
                                                )
                                            },
                                        ],
                                        "Condition": request_tag,
                                    },
                                    {
                                        "Sid": "ManageOwnedNodeIdentity",
                                        "Effect": "Allow",
                                        "Action": [
                                            "iam:AddRoleToInstanceProfile",
                                            "iam:DeleteInstanceProfile",
                                            "iam:DeleteRole",
                                            "iam:DeleteRolePolicy",
                                            "iam:GetInstanceProfile",
                                            "iam:GetRole",
                                            "iam:ListRolePolicies",
                                            "iam:PutRolePolicy",
                                            "iam:RemoveRoleFromInstanceProfile",
                                            "iam:TagInstanceProfile",
                                            "iam:TagRole",
                                            "iam:UntagInstanceProfile",
                                            "iam:UntagRole",
                                        ],
                                        "Resource": [
                                            {
                                                "Fn::Sub": (
                                                    "arn:${AWS::Partition}:iam::"
                                                    "${AWS::AccountId}:instance-profile/"
                                                    "${NodeInstanceProfileName}"
                                                )
                                            },
                                            {
                                                "Fn::Sub": (
                                                    "arn:${AWS::Partition}:iam::"
                                                    "${AWS::AccountId}:role/${NodeRoleName}"
                                                )
                                            },
                                        ],
                                    },
                                    {
                                        "Sid": "PassOwnedNodeRole",
                                        "Effect": "Allow",
                                        "Action": "iam:PassRole",
                                        "Resource": {
                                            "Fn::Sub": (
                                                "arn:${AWS::Partition}:iam::"
                                                "${AWS::AccountId}:role/${NodeRoleName}"
                                            )
                                        },
                                        "Condition": {
                                            "StringEquals": {
                                                "iam:PassedToService": "ec2.amazonaws.com",
                                            }
                                        },
                                    },
                                ],
                            },
                        }
                    ],
                },
            },
        },
        "Outputs": {
            "ConnectionRoleArn": {"Value": {"Fn::GetAtt": ["ConnectionRole", "Arn"]}},
            "VpcId": {"Value": {"Ref": "Vpc"}},
            "SubnetIds": {"Value": {"Fn::Join": [",", [{"Ref": "SubnetA"}, {"Ref": "SubnetB"}]]}},
            "SecurityGroupId": {"Value": {"Fn::GetAtt": ["NodeSecurityGroup", "GroupId"]}},
        },
    }


def _network_resource_tags(resource: str) -> list[dict[str, object]]:
    return [
        {"Key": "Name", "Value": {"Fn::Sub": f"${{AWS::StackName}}-{resource}"}},
        {"Key": "cloud-pool:managed-by", "Value": "control-plane"},
    ]


def _public_subnet_resource(resource: str, *, cidr: str, zone_index: int) -> dict[str, object]:
    return {
        "Type": "AWS::EC2::Subnet",
        "Properties": {
            "VpcId": {"Ref": "Vpc"},
            "CidrBlock": cidr,
            "MapPublicIpOnLaunch": True,
            "AvailabilityZone": {"Fn::Select": [zone_index, {"Fn::GetAZs": ""}]},
            "Tags": _network_resource_tags(resource),
        },
    }


def _connection_session_name(target: AwsAccountConnectionTarget) -> str:
    digest = hashlib.sha256(f"{target.account_id}\0{target.role_arn}".encode()).hexdigest()[:16]
    return f"account-connection-{digest}"


def _authorization_session_name(
    authorization: (AwsPendingAccountAuthorization | AwsExistingAccountAuthorization),
) -> str:
    digest = hashlib.sha256(
        f"{authorization.account_id}\0{authorization.role_arn}".encode()
    ).hexdigest()[:16]
    return f"account-authorization-{digest}"


def _assert_external_id_enforced(
    authorization: AwsPendingAccountAuthorization | AwsExistingAccountAuthorization,
    *,
    external_id: SecretStr,
    session_factory: AwsConnectionSessionFactory,
) -> None:
    _validate_external_id(authorization, external_id)
    source = session_factory(region_name=authorization.region)
    attempts: tuple[str | None, ...] = (
        None,
        f"incorrect-{authorization.external_id_sha256[:32]}",
    )
    for attempted_external_id in attempts:
        try:
            if attempted_external_id is None:
                source.client("sts").assume_role(
                    RoleArn=authorization.role_arn,
                    RoleSessionName=_authorization_session_name(authorization),
                    DurationSeconds=900,
                )
            else:
                source.client("sts").assume_role(
                    RoleArn=authorization.role_arn,
                    RoleSessionName=_authorization_session_name(authorization),
                    ExternalId=attempted_external_id,
                    DurationSeconds=900,
                )
        except ClientError as exc:
            error = exc.response.get("Error", {})
            code = str(error.get("Code", "")) if isinstance(error, Mapping) else ""
            if code.casefold() in {"accessdenied", "accessdeniedexception"}:
                continue
            raise _client_error(exc, operation="verify authorization external ID") from exc
        except BotoCoreError as exc:
            raise upstream_error(exc, operation="verify authorization external ID") from exc
        raise AwsAccountAuthorizationValidationError(
            AwsAccountAuthorizationValidationErrorCode.ExternalIdNotEnforced,
            "AWS authorization role accepted AssumeRole without the exact external ID",
        )


_NODE_DIAGNOSTICS_POLICY_NAME = "managed-node-diagnostics"
_NODE_DIAGNOSTICS_POLICY = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "SystemsManagerAgent",
                "Effect": "Allow",
                "Action": [
                    "ssm:DescribeAssociation",
                    "ssm:DescribeDocument",
                    "ssm:GetDocument",
                    "ssm:GetManifest",
                    "ssm:ListAssociations",
                    "ssm:ListInstanceAssociations",
                    "ssm:PutComplianceItems",
                    "ssm:PutInventory",
                    "ssm:UpdateAssociationStatus",
                    "ssm:UpdateInstanceAssociationStatus",
                    "ssm:UpdateInstanceInformation",
                ],
                "Resource": "*",
            },
            {
                "Sid": "SystemsManagerChannels",
                "Effect": "Allow",
                "Action": [
                    "ec2messages:AcknowledgeMessage",
                    "ec2messages:DeleteMessage",
                    "ec2messages:FailMessage",
                    "ec2messages:GetEndpoint",
                    "ec2messages:GetMessages",
                    "ec2messages:SendReply",
                    "ssmmessages:CreateControlChannel",
                    "ssmmessages:CreateDataChannel",
                    "ssmmessages:OpenControlChannel",
                    "ssmmessages:OpenDataChannel",
                ],
                "Resource": "*",
            },
        ],
    },
    sort_keys=True,
    separators=(",", ":"),
)


def _ensure_node_diagnostics_policy(
    client: AwsConnectionIamClient,
    *,
    role_name: str,
) -> None:
    """Grant the node role Systems Manager access so machines stay diagnosable.

    A machine whose worker fails before it can report is otherwise a black box:
    the platform can only observe that it never became ready. SSM gives the
    operator on-machine inspection without opening SSH or a key pair, and the
    agent ships with Amazon Linux.
    """
    try:
        client.put_role_policy(
            RoleName=role_name,
            PolicyName=_NODE_DIAGNOSTICS_POLICY_NAME,
            PolicyDocument=_NODE_DIAGNOSTICS_POLICY,
        )
    except ClientError as exc:
        raise _client_error(exc, operation="ensure managed node diagnostics policy") from exc
    except BotoCoreError as exc:
        raise upstream_error(exc, operation="ensure managed node diagnostics policy") from exc


def _ensure_node_identity(
    client: AwsConnectionIamClient,
    identity: AwsManagedNodeIdentity,
) -> AwsManagedNodeIdentity:
    role = _get_role_if_present(client, role_name=identity.role_name)
    if role is None:
        trust = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "ec2.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                    }
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            role = _validated(
                _GetRoleResponse,
                client.create_role(
                    RoleName=identity.role_name,
                    AssumeRolePolicyDocument=trust,
                    Tags=[{"Key": "cloud-pool:managed-by", "Value": "control-plane"}],
                ),
                operation="create managed node role",
            ).role
        except ClientError as exc:
            raise _client_error(exc, operation="create managed node role") from exc
        except BotoCoreError as exc:
            raise upstream_error(exc, operation="create managed node role") from exc
    if role.arn != identity.role_arn:
        raise invalid_response_error(
            "ensure managed node role", "AWS returned a role outside connection scope"
        )
    _ensure_node_diagnostics_policy(client, role_name=identity.role_name)

    profile = _get_instance_profile_if_present(
        client,
        profile_name=identity.instance_profile_name,
    )
    if profile is None:
        try:
            profile = _validated(
                _GetInstanceProfileResponse,
                client.create_instance_profile(
                    InstanceProfileName=identity.instance_profile_name,
                    Tags=[{"Key": "cloud-pool:managed-by", "Value": "control-plane"}],
                ),
                operation="create managed node instance profile",
            ).instance_profile
        except ClientError as exc:
            raise _client_error(exc, operation="create managed node instance profile") from exc
        except BotoCoreError as exc:
            raise upstream_error(exc, operation="create managed node instance profile") from exc
    if profile.arn != identity.instance_profile_arn:
        raise invalid_response_error(
            "ensure managed node instance profile",
            "AWS returned an instance profile outside connection scope",
        )
    role_arns = tuple(item.arn for item in profile.roles)
    if not role_arns:
        try:
            client.add_role_to_instance_profile(
                InstanceProfileName=identity.instance_profile_name,
                RoleName=identity.role_name,
            )
        except ClientError as exc:
            raise _client_error(exc, operation="attach managed node role") from exc
        except BotoCoreError as exc:
            raise upstream_error(exc, operation="attach managed node role") from exc
        profile = _get_instance_profile_if_present(
            client,
            profile_name=identity.instance_profile_name,
        )
        if profile is None:
            raise invalid_response_error(
                "ensure managed node instance profile",
                "managed node profile was not observable after role attachment",
            )
        role_arns = tuple(item.arn for item in profile.roles)
    if role_arns != (identity.role_arn,):
        raise invalid_response_error(
            "ensure managed node instance profile",
            "managed node instance profile contains another role",
        )
    return identity


def _delete_node_identity(
    client: AwsConnectionIamClient,
    identity: AwsManagedNodeIdentity,
) -> _AwsManagedNodeIdentityCleanupResult:
    profile = _get_instance_profile_if_present(
        client,
        profile_name=identity.instance_profile_name,
    )
    try:
        if profile is not None:
            for role in profile.roles:
                if role.arn != identity.role_arn:
                    raise invalid_response_error(
                        "delete managed node identity",
                        "managed node profile contains another role",
                    )
                client.remove_role_from_instance_profile(
                    InstanceProfileName=identity.instance_profile_name,
                    RoleName=identity.role_name,
                )
            client.delete_instance_profile(
                InstanceProfileName=identity.instance_profile_name,
            )
        if _get_role_if_present(client, role_name=identity.role_name) is not None:
            _delete_role(client, role_name=identity.role_name)
    except ClientError as exc:
        if not _iam_role_not_found(exc):
            raise _client_error(exc, operation="delete managed node identity") from exc
    except BotoCoreError as exc:
        raise upstream_error(exc, operation="delete managed node identity") from exc
    profile_exists = (
        _get_instance_profile_if_present(
            client,
            profile_name=identity.instance_profile_name,
        )
        is not None
    )
    role_exists = _get_role_if_present(client, role_name=identity.role_name) is not None
    return _AwsManagedNodeIdentityCleanupResult(
        complete=not profile_exists and not role_exists,
        role_exists=role_exists,
        instance_profile_exists=profile_exists,
    )


def _get_role_if_present(
    client: AwsConnectionIamClient,
    *,
    role_name: str,
) -> _IamRole | None:
    try:
        return _validated(
            _GetRoleResponse,
            client.get_role(RoleName=role_name),
            operation="get managed IAM role",
        ).role
    except ClientError as exc:
        if _iam_role_not_found(exc):
            return None
        raise _client_error(exc, operation="get managed IAM role") from exc
    except BotoCoreError as exc:
        raise upstream_error(exc, operation="get managed IAM role") from exc


def _get_instance_profile_if_present(
    client: AwsConnectionIamClient,
    *,
    profile_name: str,
) -> _InstanceProfile | None:
    try:
        return _validated(
            _GetInstanceProfileResponse,
            client.get_instance_profile(InstanceProfileName=profile_name),
            operation="get managed node instance profile",
        ).instance_profile
    except ClientError as exc:
        if _iam_role_not_found(exc):
            return None
        raise _client_error(exc, operation="get managed node instance profile") from exc
    except BotoCoreError as exc:
        raise upstream_error(exc, operation="get managed node instance profile") from exc


def _assumed_connection_session(
    target: AwsAccountConnectionTarget,
    session_factory: AwsConnectionSessionFactory,
) -> AwsConnectionSession:
    target.validated_scope()
    source = session_factory(region_name=target.region)
    try:
        assumed = source.client("sts").assume_role(
            RoleArn=target.role_arn,
            RoleSessionName=_connection_session_name(target),
            ExternalId=target.external_id.get_secret_value(),
            DurationSeconds=3600,
        )
    except ClientError as exc:
        raise _client_error(exc, operation="assume account connection role") from exc
    except BotoCoreError as exc:
        raise upstream_error(exc, operation="assume account connection role") from exc
    credentials = _validated(
        _AssumeRoleResponse,
        assumed,
        operation="assume account connection role",
    ).credentials
    return session_factory(
        region_name=target.region,
        aws_access_key_id=credentials.access_key_id.get_secret_value(),
        aws_secret_access_key=credentials.secret_access_key.get_secret_value(),
        aws_session_token=credentials.session_token.get_secret_value(),
    )


def _assumed_authorization_session(
    validation_input: AwsAccountAuthorizationValidationInput,
    session_factory: AwsConnectionSessionFactory,
) -> AwsConnectionSession:
    return _assumed_active_authorization_session(
        validation_input.pending,
        external_id=validation_input.external_id,
        session_factory=session_factory,
    )


def _assumed_active_authorization_session(
    authorization: AwsPendingAccountAuthorization | AwsExistingAccountAuthorization,
    *,
    external_id: SecretStr,
    session_factory: AwsConnectionSessionFactory,
) -> AwsConnectionSession:
    _validate_external_id(authorization, external_id)
    source = session_factory(region_name=authorization.region)
    try:
        assumed = source.client("sts").assume_role(
            RoleArn=authorization.role_arn,
            RoleSessionName=_authorization_session_name(authorization),
            ExternalId=external_id.get_secret_value(),
            DurationSeconds=3600,
        )
    except ClientError as exc:
        raise _client_error(exc, operation="assume account connection role") from exc
    except BotoCoreError as exc:
        raise upstream_error(exc, operation="assume account connection role") from exc
    credentials = _validated(
        _AssumeRoleResponse,
        assumed,
        operation="assume account connection role",
    ).credentials
    return session_factory(
        region_name=authorization.region,
        aws_access_key_id=credentials.access_key_id.get_secret_value(),
        aws_secret_access_key=credentials.secret_access_key.get_secret_value(),
        aws_session_token=credentials.session_token.get_secret_value(),
    )


def _validate_external_id(
    authorization: (AwsPendingAccountAuthorization | AwsExistingAccountAuthorization),
    external_id: SecretStr,
) -> None:
    raw = external_id.get_secret_value()
    if not _EXTERNAL_ID_PATTERN.fullmatch(raw):
        raise ValueError("external ID must contain 32-256 AWS-safe characters")
    if hashlib.sha256(raw.encode()).hexdigest() != authorization.external_id_sha256:
        raise ValueError("external ID does not match the authorization generation")


def _assumed_authorization_session_if_available(
    authorization: AwsPendingAccountAuthorization | AwsExistingAccountAuthorization,
    *,
    external_id: SecretStr,
    session_factory: AwsConnectionSessionFactory,
) -> AwsConnectionSession | None:
    try:
        return _assumed_active_authorization_session(
            authorization,
            external_id=external_id,
            session_factory=session_factory,
        )
    except AwsProviderControlError as exc:
        if exc.code is AwsProviderControlErrorCode.ControlRoleUnavailable:
            return None
        raise


def _describe_authorization_stack_if_present(
    client: AwsConnectionCloudFormationClient,
    *,
    authorization: AwsPendingAccountAuthorization,
    stack_target: str,
) -> _Stack | None:
    try:
        response = client.describe_stacks(StackName=stack_target)
    except ClientError as exc:
        if _cloudformation_stack_not_found(exc):
            return None
        raise _client_error(exc, operation="observe managed authorization stack") from exc
    except BotoCoreError as exc:
        raise upstream_error(exc, operation="observe managed authorization stack") from exc
    stacks = _validated(
        _DescribeStacksResponse,
        response,
        operation="observe managed authorization stack",
    ).stacks
    if len(stacks) != 1 or stacks[0].stack_name != authorization.stack_name:
        raise invalid_response_error(
            "observe managed authorization stack",
            "AWS returned a stack outside the authorization generation scope",
        )
    stack = stacks[0]
    if isinstance(authorization, AwsActiveAccountAuthorization) and (
        stack.stack_id != authorization.stack_id
    ):
        raise invalid_response_error(
            "observe managed authorization stack",
            "AWS returned a stack outside the validated authorization scope",
        )
    return stack


def _authorization_stack_console_url(*, region: str, stack_id: str) -> str:
    region_query = urlencode({"region": region}, quote_via=quote, safe="")
    stack_query = urlencode({"stackId": stack_id}, quote_via=quote, safe="")
    return (
        f"https://{aws_console_host(region)}/cloudformation/home?{region_query}"
        f"#/stacks/stackinfo?{stack_query}"
    )


def _delete_role(client: AwsConnectionIamClient, *, role_name: str) -> None:
    try:
        policies = _validated(
            _ListRolePoliciesResponse,
            client.list_role_policies(RoleName=role_name),
            operation="list predecessor authorization policies",
        )
        if policies.truncated:
            raise invalid_response_error(
                "list predecessor authorization policies",
                "AWS paginated the predecessor inline policies unexpectedly",
            )
        for policy_name in policies.policy_names:
            client.delete_role_policy(RoleName=role_name, PolicyName=policy_name)
        client.delete_role(RoleName=role_name)
    except ClientError as exc:
        if _iam_role_not_found(exc):
            return
        raise _client_error(exc, operation="clean predecessor authorization role") from exc
    except BotoCoreError as exc:
        raise upstream_error(exc, operation="clean predecessor authorization role") from exc


def _cloudformation_stack_not_found(exc: ClientError) -> bool:
    error = exc.response.get("Error", {})
    code = str(error.get("Code", "")) if isinstance(error, Mapping) else ""
    message = str(error.get("Message", "")) if isinstance(error, Mapping) else ""
    return code.casefold() == "validationerror" and "does not exist" in message.casefold()


def _consolidate_bucket_access_grants(
    grants: Sequence[AwsNodeBucketAccessGrant],
) -> tuple[AwsNodeBucketAccessGrant, ...]:
    access: dict[tuple[str, str], bool] = {}
    for grant in grants:
        key = (grant.bucket, grant.prefix)
        access[key] = access.get(key, True) and grant.read_only
    return tuple(
        AwsNodeBucketAccessGrant(bucket=bucket, prefix=prefix, read_only=read_only)
        for (bucket, prefix), read_only in sorted(access.items())
    )


def aws_node_bucket_access_policy(
    grants: Sequence[AwsNodeBucketAccessGrant],
    *,
    partition: str = "aws",
) -> dict[str, object]:
    consolidated = _consolidate_bucket_access_grants(grants)
    if not consolidated:
        raise ValueError("connected bucket access policy requires at least one grant")
    statements: list[dict[str, object]] = [
        {
            "Sid": "BucketLocation",
            "Effect": "Allow",
            "Action": ["s3:GetBucketLocation"],
            "Resource": sorted({f"arn:{partition}:s3:::{grant.bucket}" for grant in consolidated}),
        }
    ]
    writable_buckets = sorted(
        {f"arn:{partition}:s3:::{grant.bucket}" for grant in consolidated if not grant.read_only}
    )
    if writable_buckets:
        statements.append(
            {
                "Sid": "BucketMultipartUploads",
                "Effect": "Allow",
                "Action": ["s3:ListBucketMultipartUploads"],
                "Resource": writable_buckets,
            }
        )
    for index, grant in enumerate(consolidated, start=1):
        bucket_arn = f"arn:{partition}:s3:::{grant.bucket}"
        object_arn = f"{bucket_arn}/{grant.prefix}/*" if grant.prefix else f"{bucket_arn}/*"
        list_statement: dict[str, object] = {
            "Sid": f"ListPrefix{index}",
            "Effect": "Allow",
            "Action": ["s3:ListBucket"],
            "Resource": [bucket_arn],
        }
        if grant.prefix:
            list_statement["Condition"] = {
                "StringLike": {
                    "s3:prefix": [grant.prefix, f"{grant.prefix}/*"],
                }
            }
        statements.append(list_statement)
        object_actions = [
            "s3:GetObject",
            "s3:GetObjectAttributes",
            "s3:GetObjectVersion",
        ]
        if not grant.read_only:
            object_actions.extend(
                [
                    "s3:AbortMultipartUpload",
                    "s3:DeleteObject",
                    "s3:ListMultipartUploadParts",
                    "s3:PutObject",
                ]
            )
        statements.append(
            {
                "Sid": f"Objects{index}",
                "Effect": "Allow",
                "Action": object_actions,
                "Resource": [object_arn],
            }
        )
    return {"Version": "2012-10-17", "Statement": statements}


def _iam_role_not_found(exc: ClientError) -> bool:
    error = exc.response.get("Error", {})
    code = str(error.get("Code", "")) if isinstance(error, Mapping) else ""
    return code.casefold() == "nosuchentity"


def _connection_resource_suffix(user_id: str, connection_id: str) -> str:
    digest = hashlib.sha256(f"{user_id}\0{connection_id}".encode()).hexdigest()[:20]
    return digest


def _managed_node_identity(
    *,
    account_id: str,
    region: str,
    suffix: str,
) -> AwsManagedNodeIdentity:
    node_name = f"compute-node-{suffix}"
    partition = aws_partition_for_region(region)
    return AwsManagedNodeIdentity(
        role_name=node_name,
        role_arn=f"arn:{partition}:iam::{account_id}:role/{node_name}",
        instance_profile_name=node_name,
        instance_profile_arn=(f"arn:{partition}:iam::{account_id}:instance-profile/{node_name}"),
    )


def _validate_node_identity_scope(
    identity: AwsManagedNodeIdentity,
    *,
    account_id: str,
    region: str,
) -> None:
    expected_partition = aws_partition_for_region(region)
    expected_kinds = {
        identity.role_arn: "role",
        identity.instance_profile_arn: "instance-profile",
    }
    for arn, expected_kind in expected_kinds.items():
        match = _ARN_PATTERN.fullmatch(arn)
        if match is None:
            raise ValueError("managed node identity ARN is invalid")
        partition, resource_account_id, kind, _ = match.groups()
        if partition != expected_partition or resource_account_id != account_id:
            raise ValueError("managed node identity ARN is outside the account scope")
        if kind != expected_kind:
            raise ValueError(f"managed node identity ARN must identify an IAM {expected_kind}")


def _validated[ResponseT: BaseModel](
    model: type[ResponseT],
    response: Mapping[str, object],
    *,
    operation: str,
) -> ResponseT:
    try:
        return model.model_validate(response)
    except ValidationError as exc:
        raise invalid_response_error(operation, "AWS returned an invalid response") from exc


def _client_error(exc: ClientError, *, operation: str) -> AwsProviderControlError:
    error = exc.response.get("Error", {})
    code = str(error.get("Code", "")) if isinstance(error, Mapping) else ""
    message = str(error.get("Message", "")) if isinstance(error, Mapping) else ""
    normalized = code.casefold()
    if normalized in {"accessdenied", "accessdeniedexception", "unauthorizedoperation"}:
        error_code = (
            AwsProviderControlErrorCode.ControlRoleUnavailable
            if operation == "assume account connection role"
            else AwsProviderControlErrorCode.PermissionDenied
        )
    elif normalized in {"nosuchentity", "invalidclienttokenid"}:
        error_code = AwsProviderControlErrorCode.ControlRoleUnavailable
    else:
        error_code = AwsProviderControlErrorCode.UpstreamUnavailable
    return AwsProviderControlError(
        error_code,
        operation=operation,
        detail=message.strip() or code.strip() or "AWS request failed",
    )


def _default_session(
    *,
    region_name: str,
    aws_access_key_id: str | None = None,
    aws_secret_access_key: str | None = None,
    aws_session_token: str | None = None,
) -> AwsConnectionSession:
    return _Boto3ConnectionSession(
        Session(
            region_name=region_name,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            aws_session_token=aws_session_token,
        )
    )
