from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from boto3.session import Session
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX
from shared.deployment_settings import MissingDeploymentSettingError
from shared.identity import WorkspaceStorageConfig
from shared.timestamps import utc_now
from shared.workspace_storage import WorkspaceStorageGrant

from provider_aws.account_connection import AwsConnectionStsClient


def _ambient_sts_client(region_name: str) -> AwsConnectionStsClient:
    """STS as whatever identity this process already has, and no profile."""
    return Session(region_name=region_name).client("sts")


def _workspace_session_name(workspace_id: str) -> str:
    """A session name AWS accepts, derived so it is stable and never too long.

    The identifier appears in CloudTrail against every call the credential makes,
    which is what ties an action in a bucket back to the workspace it was for.
    """
    digest = hashlib.sha256(workspace_id.encode()).hexdigest()[:16]
    return f"workspace-storage-{digest}"


MINIMUM_SESSION_SECONDS = 900
"""The shortest session AWS will issue, whatever is asked for."""

DEFAULT_SESSION_SECONDS = 3600
"""One hour, which is also the ceiling when a role is reached by chaining."""


class AwsWorkspaceStorageSettings(BaseSettings):
    """The role a workspace's credential is cut from.

    A role ARN has no meaningful empty, unlike the object-store endpoint beside
    it, so absence is raised rather than defaulted.
    """

    role_arn: str = ""
    region_name: str = "us-east-1"
    session_seconds: int = Field(default=DEFAULT_SESSION_SECONDS, ge=MINIMUM_SESSION_SECONDS)

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_WORKSPACE_STORAGE_",
        extra="ignore",
    )

    def require_role_arn(self) -> str:
        if not self.role_arn:
            raise MissingDeploymentSettingError(
                f"{ENV_PREFIX}_WORKSPACE_STORAGE_ROLE_ARN",
                purpose="the role a workspace's storage credential is cut from",
            )
        return self.role_arn


def workspace_bucket_session_policy(bucket: str, *, partition: str = "aws") -> dict[str, object]:
    """Everything a container may do in one workspace's bucket, and nothing elsewhere.

    A session policy intersects with the role's own permissions, so this narrows
    a role that can reach every workspace bucket down to the one being asked for.
    The role stays broad because the set of workspaces is not known until
    customers exist; the session is what makes a credential specific.

    `s3:ListBucket` is on the bucket and the object actions are on its contents,
    which are different ARNs — a policy naming only one of them produces a mount
    that lists and cannot read, or reads and cannot list.
    """
    if not bucket:
        raise ValueError("workspace storage session policy requires a bucket")
    # Written for size as much as for meaning. A session policy is packed into a
    # budget it shares with the caller's session tags, and EKS Pod Identity
    # spends roughly half of it before this is added, so the spacious form —
    # two statements, both ARNs spelled out, every action named — does not fit
    # and STS refuses the call.
    #
    # One trailing wildcard covers the bucket and its objects. It cannot reach a
    # second bucket: every workspace bucket is this prefix followed by a
    # fixed-length id, so no bucket has another's full name as a prefix, and the
    # role this is cut from reaches nothing outside that prefix either way.
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:DeleteObject",
                    "s3:ListBucket",
                    "s3:AbortMultipartUpload",
                ],
                "Resource": f"arn:{partition}:s3:::{bucket}*",
            }
        ],
    }


@dataclass(frozen=True, slots=True)
class AwsWorkspaceStorageIssuer:
    """Cuts a workspace's credential from one role, scoped to one bucket.

    Nothing is stored per workspace: AWS has no per-bucket access key, so every
    credential is minted on demand and expires. That is not a preference — the
    alternative is an IAM identity per workspace, which stops at the account's
    5,000-user ceiling and puts a permanent key on a worker.
    """

    settings: AwsWorkspaceStorageSettings
    sts_factory: Callable[[str], AwsConnectionStsClient] = _ambient_sts_client
    """An STS client speaking as the workload's own role.

    Deliberately not the connected-account session: that applies the profile
    which chains onto the control principal, and the principal exists to cross
    into a customer's account. A workspace's bucket is this deployment's own
    storage, so the call belongs to the role the pod already holds.
    """

    def issue(
        self,
        *,
        workspace_id: str,
        storage: WorkspaceStorageConfig,
    ) -> WorkspaceStorageGrant:
        bucket = storage.bucket or ""
        if not bucket:
            raise ValueError(f"workspace {workspace_id!r} has no bucket to grant")
        seconds = max(self.settings.session_seconds, MINIMUM_SESSION_SECONDS)
        assumed = self.sts_factory(self.settings.region_name).assume_role(
            RoleArn=self.settings.require_role_arn(),
            RoleSessionName=_workspace_session_name(workspace_id),
            DurationSeconds=seconds,
            Policy=json.dumps(workspace_bucket_session_policy(bucket), separators=(",", ":")),
        )
        credentials = assumed["Credentials"]
        if not isinstance(credentials, dict):
            raise TypeError("assume-role response carried no credentials")
        expiration = credentials.get("Expiration")
        return WorkspaceStorageGrant(
            endpoint_url=storage.endpoint_url,
            region=storage.region or self.settings.region_name,
            bucket_name=bucket,
            prefix=storage.key_prefix,
            force_path_style=storage.force_path_style,
            access_key=str(credentials["AccessKeyId"]),
            secret_key=str(credentials["SecretAccessKey"]),
            session_token=str(credentials["SessionToken"]),
            # STS states its own expiry, and it is authoritative over the duration
            # asked for: a chained role is capped at an hour however long a caller
            # requests, so trusting the request would refresh too late.
            expires_at=expiration
            if expiration is not None
            else utc_now() + timedelta(seconds=seconds),
        )


__all__ = [
    "DEFAULT_SESSION_SECONDS",
    "MINIMUM_SESSION_SECONDS",
    "AwsWorkspaceStorageIssuer",
    "AwsWorkspaceStorageSettings",
    "workspace_bucket_session_policy",
]
