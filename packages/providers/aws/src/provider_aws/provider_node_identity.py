from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from urllib.parse import parse_qsl, urlparse
from xml.etree import ElementTree

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from .instance_catalog import aws_partition_for_region

AWS_STS_RESPONSE_NAMESPACE = "https://sts.amazonaws.com/doc/2011-06-15/"
AWS_STS_API_VERSION = "2011-06-15"
AWS_STS_MAX_PROOF_URL_BYTES = 16 * 1024
AWS_STS_MAX_PROOF_RESPONSE_BYTES = 32 * 1024
AWS_STS_MAX_PROOF_EXPIRY_SECONDS = 60
AWS_STS_MAX_FUTURE_SKEW_SECONDS = 30
AWS_STS_PROOF_TIMEOUT_SECONDS = 2.0
AWS_STS_PROOF_CONNECT_TIMEOUT_SECONDS = 1.0

_ACCOUNT_ID_PATTERN = re.compile(r"^[0-9]{12}$")
_AWS_ACCESS_KEY_PATTERN = re.compile(r"^[A-Z0-9]{16,128}$")
_AWS_SIGNATURE_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_INSTANCE_ID_PATTERN = re.compile(r"^i-[0-9a-f]{8,17}$")
_INSTANCE_PROFILE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9+=,.@_-]{1,128}$")
_REGION_PATTERN = re.compile(r"^(us-gov|us|af|ap|ca|cn|eu|il|me|mx|sa)-[a-z0-9-]+-[0-9]+$")
_ROLE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9+=,.@_-]{1,64}$")
_ROLE_PRINCIPAL_ID_PATTERN = re.compile(r"^[A-Z0-9]{16,128}$")
_SECURITY_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9/+=,.@_-]{1,4096}$")

_REQUIRED_QUERY_KEYS = frozenset(
    {
        "Action",
        "Version",
        "X-Amz-Algorithm",
        "X-Amz-Credential",
        "X-Amz-Date",
        "X-Amz-Expires",
        "X-Amz-SignedHeaders",
        "X-Amz-Security-Token",
        "X-Amz-Signature",
    }
)


class AwsProviderNodeIdentityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AwsProviderNodeIdentityErrorCode(StrEnum):
    InvalidProof = "invalid_proof"
    ExpiredProof = "expired_proof"
    ProofNotYetValid = "proof_not_yet_valid"
    ReplayedProof = "replayed_proof"
    UpstreamUnavailable = "upstream_unavailable"
    ResourceNotFound = "resource_not_found"
    ResourceNotReady = "resource_not_ready"
    IdentityMismatch = "identity_mismatch"


class AwsProviderNodeIdentityError(RuntimeError):
    def __init__(self, code: AwsProviderNodeIdentityErrorCode, *, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"AWS provider-node identity verification failed: {detail}")


class AwsProviderNodeIdentityTransportError(RuntimeError):
    """Raised by the HTTP adapter when the bounded STS request cannot complete."""


class AwsProviderNodeReplayGuardError(RuntimeError):
    """Raised when the durable replay claim cannot be checked atomically."""


class AwsStsGetCallerIdentityProof(AwsProviderNodeIdentityModel):
    presigned_url: SecretStr = Field(repr=False)
    region: str
    instance_id: str

    @field_validator("region")
    @classmethod
    def validate_region(cls, value: str) -> str:
        region = value.strip().lower()
        if not _REGION_PATTERN.fullmatch(region):
            raise ValueError("invalid AWS region")
        return region

    @field_validator("instance_id")
    @classmethod
    def validate_instance_id(cls, value: str) -> str:
        instance_id = value.strip().lower()
        if not _INSTANCE_ID_PATTERN.fullmatch(instance_id):
            raise ValueError("invalid EC2 instance ID")
        return instance_id


class AwsProviderNodeIdentityTarget(AwsProviderNodeIdentityModel):
    account_id: str = Field(pattern=_ACCOUNT_ID_PATTERN.pattern)
    region: str = Field(pattern=_REGION_PATTERN.pattern)
    node_role_arn: str
    node_instance_profile_arn: str
    autoscaling_group_name: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$",
    )

    @field_validator("region")
    @classmethod
    def normalize_region(cls, value: str) -> str:
        return value.strip().lower()

    @model_validator(mode="after")
    def validate_connection_identity(self) -> AwsProviderNodeIdentityTarget:
        try:
            node_role = _parse_node_role_arn(self.node_role_arn)
            node_profile = _parse_instance_profile_arn(self.node_instance_profile_arn)
        except ValueError as exc:
            raise ValueError("invalid AWS node IAM identity") from exc
        expected_partition = aws_partition_for_region(self.region)
        if (
            node_role.partition != expected_partition
            or node_profile.partition != expected_partition
            or node_role.account_id != self.account_id
            or node_profile.account_id != self.account_id
        ):
            raise ValueError("AWS node IAM identity does not match account and region")
        return self


class AwsVerifiedProviderNodeIdentity(AwsProviderNodeIdentityModel):
    account_id: str
    partition: str
    region: str
    instance_id: str
    caller_arn: str
    caller_user_id: str
    node_role_arn: str
    node_instance_profile_arn: str
    autoscaling_group_name: str
    proof_sha256: str
    proof_expires_at: datetime


@dataclass(frozen=True, slots=True)
class AwsStsProofHttpResponse:
    status_code: int
    body: bytes
    content_type: str = ""
    content_length: int | None = None


class AwsStsProofHttpClient(Protocol):
    def execute_presigned_get(
        self,
        *,
        url: str,
        timeout_seconds: float,
        max_response_bytes: int,
        follow_redirects: bool,
    ) -> AwsStsProofHttpResponse: ...


class AwsProviderNodeReplayGuard(Protocol):
    def claim_once(self, *, proof_sha256: str, expires_at: datetime) -> bool: ...


class _ValidatedPresignedRequest(AwsProviderNodeIdentityModel):
    url: SecretStr = Field(repr=False)
    signed_at: datetime
    expires_at: datetime
    proof_sha256: str


class _StsCallerIdentity(AwsProviderNodeIdentityModel):
    account_id: str
    arn: str
    user_id: str
    partition: str
    role_name: str
    role_session_name: str


class AwsProviderNodeIdentityVerifier:
    def __init__(
        self,
        *,
        http_client: AwsStsProofHttpClient,
        replay_guard: AwsProviderNodeReplayGuard,
    ) -> None:
        self._http_client = http_client
        self._replay_guard = replay_guard

    def verify(
        self,
        proof: AwsStsGetCallerIdentityProof,
        *,
        target: AwsProviderNodeIdentityTarget,
        provider_machine_ids: tuple[str, ...],
        now: datetime | None = None,
    ) -> AwsVerifiedProviderNodeIdentity:
        if proof.region != target.region:
            raise _identity_error(
                AwsProviderNodeIdentityErrorCode.IdentityMismatch,
                "claimed region does not match the enrollment target",
            )

        verified_at = _normalized_now(now)
        request = _validate_presigned_request(proof, target=target, now=verified_at)
        caller = self._execute_sts_request(request)
        self._verify_caller_scope(caller, proof=proof, target=target)

        node_role = _parse_node_role_arn(target.node_role_arn)
        node_profile = _parse_instance_profile_arn(target.node_instance_profile_arn)
        if (
            node_role.partition != caller.partition
            or node_role.account_id != caller.account_id
            or node_role.role_name != caller.role_name
        ):
            raise _identity_error(
                AwsProviderNodeIdentityErrorCode.IdentityMismatch,
                "STS caller does not match the connected AWS node role",
            )
        if (
            node_profile.partition != caller.partition
            or node_profile.account_id != caller.account_id
        ):
            raise _identity_error(
                AwsProviderNodeIdentityErrorCode.UpstreamUnavailable,
                "connected AWS node instance profile does not match the caller scope",
            )
        if caller.role_session_name != proof.instance_id:
            raise _identity_error(
                AwsProviderNodeIdentityErrorCode.IdentityMismatch,
                "STS role session does not match the claimed EC2 instance",
            )
        user_session = caller.user_id.rsplit(":", maxsplit=1)[-1]
        if user_session != proof.instance_id:
            raise _identity_error(
                AwsProviderNodeIdentityErrorCode.IdentityMismatch,
                "STS user identity does not match the claimed EC2 instance",
            )

        if len(set(provider_machine_ids)) != len(provider_machine_ids) or any(
            not _INSTANCE_ID_PATTERN.fullmatch(instance_id) for instance_id in provider_machine_ids
        ):
            raise _identity_error(
                AwsProviderNodeIdentityErrorCode.UpstreamUnavailable,
                "managed AWS provider inventory is invalid",
            )
        if proof.instance_id not in provider_machine_ids:
            raise _identity_error(
                AwsProviderNodeIdentityErrorCode.IdentityMismatch,
                "claimed EC2 instance is not a member of the managed Auto Scaling Group",
            )

        try:
            claimed = self._replay_guard.claim_once(
                proof_sha256=request.proof_sha256,
                expires_at=request.expires_at,
            )
        except AwsProviderNodeReplayGuardError as exc:
            raise _identity_error(
                AwsProviderNodeIdentityErrorCode.UpstreamUnavailable,
                "durable proof replay check is unavailable",
            ) from exc
        if not claimed:
            raise _identity_error(
                AwsProviderNodeIdentityErrorCode.ReplayedProof,
                "identity proof has already been used",
            )

        return AwsVerifiedProviderNodeIdentity(
            account_id=caller.account_id,
            partition=caller.partition,
            region=target.region,
            instance_id=proof.instance_id,
            caller_arn=caller.arn,
            caller_user_id=caller.user_id,
            node_role_arn=target.node_role_arn,
            node_instance_profile_arn=target.node_instance_profile_arn,
            autoscaling_group_name=target.autoscaling_group_name,
            proof_sha256=request.proof_sha256,
            proof_expires_at=request.expires_at,
        )

    def _execute_sts_request(
        self,
        request: _ValidatedPresignedRequest,
    ) -> _StsCallerIdentity:
        try:
            response = self._http_client.execute_presigned_get(
                url=request.url.get_secret_value(),
                timeout_seconds=AWS_STS_PROOF_TIMEOUT_SECONDS,
                max_response_bytes=AWS_STS_MAX_PROOF_RESPONSE_BYTES,
                follow_redirects=False,
            )
        except AwsProviderNodeIdentityTransportError as exc:
            raise _identity_error(
                AwsProviderNodeIdentityErrorCode.UpstreamUnavailable,
                "regional AWS STS identity service is unavailable",
            ) from exc
        if 300 <= response.status_code < 400:
            raise _identity_error(
                AwsProviderNodeIdentityErrorCode.InvalidProof,
                "AWS STS identity proof returned a redirect",
            )
        if response.status_code == 429 or response.status_code >= 500:
            raise _identity_error(
                AwsProviderNodeIdentityErrorCode.UpstreamUnavailable,
                "regional AWS STS identity service is unavailable",
            )
        if response.status_code != 200:
            raise _identity_error(
                AwsProviderNodeIdentityErrorCode.InvalidProof,
                "AWS STS rejected the identity proof",
            )
        if response.content_length is not None and (
            response.content_length < 0
            or response.content_length > AWS_STS_MAX_PROOF_RESPONSE_BYTES
        ):
            raise _identity_error(
                AwsProviderNodeIdentityErrorCode.InvalidProof,
                "AWS STS identity response exceeds the size limit",
            )
        if len(response.body) > AWS_STS_MAX_PROOF_RESPONSE_BYTES:
            raise _identity_error(
                AwsProviderNodeIdentityErrorCode.InvalidProof,
                "AWS STS identity response exceeds the size limit",
            )
        content_type = response.content_type.partition(";")[0].strip().casefold()
        if content_type and content_type not in {"application/xml", "text/xml"}:
            raise _identity_error(
                AwsProviderNodeIdentityErrorCode.InvalidProof,
                "AWS STS identity response has an invalid content type",
            )
        return _parse_sts_caller_identity(response.body)

    def _verify_caller_scope(
        self,
        caller: _StsCallerIdentity,
        *,
        proof: AwsStsGetCallerIdentityProof,
        target: AwsProviderNodeIdentityTarget,
    ) -> None:
        expected_partition = aws_partition_for_region(proof.region)
        if caller.account_id != target.account_id:
            raise _identity_error(
                AwsProviderNodeIdentityErrorCode.IdentityMismatch,
                "STS caller account does not match the enrollment target",
            )
        if caller.partition != expected_partition:
            raise _identity_error(
                AwsProviderNodeIdentityErrorCode.IdentityMismatch,
                "STS caller partition does not match the enrollment target",
            )


class _NodeRoleIdentity(AwsProviderNodeIdentityModel):
    partition: str
    account_id: str
    role_name: str


class _InstanceProfileIdentity(AwsProviderNodeIdentityModel):
    partition: str
    account_id: str
    profile_name: str


def _validate_presigned_request(
    proof: AwsStsGetCallerIdentityProof,
    *,
    target: AwsProviderNodeIdentityTarget,
    now: datetime,
) -> _ValidatedPresignedRequest:
    raw_url = proof.presigned_url.get_secret_value()
    if len(raw_url.encode()) > AWS_STS_MAX_PROOF_URL_BYTES:
        raise _identity_error(
            AwsProviderNodeIdentityErrorCode.InvalidProof,
            "AWS STS identity proof URL exceeds the size limit",
        )
    try:
        parsed = urlparse(raw_url)
        parsed_port = parsed.port
    except ValueError as exc:
        raise _identity_error(
            AwsProviderNodeIdentityErrorCode.InvalidProof,
            "AWS STS identity proof URL is invalid",
        ) from exc
    expected_host = _sts_hostname(target.region)
    valid_netlocs = {expected_host, f"{expected_host}:443"}
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname != expected_host
        or parsed_port not in {None, 443}
        or parsed.netloc not in valid_netlocs
        or parsed.path != "/"
        or parsed.params
        or parsed.fragment
    ):
        raise _identity_error(
            AwsProviderNodeIdentityErrorCode.InvalidProof,
            "AWS STS identity proof URL does not target the expected regional endpoint",
        )
    try:
        query_pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=len(_REQUIRED_QUERY_KEYS),
        )
    except ValueError as exc:
        raise _identity_error(
            AwsProviderNodeIdentityErrorCode.InvalidProof,
            "AWS STS identity proof query is invalid",
        ) from exc
    query: dict[str, str] = {}
    for key, value in query_pairs:
        if key in query:
            raise _identity_error(
                AwsProviderNodeIdentityErrorCode.InvalidProof,
                "AWS STS identity proof contains duplicate query fields",
            )
        query[key] = value
    if frozenset(query) != _REQUIRED_QUERY_KEYS:
        raise _identity_error(
            AwsProviderNodeIdentityErrorCode.InvalidProof,
            "AWS STS identity proof query fields are not allowed",
        )
    if (
        query["Action"] != "GetCallerIdentity"
        or query["Version"] != AWS_STS_API_VERSION
        or query["X-Amz-Algorithm"] != "AWS4-HMAC-SHA256"
        or query["X-Amz-SignedHeaders"] != "host"
        or not _SECURITY_TOKEN_PATTERN.fullmatch(query["X-Amz-Security-Token"])
        or not _AWS_SIGNATURE_PATTERN.fullmatch(query["X-Amz-Signature"])
    ):
        raise _identity_error(
            AwsProviderNodeIdentityErrorCode.InvalidProof,
            "AWS STS identity proof signature fields are invalid",
        )

    credential_parts = query["X-Amz-Credential"].split("/")
    if len(credential_parts) != 5:
        raise _identity_error(
            AwsProviderNodeIdentityErrorCode.InvalidProof,
            "AWS STS identity proof credential scope is invalid",
        )
    access_key_id, credential_date, credential_region, service, terminator = credential_parts
    if (
        not _AWS_ACCESS_KEY_PATTERN.fullmatch(access_key_id)
        or credential_region != target.region
        or service != "sts"
        or terminator != "aws4_request"
    ):
        raise _identity_error(
            AwsProviderNodeIdentityErrorCode.InvalidProof,
            "AWS STS identity proof credential scope is invalid",
        )
    try:
        signed_at = datetime.strptime(query["X-Amz-Date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        if not re.fullmatch(r"[1-9][0-9]{0,2}", query["X-Amz-Expires"]):
            raise ValueError
        expiry_seconds = int(query["X-Amz-Expires"])
    except ValueError as exc:
        raise _identity_error(
            AwsProviderNodeIdentityErrorCode.InvalidProof,
            "AWS STS identity proof freshness fields are invalid",
        ) from exc
    if credential_date != signed_at.strftime("%Y%m%d"):
        raise _identity_error(
            AwsProviderNodeIdentityErrorCode.InvalidProof,
            "AWS STS identity proof credential date is invalid",
        )
    if not 1 <= expiry_seconds <= AWS_STS_MAX_PROOF_EXPIRY_SECONDS:
        raise _identity_error(
            AwsProviderNodeIdentityErrorCode.InvalidProof,
            "AWS STS identity proof expiry is outside the allowed range",
        )
    if signed_at > now + timedelta(seconds=AWS_STS_MAX_FUTURE_SKEW_SECONDS):
        raise _identity_error(
            AwsProviderNodeIdentityErrorCode.ProofNotYetValid,
            "AWS STS identity proof is not yet valid",
        )
    expires_at = signed_at + timedelta(seconds=expiry_seconds)
    if now > expires_at:
        raise _identity_error(
            AwsProviderNodeIdentityErrorCode.ExpiredProof,
            "AWS STS identity proof has expired",
        )
    return _ValidatedPresignedRequest(
        url=SecretStr(raw_url),
        signed_at=signed_at,
        expires_at=expires_at,
        proof_sha256=hashlib.sha256(raw_url.encode()).hexdigest(),
    )


def _parse_sts_caller_identity(payload: bytes) -> _StsCallerIdentity:
    if not payload or len(payload) > AWS_STS_MAX_PROOF_RESPONSE_BYTES:
        raise _identity_error(
            AwsProviderNodeIdentityErrorCode.InvalidProof,
            "AWS STS identity response is empty or too large",
        )
    normalized = payload.upper()
    if b"<!DOCTYPE" in normalized or b"<!ENTITY" in normalized or b"\x00" in payload:
        raise _identity_error(
            AwsProviderNodeIdentityErrorCode.InvalidProof,
            "AWS STS identity response contains forbidden XML constructs",
        )
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise _identity_error(
            AwsProviderNodeIdentityErrorCode.InvalidProof,
            "AWS STS identity response is invalid XML",
        ) from exc
    if root.tag != f"{{{AWS_STS_RESPONSE_NAMESPACE}}}GetCallerIdentityResponse":
        raise _identity_error(
            AwsProviderNodeIdentityErrorCode.InvalidProof,
            "AWS STS identity response root is invalid",
        )
    result_tag = f"{{{AWS_STS_RESPONSE_NAMESPACE}}}GetCallerIdentityResult"
    metadata_tag = f"{{{AWS_STS_RESPONSE_NAMESPACE}}}ResponseMetadata"
    root_children = list(root)
    if any(child.tag not in {result_tag, metadata_tag} for child in root_children):
        raise _identity_error(
            AwsProviderNodeIdentityErrorCode.InvalidProof,
            "AWS STS identity response fields are invalid",
        )
    if sum(child.tag == metadata_tag for child in root_children) > 1:
        raise _identity_error(
            AwsProviderNodeIdentityErrorCode.InvalidProof,
            "AWS STS identity response metadata is invalid",
        )
    results = [child for child in root_children if child.tag == result_tag]
    if len(results) != 1:
        raise _identity_error(
            AwsProviderNodeIdentityErrorCode.InvalidProof,
            "AWS STS identity response result is invalid",
        )
    values: dict[str, str] = {}
    for child in results[0]:
        if not child.tag.startswith(f"{{{AWS_STS_RESPONSE_NAMESPACE}}}") or list(child):
            raise _identity_error(
                AwsProviderNodeIdentityErrorCode.InvalidProof,
                "AWS STS identity response fields are invalid",
            )
        key = child.tag.removeprefix(f"{{{AWS_STS_RESPONSE_NAMESPACE}}}")
        if key not in {"Account", "Arn", "UserId"} or key in values:
            raise _identity_error(
                AwsProviderNodeIdentityErrorCode.InvalidProof,
                "AWS STS identity response fields are invalid",
            )
        values[key] = (child.text or "").strip()
    if set(values) != {"Account", "Arn", "UserId"}:
        raise _identity_error(
            AwsProviderNodeIdentityErrorCode.InvalidProof,
            "AWS STS identity response fields are incomplete",
        )
    return _caller_identity(values["Account"], values["Arn"], values["UserId"])


def _caller_identity(account_id: str, arn: str, user_id: str) -> _StsCallerIdentity:
    if not _ACCOUNT_ID_PATTERN.fullmatch(account_id):
        raise _identity_error(
            AwsProviderNodeIdentityErrorCode.InvalidProof,
            "AWS STS identity response account is invalid",
        )
    arn_parts = arn.split(":", maxsplit=5)
    if len(arn_parts) != 6:
        raise _identity_error(
            AwsProviderNodeIdentityErrorCode.InvalidProof,
            "AWS STS caller ARN is invalid",
        )
    prefix, partition, service, region, arn_account, resource = arn_parts
    resource_parts = resource.split("/")
    if (
        prefix != "arn"
        or partition not in {"aws", "aws-us-gov", "aws-cn"}
        or service != "sts"
        or region
        or arn_account != account_id
        or len(resource_parts) != 3
        or resource_parts[0] != "assumed-role"
        or not _ROLE_NAME_PATTERN.fullmatch(resource_parts[1])
        or not _INSTANCE_ID_PATTERN.fullmatch(resource_parts[2])
    ):
        raise _identity_error(
            AwsProviderNodeIdentityErrorCode.InvalidProof,
            "AWS STS caller ARN is invalid",
        )
    user_parts = user_id.split(":")
    if (
        len(user_parts) != 2
        or not _ROLE_PRINCIPAL_ID_PATTERN.fullmatch(user_parts[0])
        or not _INSTANCE_ID_PATTERN.fullmatch(user_parts[1])
    ):
        raise _identity_error(
            AwsProviderNodeIdentityErrorCode.InvalidProof,
            "AWS STS caller user ID is invalid",
        )
    return _StsCallerIdentity(
        account_id=account_id,
        arn=arn,
        user_id=user_id,
        partition=partition,
        role_name=resource_parts[1],
        role_session_name=resource_parts[2],
    )


def _parse_node_role_arn(arn: str) -> _NodeRoleIdentity:
    parts = arn.split(":", maxsplit=5)
    if len(parts) != 6:
        raise ValueError("invalid AWS node role ARN")
    prefix, partition, service, region, account_id, resource = parts
    if (
        prefix != "arn"
        or partition not in {"aws", "aws-us-gov", "aws-cn"}
        or service != "iam"
        or region
        or not _ACCOUNT_ID_PATTERN.fullmatch(account_id)
        or not resource.startswith("role/")
    ):
        raise ValueError("invalid AWS node role ARN")
    role_path = resource.removeprefix("role/")
    role_name = role_path.rsplit("/", maxsplit=1)[-1]
    if not role_path or "//" in role_path:
        raise ValueError("invalid AWS node role ARN")
    if not _ROLE_NAME_PATTERN.fullmatch(role_name):
        raise ValueError("invalid AWS node role ARN")
    return _NodeRoleIdentity(
        partition=partition,
        account_id=account_id,
        role_name=role_name,
    )


def _parse_instance_profile_arn(arn: str) -> _InstanceProfileIdentity:
    parts = arn.split(":", maxsplit=5)
    if len(parts) != 6:
        raise ValueError("invalid AWS node instance profile ARN")
    prefix, partition, service, region, account_id, resource = parts
    if (
        prefix != "arn"
        or partition not in {"aws", "aws-us-gov", "aws-cn"}
        or service != "iam"
        or region
        or not _ACCOUNT_ID_PATTERN.fullmatch(account_id)
        or not resource.startswith("instance-profile/")
    ):
        raise ValueError("invalid AWS node instance profile ARN")
    profile_path = resource.removeprefix("instance-profile/")
    profile_name = profile_path.rsplit("/", maxsplit=1)[-1]
    if not profile_path or "//" in profile_path:
        raise ValueError("invalid AWS node instance profile ARN")
    if not _INSTANCE_PROFILE_NAME_PATTERN.fullmatch(profile_name):
        raise ValueError("invalid AWS node instance profile ARN")
    return _InstanceProfileIdentity(
        partition=partition,
        account_id=account_id,
        profile_name=profile_name,
    )


def _sts_hostname(region: str) -> str:
    suffix = "amazonaws.com.cn" if aws_partition_for_region(region) == "aws-cn" else "amazonaws.com"
    return f"sts.{region}.{suffix}"


def _normalized_now(now: datetime | None) -> datetime:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        raise ValueError("identity verification time must be timezone-aware")
    return value.astimezone(UTC)


def _identity_error(
    code: AwsProviderNodeIdentityErrorCode,
    detail: str,
) -> AwsProviderNodeIdentityError:
    return AwsProviderNodeIdentityError(code, detail=detail)
