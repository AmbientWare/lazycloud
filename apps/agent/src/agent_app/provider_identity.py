"""The signed identity a provider node presents when it enrolls or reports a boot phase."""

from __future__ import annotations

import http.client
from dataclasses import dataclass
from typing import Protocol

from provider_aws.instance_metadata import AwsProviderNodeProofError
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


class ProviderNodeIdentityUnavailableError(ProviderNodeIdentityEvidenceError):
    """The instance metadata service did not answer, as it may not early in a boot."""


class ProviderNodeIdentityEvidenceProvider(Protocol):
    def create(
        self,
        *,
        expected_region: str | None = None,
    ) -> ProviderNodeIdentityEvidence: ...


class _NodeProof(Protocol):
    @property
    def region(self) -> str: ...

    @property
    def instance_id(self) -> str: ...

    @property
    def presigned_url(self) -> SecretStr: ...


class _NodeProofProvider(Protocol):
    def create(self, *, expected_region: str | None = None) -> _NodeProof: ...


@dataclass(frozen=True, slots=True)
class _AwsProviderNodeIdentityEvidenceProvider:
    provider: _NodeProofProvider

    def create(
        self,
        *,
        expected_region: str | None = None,
    ) -> ProviderNodeIdentityEvidence:
        try:
            proof = self.provider.create(expected_region=expected_region)
        except AwsProviderNodeProofError as exc:
            if isinstance(exc.__cause__, OSError | http.client.HTTPException):
                raise ProviderNodeIdentityUnavailableError(str(exc)) from exc
            raise ProviderNodeIdentityEvidenceError(str(exc)) from exc
        return ProviderNodeIdentityEvidence(
            provider=ProviderKind.Aws,
            region=proof.region,
            provider_instance_id=proof.instance_id,
            proof_url=proof.presigned_url,
        )


def provider_node_identity_evidence_provider() -> ProviderNodeIdentityEvidenceProvider:
    # Imported here so a machine a customer joined never loads botocore or the
    # AWS provider at startup; only a provider node asks for this evidence.
    from provider_aws.provider_node_proof import AwsEc2ProviderNodeIdentityProofProvider

    return _AwsProviderNodeIdentityEvidenceProvider(AwsEc2ProviderNodeIdentityProofProvider())
