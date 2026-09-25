"""The signed identity a provider node presents when it enrolls or reports a boot phase."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from provider_aws.provider_node_proof import (
    AwsEc2ProviderNodeIdentityProofProvider,
    AwsProviderNodeProofError,
)
from pydantic import SecretStr
from shared.provider_config import ProviderKind


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


def provider_node_identity_evidence_provider() -> ProviderNodeIdentityEvidenceProvider:
    return _AwsProviderNodeIdentityEvidenceProvider(AwsEc2ProviderNodeIdentityProofProvider())
