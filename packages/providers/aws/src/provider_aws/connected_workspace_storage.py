"""Workspace buckets that live in a customer's connected AWS account.

The platform never holds a standing credential for a connected account. Every
call here assumes the connection role with its external ID, and a workload
credential is that same assumption narrowed by a session policy to one bucket.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Self

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import JsonValue
from shared.aws_connections import AwsAccountConnection
from shared.identity import WorkspaceRecord, WorkspaceStorageConfig
from shared.workspace_storage import WorkspaceStorageGrant
from storage_client.s3 import S3ObjectStoreClient, S3ObjectStoreSettings

from provider_aws.account_connection import (
    AWS_ACCOUNT_CONNECTION_TEMPLATE_VERSION,
    AwsConnectionSessionFactory,
    _AssumeRoleResponse,
    _client_error,
    _default_session,
    _TemporaryCredentials,
    _validated,
)
from provider_aws.instance_catalog import aws_partition_for_region
from provider_aws.provider_control import upstream_error

CONNECTED_WORKSPACE_BUCKET_PREFIX = "lazycloud-workspace"
_GRANT_SECONDS = 900


def connected_workspace_bucket(workspace_id: str) -> str:
    return f"{CONNECTED_WORKSPACE_BUCKET_PREFIX}-{workspace_id}".replace("_", "-")


def connection_storage_region(connection: AwsAccountConnection) -> str:
    """The region a connection's workspace buckets are created in.

    A managed authorization was deployed in one region and that is where the
    connection's network lives, so the bucket joins it there. An existing-role
    authorization has no stack; its first network region stands in.
    """
    authorization = connection.active_authorization
    if authorization is not None:
        if authorization.managed_authorization is not None:
            return authorization.managed_authorization.region
        if authorization.authorization_stack is not None:
            return authorization.authorization_stack.region
    if connection.networks:
        return sorted(connection.networks)[0]
    raise ValueError("connected AWS account has no region to keep workspace storage in")


def _require_current_template(connection: AwsAccountConnection) -> None:
    """Refuse an account authorized before bucket permissions joined the template.

    A ready connection is not enough: the role it holds may predate the
    statement that lets it create buckets, and the failure would otherwise
    arrive as AccessDenied halfway through creating a workspace.
    """
    authorization = connection.active_authorization
    managed = authorization.managed_authorization if authorization is not None else None
    if managed is not None and managed.template_version != AWS_ACCOUNT_CONNECTION_TEMPLATE_VERSION:
        raise ValueError(
            f"AWS account {connection.account_id} was authorized with connection template "
            f"{managed.template_version}; run `lazycloud cloud reconnect` to authorize "
            f"{AWS_ACCOUNT_CONNECTION_TEMPLATE_VERSION} before creating a workspace there"
        )


def _endpoint(region: str) -> str:
    suffix = "amazonaws.com.cn" if aws_partition_for_region(region) == "aws-cn" else "amazonaws.com"
    return f"https://s3.{region}.{suffix}"


def _bucket_policy(partition: str, bucket: str) -> str:
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
    return json.dumps(policy, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class AwsConnectedWorkspaceStorage:
    session_factory: AwsConnectionSessionFactory
    public_origin: str

    @classmethod
    def from_default_chain(cls, *, public_origin: str) -> Self:
        return cls(_default_session, public_origin=public_origin)

    def assert_provisionable(self, connection: AwsAccountConnection) -> None:
        _require_current_template(connection)

    def provision(
        self, workspace: WorkspaceRecord, connection: AwsAccountConnection
    ) -> WorkspaceStorageConfig:
        _require_current_template(connection)
        region = connection_storage_region(connection)
        bucket = connected_workspace_bucket(workspace.id)
        client = self._client(workspace, connection, region=region, bucket=bucket, scoped=False)
        try:
            client.create_bucket(bucket)
            client.validate_bucket_access(bucket)
            client.configure_workspace_bucket(bucket, public_origin=self.public_origin)
        finally:
            client.close()
        return WorkspaceStorageConfig(
            backend="s3",
            bucket=bucket,
            endpoint_url=_endpoint(region),
            region=region,
        )

    def issue(
        self, workspace: WorkspaceRecord, connection: AwsAccountConnection
    ) -> WorkspaceStorageGrant:
        bucket = self._bucket(workspace, connection)
        region = workspace.storage.region
        credentials = self._assume(workspace, connection, region=region, bucket=bucket, scoped=True)
        return WorkspaceStorageGrant(
            endpoint_url=workspace.storage.endpoint_url,
            region=region,
            bucket_name=bucket,
            access_key=credentials.access_key_id.get_secret_value(),
            secret_key=credentials.secret_access_key.get_secret_value(),
            session_token=credentials.session_token.get_secret_value(),
            expires_at=credentials.expiration,
        )

    def retire(self, workspace: WorkspaceRecord, connection: AwsAccountConnection) -> None:
        bucket = self._bucket(workspace, connection)
        client = self._client(
            workspace, connection, region=workspace.storage.region, bucket=bucket, scoped=False
        )
        try:
            client.retire_bucket(bucket)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "NoSuchBucket":
                raise _client_error(exc, operation="retire connected workspace bucket") from exc
        finally:
            client.close()

    def _bucket(self, workspace: WorkspaceRecord, connection: AwsAccountConnection) -> str:
        storage = workspace.storage
        expected = connected_workspace_bucket(workspace.id)
        if (
            workspace.connection_id != connection.id
            or storage.backend != "s3"
            or storage.bucket != expected
            or not storage.region
            or storage.endpoint_url != _endpoint(storage.region)
            or storage.key_prefix
        ):
            raise ValueError("workspace storage does not name its bucket in the connected account")
        return expected

    def _client(
        self,
        workspace: WorkspaceRecord,
        connection: AwsAccountConnection,
        *,
        region: str,
        bucket: str,
        scoped: bool,
    ) -> S3ObjectStoreClient:
        credentials = self._assume(
            workspace, connection, region=region, bucket=bucket, scoped=scoped
        )
        # The settings bucket is the client's notion of a shared platform bucket it
        # must never purge; every call here names the workspace bucket explicitly.
        return S3ObjectStoreClient.from_settings(
            S3ObjectStoreSettings(
                bucket=CONNECTED_WORKSPACE_BUCKET_PREFIX,
                endpoint_url=_endpoint(region),
                region_name=region,
                access_key_id=credentials.access_key_id.get_secret_value(),
                secret_access_key=credentials.secret_access_key.get_secret_value(),
                session_token=credentials.session_token.get_secret_value(),
                credential_expires_at=credentials.expiration,
                workspace_bucket_prefix=CONNECTED_WORKSPACE_BUCKET_PREFIX,
            )
        )

    def _assume(
        self,
        workspace: WorkspaceRecord,
        connection: AwsAccountConnection,
        *,
        region: str,
        bucket: str,
        scoped: bool,
    ) -> _TemporaryCredentials:
        authorization = connection.active_authorization
        if authorization is None:
            raise ValueError("connected AWS account has no active authorization")
        partition = aws_partition_for_region(region)
        session_name = f"workspace-{hashlib.sha256(workspace.id.encode()).hexdigest()[:40]}"
        source = self.session_factory(region_name=region)
        try:
            if scoped:
                assumed = source.client("sts").assume_role(
                    RoleArn=authorization.role_arn,
                    RoleSessionName=session_name,
                    ExternalId=connection.external_id,
                    DurationSeconds=_GRANT_SECONDS,
                    Policy=_bucket_policy(partition, bucket),
                )
            else:
                assumed = source.client("sts").assume_role(
                    RoleArn=authorization.role_arn,
                    RoleSessionName=session_name,
                    ExternalId=connection.external_id,
                    DurationSeconds=_GRANT_SECONDS,
                )
        except ClientError as exc:
            raise _client_error(exc, operation="assume connection role for storage") from exc
        except BotoCoreError as exc:
            raise upstream_error(exc, operation="assume connection role for storage") from exc
        return _validated(
            _AssumeRoleResponse, assumed, operation="assume connection role for storage"
        ).credentials


__all__ = [
    "CONNECTED_WORKSPACE_BUCKET_PREFIX",
    "AwsConnectedWorkspaceStorage",
    "connected_workspace_bucket",
    "connection_storage_region",
]
