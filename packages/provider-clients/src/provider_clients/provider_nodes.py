from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from compute.provider_nodes import (
    ProviderNodeIdentityProof,
    ProviderNodeIdentityVerifier,
    VerifiedProviderNodeIdentity,
)
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
from pydantic import SecretStr
from shared.aws_connections import AwsAccountConnection
from shared.compute_policy import ComputeUnitRecord
from shared.errors import InvalidInputError, UpstreamUnavailableError
from shared.provider_config import ProviderKind
from shared.timestamps import utc_now


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


class ProviderNodeIdentityEvidenceError(RuntimeError):
    """Provider-backed node identity evidence could not be created."""


class ProviderNodeIdentityEvidenceProvider(Protocol):
    def create(
        self,
        *,
        expected_region: str | None = None,
    ) -> ProviderNodeIdentityEvidence: ...


@dataclass(frozen=True, slots=True)
class _AwsProviderNodeIdentityEvidenceProvider:
    provider: AwsEc2ProviderNodeIdentityProofProvider

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


def provider_node_identity_evidence_provider(
    provider: ProviderKind,
) -> ProviderNodeIdentityEvidenceProvider:
    if provider is ProviderKind.Aws:
        return _AwsProviderNodeIdentityEvidenceProvider(AwsEc2ProviderNodeIdentityProofProvider())
    raise ValueError(f"provider node identity {provider.value!r} is not supported")


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
        connection: AwsAccountConnection,
        provider_instance_ids: tuple[str, ...],
    ) -> VerifiedProviderNodeIdentity:
        if proof.provider is not ProviderKind.Aws:
            raise InvalidInputError(f"unsupported provider node identity: {proof.provider.value}")
        if connection.node_role_arn is None or connection.node_instance_profile_arn is None:
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
    "provider_node_identity_evidence_provider",
]
