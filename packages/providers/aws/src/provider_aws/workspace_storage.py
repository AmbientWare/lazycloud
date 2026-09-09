from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from types import ModuleType
from typing import Literal, Protocol, TypedDict, runtime_checkable

import boto3
from boto3.session import Session
from botocore.client import BaseClient
from botocore.exceptions import ClientError
from pydantic import Field, JsonValue, TypeAdapter
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.identity import WorkspaceStorageConfig
from shared.workspace_storage import WorkspaceStorageGrant
from storage_client.s3 import S3ObjectStoreClient, S3ObjectStoreSettings


class AwsWorkspaceStorageSettings(BaseSettings):
    role_arn: str = Field(
        default="",
        pattern=r"^arn:(aws|aws-us-gov|aws-cn):iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$",
    )

    model_config = SettingsConfigDict(
        env_prefix="LAZYCLOUD_AWS_WORKSPACE_STORAGE_", extra="ignore", hide_input_in_errors=True
    )


class _Credentials(TypedDict):
    AccessKeyId: str
    SecretAccessKey: str
    SessionToken: str
    Expiration: datetime


class _AssumeRoleResponse(TypedDict):
    Credentials: _Credentials


class _BucketPolicyResponse(TypedDict):
    Policy: str


@runtime_checkable
class _ClientFactory(Protocol):
    def client(self, service_name: Literal["s3", "sts"], *, region_name: str) -> BaseClient: ...


@runtime_checkable
class _CredentialAuthority(Protocol):
    def assume_role(
        self, *, RoleArn: str, RoleSessionName: str, DurationSeconds: int, Policy: str
    ) -> _AssumeRoleResponse: ...

    def close(self) -> None: ...


@runtime_checkable
class _BucketAdministration(Protocol):
    def head_bucket(self, *, Bucket: str, ExpectedBucketOwner: str) -> None: ...

    def get_bucket_policy(
        self, *, Bucket: str, ExpectedBucketOwner: str
    ) -> _BucketPolicyResponse: ...

    def put_bucket_policy(self, *, Bucket: str, ExpectedBucketOwner: str, Policy: str) -> None: ...

    def close(self) -> None: ...


def _client(service: Literal["s3", "sts"], region: str) -> BaseClient:
    factory: ModuleType = boto3
    if not isinstance(factory, _ClientFactory):
        raise TypeError("boto3 is missing its client factory")
    return factory.client(service, region_name=region)


@dataclass(frozen=True, slots=True)
class AwsWorkspaceStorageIssuer:
    settings: S3ObjectStoreSettings
    authority: AwsWorkspaceStorageSettings

    def __post_init__(self) -> None:
        region = self.settings.region_name
        partition = Session().get_partition_for_region(region)
        suffix = "amazonaws.com.cn" if partition == "aws-cn" else "amazonaws.com"
        if (
            not region
            or self.authority.role_arn.split(":")[1] != partition
            or self.settings.endpoint_url != f"https://s3.{region}.{suffix}"
        ):
            raise ValueError("managed workspace storage requires its regional AWS S3 endpoint")
        if (
            self.settings.access_key_id
            or self.settings.secret_access_key
            or self.settings.session_token
        ):
            raise ValueError("managed AWS workspace storage requires the workload credential chain")

    def _bucket(self, workspace_id: str, storage: WorkspaceStorageConfig) -> str:
        if storage.access_key or storage.secret_key or storage.config.get("session_token"):
            raise ValueError("managed workspace storage cannot use customer credentials")
        expected = f"{self.settings.workspace_bucket_prefix}-{workspace_id}".replace("_", "-")
        if (
            not workspace_id
            or re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", expected) is None
            or storage.backend != "s3"
            or storage.bucket != expected
            or expected == self.settings.bucket
            or storage.endpoint_url != self.settings.endpoint_url
            or storage.region != self.settings.region_name
            or storage.key_prefix
        ):
            raise ValueError("workspace storage does not name its deployment-owned S3 bucket")
        return expected

    def issue(self, *, workspace_id: str, storage: WorkspaceStorageConfig) -> WorkspaceStorageGrant:
        bucket = self._bucket(workspace_id, storage)
        administration = _client("s3", self.settings.region_name)
        if not isinstance(administration, _BucketAdministration):
            raise TypeError("AWS S3 client is missing bucket administration operations")
        try:
            administration.head_bucket(
                Bucket=bucket, ExpectedBucketOwner=self.authority.role_arn.split(":")[4]
            )
        finally:
            administration.close()
        partition = self.authority.role_arn.split(":")[1]
        bucket_arn = f"arn:{partition}:s3:::{bucket}"
        policy: dict[str, JsonValue] = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "s3:ListBucket",
                        "s3:GetBucketLocation",
                        "s3:ListBucketMultipartUploads",
                    ],
                    "Resource": bucket_arn,
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "s3:GetObject",
                        "s3:PutObject",
                        "s3:DeleteObject",
                        "s3:AbortMultipartUpload",
                        "s3:ListMultipartUploadParts",
                    ],
                    "Resource": f"{bucket_arn}/*",
                },
            ],
        }
        authority = _client("sts", self.settings.region_name)
        if not isinstance(authority, _CredentialAuthority):
            raise TypeError("AWS STS client is missing credential issuance operations")
        try:
            response = authority.assume_role(
                RoleArn=self.authority.role_arn,
                RoleSessionName=f"workspace-{sha256(workspace_id.encode()).hexdigest()[:40]}",
                DurationSeconds=900,
                Policy=json.dumps(policy, separators=(",", ":")),
            )
        finally:
            authority.close()
        credentials = response["Credentials"]
        return WorkspaceStorageGrant(
            endpoint_url=self.settings.endpoint_url,
            region=self.settings.region_name,
            bucket_name=bucket,
            force_path_style=self.settings.force_path_style,
            access_key=credentials["AccessKeyId"],
            secret_key=credentials["SecretAccessKey"],
            session_token=credentials["SessionToken"],
            expires_at=credentials["Expiration"],
        )

    def retire(self, *, workspace_id: str, storage: WorkspaceStorageConfig) -> None:
        bucket = self._bucket(workspace_id, storage)
        owner = self.authority.role_arn.split(":")[4]
        partition = self.authority.role_arn.split(":")[1]
        bucket_arn = f"arn:{partition}:s3:::{bucket}"
        deny: dict[str, JsonValue] = {
            "Sid": "LazyCloudRetiredWorkspaceGrants",
            "Effect": "Deny",
            "Principal": {"AWS": self.authority.role_arn},
            "Action": "s3:*",
            "Resource": [bucket_arn, f"{bucket_arn}/*"],
        }
        administration = _client("s3", self.settings.region_name)
        if not isinstance(administration, _BucketAdministration):
            raise TypeError("AWS S3 client is missing bucket administration operations")
        try:
            try:
                response = administration.get_bucket_policy(
                    Bucket=bucket, ExpectedBucketOwner=owner
                )
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code == "NoSuchBucket":
                    return
                if code != "NoSuchBucketPolicy":
                    raise
                policy: dict[str, JsonValue] = {"Version": "2012-10-17", "Statement": []}
            else:
                policy = TypeAdapter(dict[str, JsonValue]).validate_json(response["Policy"])
            raw_statements = policy.get("Statement")
            statements: JsonValue = (
                [raw_statements] if isinstance(raw_statements, dict) else raw_statements
            )
            if not isinstance(statements, list) or any(
                not isinstance(item, dict) for item in statements
            ):
                raise ValueError("workspace bucket has an invalid policy")
            for statement in statements:
                if isinstance(statement, dict) and statement.get("Sid") == deny["Sid"]:
                    if statement != deny:
                        raise ValueError(
                            "workspace retirement policy conflicts with an existing statement"
                        )
                    break
            else:
                statements.append(deny)
            policy["Statement"] = statements
            # Fence live grants before purging. The control-plane role retains administration.
            administration.put_bucket_policy(
                Bucket=bucket, ExpectedBucketOwner=owner, Policy=json.dumps(policy)
            )
        finally:
            administration.close()
        client = S3ObjectStoreClient.from_settings(self.settings)
        try:
            client.retire_bucket(bucket)
        finally:
            client.close()


__all__ = ["AwsWorkspaceStorageIssuer", "AwsWorkspaceStorageSettings"]
