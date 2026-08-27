from __future__ import annotations

import http.client
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from compute.aws_connections import (
    AwsAccountConnectionService,
    AwsAccountPoolDrainer,
    AwsConnectionCapacityBaseline,
    ConnectedCloudAdmission,
)
from compute.bucket_access import AwsDeploymentBucketAccessService
from compute.catalog import ComputeCatalogRegion
from compute.context import ComputeContext
from coordination.redis_client import RedisClient
from networking.settings import (
    BackendRouteSettings,
    TailnetControlSettings,
    TailnetRuntimeSettings,
)
from observability.workspace_changes import WorkspaceChangePublisher
from provider_aws import require_resolvable_aws_credentials
from provider_aws.provider_node_identity import AWS_STS_PROOF_CONNECT_TIMEOUT_SECONDS
from provider_clients import configured_aws_account_connection_components
from provider_clients.provider_nodes import (
    ProviderNodeIdentityHttpError,
    ProviderNodeIdentityHttpResponse,
    ProviderNodeIdentityReplayError,
)
from provider_clients.settings import AwsAccountConnectionSettings, AwsCapacitySettings
from redis.exceptions import RedisError


@dataclass(frozen=True, slots=True)
class AwsAccountConnectionComposition:
    service: AwsAccountConnectionService
    deployment_bucket_access: AwsDeploymentBucketAccessService


@dataclass(frozen=True, slots=True)
class RedisProviderNodeIdentityReplayGuard:
    redis: RedisClient

    def claim_once(self, *, proof_sha256: str, expires_at: datetime) -> bool:
        normalized_expiry = (
            expires_at.replace(tzinfo=UTC)
            if expires_at.tzinfo is None
            else expires_at.astimezone(UTC)
        )
        ttl_seconds = max(math.ceil((normalized_expiry - datetime.now(UTC)).total_seconds()), 1)
        try:
            return bool(
                self.redis.set(
                    self.redis.key("provider-node", "identity-proof", proof_sha256),
                    "claimed",
                    ex=ttl_seconds,
                    nx=True,
                )
            )
        except RedisError as exc:
            raise ProviderNodeIdentityReplayError(
                "provider node identity replay claim failed"
            ) from exc


@dataclass(frozen=True, slots=True)
class BoundedProviderNodeIdentityHttpClient:
    user_agent: str = "provider-node-identity/1"

    def execute_presigned_get(
        self,
        *,
        url: str,
        timeout_seconds: float,
        max_response_bytes: int,
        follow_redirects: bool,
    ) -> ProviderNodeIdentityHttpResponse:
        if follow_redirects:
            raise ProviderNodeIdentityHttpError("redirects are not supported")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname is None:
            raise ProviderNodeIdentityHttpError("identity proof URL is not HTTPS")
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        connection = http.client.HTTPSConnection(
            parsed.hostname,
            parsed.port or 443,
            timeout=min(AWS_STS_PROOF_CONNECT_TIMEOUT_SECONDS, timeout_seconds),
        )
        try:
            # Connect on the short budget, then read on the full one: a
            # black-holed endpoint must not spend the whole budget twice.
            connection.connect()
            if connection.sock is not None:
                connection.sock.settimeout(timeout_seconds)
            connection.request(
                "GET",
                target,
                headers={"Accept": "application/xml", "User-Agent": self.user_agent},
            )
            response = connection.getresponse()
            content_length = _content_length(response.getheader("Content-Length"))
            body = response.read(max_response_bytes + 1)
            return _ProviderNodeIdentityHttpResponse(
                status_code=response.status,
                body=body,
                content_type=response.getheader("Content-Type", ""),
                content_length=content_length,
            )
        except (OSError, TimeoutError, http.client.HTTPException, ValueError) as exc:
            raise ProviderNodeIdentityHttpError("identity proof request failed") from exc
        finally:
            connection.close()


@dataclass(slots=True)
class _ProviderNodeIdentityHttpResponse:
    status_code: int
    body: bytes
    content_type: str
    content_length: int | None


def _content_length(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return -1


def aws_account_connection_composition_from_settings(
    *,
    context: ComputeContext,
    pool_drainer: AwsAccountPoolDrainer,
    connection_settings: AwsAccountConnectionSettings,
    capacity_settings: AwsCapacitySettings,
    gateway_origin: str,
    internal_origin: str,
    tailnet_runtime: TailnetRuntimeSettings,
    tailnet_control: TailnetControlSettings,
    backend_route: BackendRouteSettings,
    workspace_changes: WorkspaceChangePublisher,
    capacity_baseline: AwsConnectionCapacityBaseline,
    available_catalog: tuple[ComputeCatalogRegion, ...],
    admission: ConnectedCloudAdmission,
) -> AwsAccountConnectionComposition | None:
    # Connected AWS is an optional deployment shape, but a half-configured one is not:
    # the settings validator already rejected that, so absence here is genuine absence.
    if not connection_settings.configured:
        return None
    components = configured_aws_account_connection_components(
        connection_settings,
        capacity=capacity_settings,
        gateway_origin=gateway_origin,
        internal_origin=internal_origin,
        tailnet_runtime=tailnet_runtime,
        tailnet_control=tailnet_control,
        backend_route=backend_route,
    )
    bucket_access = AwsDeploymentBucketAccessService(
        context=context,
        controller=components.bucket_access,
    )
    service = AwsAccountConnectionService(
        context=context,
        authorization_planner=components.authorization_planner,
        validator=components.validator,
        authorization_lifecycle=components.authorization_lifecycle,
        pool_drainer=pool_drainer,
        admission=admission,
        bucket_access_reconciler=bucket_access,
        capacity_baseline=capacity_baseline,
        workspace_changes=workspace_changes,
        available_catalog=available_catalog,
        external_id_bytes=connection_settings.external_id_bytes,
        draft_ttl_seconds=connection_settings.draft_ttl_seconds,
        cleanup_tombstone_ttl_seconds=connection_settings.cleanup_tombstone_ttl_seconds,
        cleanup_timeout_seconds=connection_settings.cleanup_timeout_seconds,
    )
    return AwsAccountConnectionComposition(
        service=service,
        deployment_bucket_access=bucket_access,
    )


def require_connected_aws_deployment_credentials(
    connection_settings: AwsAccountConnectionSettings,
) -> None:
    """Refuse to start a connected-AWS deployment that mounts no credentials.

    A startup precondition, not a step of composition: credentials resolve from
    the ambient environment rather than from settings, and the service graph must
    build from what it is handed. Checking here keeps that read at the process
    entrypoint and still fails a stack that mounts nothing, instead of letting it
    report healthy and fail every connection later.
    """
    if not connection_settings.configured:
        return
    require_resolvable_aws_credentials()


__all__ = [
    "AwsAccountConnectionComposition",
    "BoundedProviderNodeIdentityHttpClient",
    "RedisProviderNodeIdentityReplayGuard",
    "aws_account_connection_composition_from_settings",
    "require_connected_aws_deployment_credentials",
]
