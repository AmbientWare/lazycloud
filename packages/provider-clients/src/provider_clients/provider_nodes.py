from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Protocol

from agent.provider_identity import ProviderHostCredentials
from compute.provider_nodes import (
    ProviderNodeIdentityProof,
    ProviderNodeIdentityVerifier,
    VerifiedProviderNodeIdentity,
)
from coordination.redis_client import RedisClient
from coordination.request_cooldown import RedisRequestCooldown
from provider_aws import (
    AwsEc2ProviderNodeIdentityProofProvider,
    AwsProviderNodeIdentityError,
    AwsProviderNodeIdentityErrorCode,
    AwsProviderNodeIdentityTarget,
    AwsProviderNodeIdentityTransportError,
    AwsProviderNodeProofError,
    AwsProviderNodeReplayGuardError,
    AwsStsGetCallerIdentityProof,
    AwsStsProofHttpResponse,
    provider_node_identity,
)
from provider_hetzner.client import HetznerClient
from provider_hetzner.identity import verify_node as verify_hetzner_node
from provider_hyperstack.client import HyperstackClient
from provider_hyperstack.identity import verify_node as verify_hyperstack_node
from provider_ovh.client import OvhClient
from provider_ovh.identity import verify_node as verify_ovh_node
from pydantic import SecretStr
from shared.aws_connections import AwsAccountConnection
from shared.compute_policy import ComputeUnitRecord
from shared.errors import InvalidInputError, UpstreamUnavailableError
from shared.provider_config import ProviderKind
from shared.provider_identity import (
    ProviderBootstrapNodeEvidence,
    ProviderBootstrapNodeIdentityTarget,
)
from shared.timestamps import utc_now

from provider_clients.settings import PlatformCapacitySettings


class ProviderNodeIdentityHttpResponse(Protocol):
    status_code: int
    body: bytes
    content_type: str
    content_length: int | None


class ProviderNodeIdentityHttpClient(Protocol):
    def execute_presigned_get(
        self,
        *,
        url: str,
        timeout_seconds: float,
        max_response_bytes: int,
        follow_redirects: bool,
    ) -> ProviderNodeIdentityHttpResponse: ...


class ProviderNodeIdentityHttpError(RuntimeError):
    """The bounded provider identity request could not complete."""


class ProviderNodeIdentityReplayGuard(Protocol):
    def claim_once(self, *, proof_sha256: str, expires_at: datetime) -> bool: ...


class ProviderNodeIdentityReplayError(RuntimeError):
    """The durable provider identity replay claim could not complete."""


@dataclass(frozen=True, slots=True)
class ProviderNodeIdentityEvidence:
    provider: ProviderKind
    region: str
    provider_instance_id: str
    proof_url: SecretStr
    launch_id: str = ""
    bootstrap_token: SecretStr = field(default_factory=lambda: SecretStr(""), repr=False)
    node_agent_token: SecretStr = field(default_factory=lambda: SecretStr(""), repr=False)


class ProviderNodeIdentityEvidenceError(RuntimeError):
    """Provider-backed node identity evidence could not be created."""


class ProviderNodeIdentityEvidenceProvider(Protocol):
    def create(
        self,
        *,
        expected_region: str | None = None,
    ) -> ProviderNodeIdentityEvidence: ...

    def acknowledge(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _AwsProviderNodeIdentityEvidenceProvider:
    provider: AwsEc2ProviderNodeIdentityProofProvider

    def acknowledge(self) -> None:
        pass

    def create(
        self,
        *,
        expected_region: str | None = None,
    ) -> ProviderNodeIdentityEvidence:
        try:
            proof = self.provider.create(expected_region=expected_region)
        except AwsProviderNodeProofError as exc:
            raise ProviderNodeIdentityEvidenceError(str(exc)) from exc
        return ProviderNodeIdentityEvidence(
            provider=ProviderKind.Aws,
            region=proof.region,
            provider_instance_id=proof.instance_id,
            proof_url=proof.presigned_url,
        )


@dataclass(frozen=True, slots=True)
class _BootstrapProviderNodeIdentityEvidenceProvider:
    provider: ProviderKind
    credentials: ProviderHostCredentials

    def create(self, *, expected_region: str | None = None) -> ProviderNodeIdentityEvidence:
        token = self.credentials.node_token()
        region = self.credentials.region()
        if expected_region is not None and region != expected_region:
            raise ProviderNodeIdentityEvidenceError("provider node is in the wrong region")
        return ProviderNodeIdentityEvidence(
            provider=self.provider,
            region=region,
            provider_instance_id=self.credentials.instance_id(),
            proof_url=SecretStr("provider-bootstrap"),
            launch_id=self.credentials.launch_id(),
            bootstrap_token=self.credentials.bootstrap_token(),
            node_agent_token=token,
        )

    def acknowledge(self) -> None:
        self.credentials.acknowledge()


def provider_node_identity_evidence_provider(
    provider: ProviderKind,
    *,
    state_dir: Path,
) -> ProviderNodeIdentityEvidenceProvider:
    if provider is ProviderKind.Aws:
        return _AwsProviderNodeIdentityEvidenceProvider(AwsEc2ProviderNodeIdentityProofProvider())
    return _BootstrapProviderNodeIdentityEvidenceProvider(
        provider, ProviderHostCredentials(state_dir)
    )


@dataclass(frozen=True, slots=True)
class _AwsProofHttpClient:
    client: ProviderNodeIdentityHttpClient

    def execute_presigned_get(
        self,
        *,
        url: str,
        timeout_seconds: float,
        max_response_bytes: int,
        follow_redirects: bool,
    ) -> AwsStsProofHttpResponse:
        try:
            response = self.client.execute_presigned_get(
                url=url,
                timeout_seconds=timeout_seconds,
                max_response_bytes=max_response_bytes,
                follow_redirects=follow_redirects,
            )
        except ProviderNodeIdentityHttpError as exc:
            raise AwsProviderNodeIdentityTransportError(str(exc)) from exc
        return AwsStsProofHttpResponse(
            status_code=response.status_code,
            body=response.body,
            content_type=response.content_type,
            content_length=response.content_length,
        )


@dataclass(frozen=True, slots=True)
class _AwsReplayGuard:
    guard: ProviderNodeIdentityReplayGuard

    def claim_once(self, *, proof_sha256: str, expires_at: datetime) -> bool:
        try:
            return self.guard.claim_once(proof_sha256=proof_sha256, expires_at=expires_at)
        except ProviderNodeIdentityReplayError as exc:
            raise AwsProviderNodeReplayGuardError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class AwsProviderNodeIdentityAdapter(ProviderNodeIdentityVerifier):
    http_client: ProviderNodeIdentityHttpClient
    replay_guard: ProviderNodeIdentityReplayGuard

    def verify(
        self,
        proof: ProviderNodeIdentityProof,
        *,
        pool: ComputeUnitRecord,
        connection: AwsAccountConnection | None,
        provider_instance_ids: tuple[str, ...],
    ) -> VerifiedProviderNodeIdentity:
        if proof.provider is not ProviderKind.Aws:
            raise InvalidInputError(f"unsupported provider node identity: {proof.provider.value}")
        if (
            connection is None
            or connection.node_role_arn is None
            or connection.node_instance_profile_arn is None
        ):
            raise UpstreamUnavailableError("AWS node identity is not ready")
        if not pool.provider_state.resource_id:
            raise UpstreamUnavailableError("AWS provider pool identity is not ready")
        try:
            verified = provider_node_identity.AwsProviderNodeIdentityVerifier(
                http_client=_AwsProofHttpClient(self.http_client),
                replay_guard=_AwsReplayGuard(self.replay_guard),
            ).verify(
                AwsStsGetCallerIdentityProof(
                    presigned_url=proof.proof_url,
                    region=proof.region,
                    instance_id=proof.provider_instance_id,
                ),
                target=AwsProviderNodeIdentityTarget(
                    account_id=connection.account_id,
                    region=pool.region,
                    node_role_arn=connection.node_role_arn,
                    node_instance_profile_arn=connection.node_instance_profile_arn,
                    autoscaling_group_name=pool.provider_state.resource_id,
                ),
                provider_machine_ids=provider_instance_ids,
            )
        except AwsProviderNodeIdentityError as exc:
            if exc.code in {
                AwsProviderNodeIdentityErrorCode.UpstreamUnavailable,
                AwsProviderNodeIdentityErrorCode.ResourceNotFound,
                AwsProviderNodeIdentityErrorCode.ResourceNotReady,
            }:
                raise UpstreamUnavailableError(
                    f"AWS instance identity could not be verified: {exc.detail}"
                ) from exc
            raise InvalidInputError(
                f"AWS instance identity could not be verified: {exc.detail}"
            ) from exc
        return VerifiedProviderNodeIdentity(
            provider=ProviderKind.Aws,
            account_id=verified.account_id,
            region=verified.region,
            provider_instance_id=verified.instance_id,
            role_arn=verified.node_role_arn,
            instance_profile_arn=verified.node_instance_profile_arn,
            provider_resource_id=verified.autoscaling_group_name,
            verified_at=utc_now(),
        )


class ProviderBootstrapNodeVerifier(Protocol):
    def __call__(
        self, *, instance_id: str, target: ProviderBootstrapNodeIdentityTarget
    ) -> ProviderBootstrapNodeEvidence: ...


@dataclass(frozen=True, slots=True)
class ProviderNodeIdentityRegistry:
    aws: AwsProviderNodeIdentityAdapter
    bootstrap_nodes: Mapping[str, ProviderBootstrapNodeVerifier] = field(
        default_factory=lambda: dict[str, ProviderBootstrapNodeVerifier]()
    )

    def verify(
        self,
        proof: ProviderNodeIdentityProof,
        *,
        pool: ComputeUnitRecord,
        connection: AwsAccountConnection | None,
        provider_instance_ids: tuple[str, ...],
    ) -> VerifiedProviderNodeIdentity:
        if proof.provider is ProviderKind.Aws:
            return self.aws.verify(
                proof,
                pool=pool,
                connection=connection,
                provider_instance_ids=provider_instance_ids,
            )
        verifier = self.bootstrap_nodes.get(pool.provider_ref)
        if verifier is None:
            raise UpstreamUnavailableError(
                f"provider identity binding {pool.provider_ref!r} is not configured"
            )
        if (
            not pool.provider_ref.startswith(f"{proof.provider.value}:")
            or proof.proof_url.get_secret_value() != "provider-bootstrap"
            or proof.region != pool.region
        ):
            raise InvalidInputError("invalid provider host identity")
        verified = verifier(
            instance_id=proof.provider_instance_id,
            target=ProviderBootstrapNodeIdentityTarget(
                provider_ref=pool.provider_ref,
                unit_id=pool.id,
                launch_id=proof.launch_id,
                region=pool.region,
            ),
        )
        return VerifiedProviderNodeIdentity(
            provider=proof.provider,
            account_id=pool.provider_ref,
            region=verified.region,
            provider_instance_id=verified.instance_id,
            role_arn="",
            instance_profile_arn="",
            provider_resource_id=pool.id,
            verified_at=utc_now(),
        )


def configured_provider_node_identity_registry(
    *,
    aws: AwsProviderNodeIdentityAdapter,
    platform_settings: PlatformCapacitySettings,
    redis: RedisClient,
) -> ProviderNodeIdentityRegistry:
    bootstrap_nodes: dict[str, ProviderBootstrapNodeVerifier] = {}
    for binding in platform_settings.hetzner:
        bootstrap_nodes[binding.ref] = partial(
            verify_hetzner_node,
            HetznerClient(
                platform_settings.hetzner_tokens[binding.ref],
                cooldown=RedisRequestCooldown(redis, binding.ref),
            ),
        )
    for binding in platform_settings.hyperstack:
        bootstrap_nodes[binding.ref] = partial(
            verify_hyperstack_node,
            HyperstackClient(platform_settings.hyperstack_tokens[binding.ref]),
        )
    for binding in platform_settings.ovh:
        credentials = platform_settings.ovh_credentials[binding.ref]
        bootstrap_nodes[binding.ref] = partial(
            verify_ovh_node,
            OvhClient(
                application_key=credentials.application_key,
                application_secret=credentials.application_secret,
                consumer_key=credentials.consumer_key,
                project_id=binding.project_id,
            ),
        )
    return ProviderNodeIdentityRegistry(aws=aws, bootstrap_nodes=bootstrap_nodes)


__all__ = [
    "AwsProviderNodeIdentityAdapter",
    "ProviderNodeIdentityEvidence",
    "ProviderNodeIdentityEvidenceError",
    "ProviderNodeIdentityEvidenceProvider",
    "ProviderNodeIdentityHttpClient",
    "ProviderNodeIdentityHttpError",
    "ProviderNodeIdentityHttpResponse",
    "ProviderNodeIdentityReplayError",
    "ProviderNodeIdentityReplayGuard",
    "configured_provider_node_identity_registry",
    "provider_node_identity_evidence_provider",
]
