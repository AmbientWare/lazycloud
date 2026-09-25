from __future__ import annotations

import http.client
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from secrets import token_hex
from urllib.parse import quote

from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from compute.provider_nodes import (
    ProviderNodeIdentityProof,
    ProviderNodeIdentityProofError,
    ProviderNodeIdentityUnavailableError,
)
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from shared.provider_config import ProviderKind

from .instance_catalog import aws_partition_for_region
from .instance_metadata import (
    AWS_IMDS_MAX_TEXT_BYTES,
    AWS_IMDS_TIMEOUT_SECONDS,
    AWS_IMDS_TOKEN_TTL_SECONDS,
    AwsDirectInstanceMetadataTransport,
    AwsInstanceMetadataTransport,
    AwsProviderNodeProofError,
)
from .provider_node_identity import AWS_STS_PROOF_NONCE_KEY, AwsStsGetCallerIdentityProof

AWS_IMDS_MAX_CREDENTIAL_BYTES = 16 * 1024
AWS_STS_PROOF_EXPIRY_SECONDS = 60

_INSTANCE_ID_PATTERN = re.compile(r"^i-[0-9a-f]{8,17}$")
_REGION_PATTERN = re.compile(r"^(us-gov|us|af|ap|ca|cn|eu|il|me|mx|sa)-[a-z0-9-]+-[0-9]+$")
_ROLE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9+=,.@_-]{1,64}$")


class _AwsInstanceRoleCredentials(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    code: str = Field(alias="Code")
    access_key_id: SecretStr = Field(alias="AccessKeyId")
    secret_access_key: SecretStr = Field(alias="SecretAccessKey")
    token: SecretStr = Field(alias="Token")
    expiration: datetime = Field(alias="Expiration")

    @field_validator("access_key_id", "secret_access_key", "token")
    @classmethod
    def credentials_cannot_be_empty(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("EC2 instance role credentials cannot be empty")
        return value


@dataclass(frozen=True, slots=True)
class AwsEc2ProviderNodeIdentityProofProvider:
    transport: AwsInstanceMetadataTransport = field(
        default_factory=AwsDirectInstanceMetadataTransport
    )
    timeout_seconds: float = AWS_IMDS_TIMEOUT_SECONDS

    def create(self, *, expected_region: str | None = None) -> AwsStsGetCallerIdentityProof:
        region = _validated_region(expected_region) if expected_region is not None else None
        token = self._text(
            method="PUT",
            path="/latest/api/token",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": str(AWS_IMDS_TOKEN_TTL_SECONDS)},
        )
        metadata_headers = {"X-aws-ec2-metadata-token": token}
        instance_id = self._text(
            method="GET",
            path="/latest/meta-data/instance-id",
            headers=metadata_headers,
        ).lower()
        if not _INSTANCE_ID_PATTERN.fullmatch(instance_id):
            raise AwsProviderNodeProofError("EC2 metadata returned an invalid instance ID")
        observed_region = _validated_region(
            self._text(
                method="GET",
                path="/latest/meta-data/placement/region",
                headers=metadata_headers,
            )
        )
        if region is not None and observed_region != region:
            raise AwsProviderNodeProofError(
                "EC2 metadata region does not match the configured cloud pool region"
            )
        region = observed_region
        role_name = self._text(
            method="GET",
            path="/latest/meta-data/iam/security-credentials/",
            headers=metadata_headers,
        )
        if not _ROLE_NAME_PATTERN.fullmatch(role_name):
            raise AwsProviderNodeProofError("EC2 metadata returned an invalid role name")
        credentials = self._credentials(
            path=f"/latest/meta-data/iam/security-credentials/{quote(role_name, safe='')}",
            headers=metadata_headers,
        )
        if credentials.code != "Success":
            raise AwsProviderNodeProofError("EC2 instance role credentials are unavailable")
        expiration = credentials.expiration
        if expiration.tzinfo is None or expiration.astimezone(UTC) <= datetime.now(UTC):
            raise AwsProviderNodeProofError("EC2 instance role credentials are expired")

        request = AWSRequest(method="GET", url=_regional_sts_url(region, nonce=token_hex(16)))
        SigV4QueryAuth(
            Credentials(
                access_key=credentials.access_key_id.get_secret_value(),
                secret_key=credentials.secret_access_key.get_secret_value(),
                token=credentials.token.get_secret_value(),
            ),
            "sts",
            region,
            expires=AWS_STS_PROOF_EXPIRY_SECONDS,
        ).add_auth(request)
        prepared = request.prepare()
        proof_url = prepared.url
        if not isinstance(proof_url, str) or not proof_url:
            raise AwsProviderNodeProofError("failed to create AWS STS identity proof")
        return AwsStsGetCallerIdentityProof(
            presigned_url=SecretStr(proof_url),
            region=region,
            instance_id=instance_id,
        )

    def _text(self, *, method: str, path: str, headers: dict[str, str]) -> str:
        response = self.transport.request(
            method=method,
            path=path,
            headers=headers,
            timeout_seconds=self.timeout_seconds,
            max_response_bytes=AWS_IMDS_MAX_TEXT_BYTES,
        )
        if response.status_code != 200:
            raise AwsProviderNodeProofError(
                f"EC2 metadata request failed with status {response.status_code}"
            )
        try:
            value = response.body.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise AwsProviderNodeProofError("EC2 metadata returned invalid text") from exc
        if not value:
            raise AwsProviderNodeProofError("EC2 metadata returned an empty value")
        return value

    def _credentials(
        self,
        *,
        path: str,
        headers: dict[str, str],
    ) -> _AwsInstanceRoleCredentials:
        response = self.transport.request(
            method="GET",
            path=path,
            headers=headers,
            timeout_seconds=self.timeout_seconds,
            max_response_bytes=AWS_IMDS_MAX_CREDENTIAL_BYTES,
        )
        if response.status_code != 200:
            raise AwsProviderNodeProofError(
                f"EC2 instance role credential request failed with status {response.status_code}"
            )
        try:
            return _AwsInstanceRoleCredentials.model_validate_json(response.body)
        except ValueError as exc:
            raise AwsProviderNodeProofError(
                "EC2 metadata returned invalid instance role credentials"
            ) from exc


def _validated_region(value: str) -> str:
    region = value.strip().lower()
    if not _REGION_PATTERN.fullmatch(region):
        raise AwsProviderNodeProofError("invalid AWS region")
    return region


def _regional_sts_url(region: str, *, nonce: str) -> str:
    partition = aws_partition_for_region(region)
    dns_suffix = "amazonaws.com.cn" if partition == "aws-cn" else "amazonaws.com"
    return (
        f"https://sts.{region}.{dns_suffix}/?Action=GetCallerIdentity&Version=2011-06-15"
        f"&{AWS_STS_PROOF_NONCE_KEY}={nonce}"
    )


@dataclass(frozen=True, slots=True)
class AwsProviderNodeIdentityProofProvider:
    """An EC2 node's identity proof, with metadata that does not answer marked unavailable."""

    provider: AwsEc2ProviderNodeIdentityProofProvider = field(
        default_factory=AwsEc2ProviderNodeIdentityProofProvider
    )

    def create(self, *, expected_region: str | None = None) -> ProviderNodeIdentityProof:
        try:
            proof = self.provider.create(expected_region=expected_region)
        except AwsProviderNodeProofError as exc:
            if isinstance(exc.__cause__, OSError | http.client.HTTPException):
                raise ProviderNodeIdentityUnavailableError(str(exc)) from exc
            raise ProviderNodeIdentityProofError(str(exc)) from exc
        return ProviderNodeIdentityProof(
            provider=ProviderKind.Aws,
            region=proof.region,
            provider_instance_id=proof.instance_id,
            proof_url=proof.presigned_url,
        )


__all__ = ["AwsEc2ProviderNodeIdentityProofProvider", "AwsProviderNodeIdentityProofProvider"]
