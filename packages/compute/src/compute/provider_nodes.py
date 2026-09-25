from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import SecretStr
from shared.compute_policy import ComputeUnitRecord
from shared.contracts import ContractModel
from shared.provider_config import ProviderKind


class ProviderNodeIdentityProof(ContractModel):
    provider: ProviderKind
    region: str
    provider_instance_id: str
    proof_url: SecretStr


class VerifiedProviderNodeIdentity(ContractModel):
    provider: ProviderKind
    account_id: str
    region: str
    provider_instance_id: str
    role_arn: str
    instance_profile_arn: str
    provider_resource_id: str
    verified_at: datetime


class ProviderNodeAdmission(ContractModel):
    account_id: str
    machine_role_id: str
    machine_profile_id: str


class ProviderNodeIdentityVerifier(Protocol):
    def verify(
        self,
        proof: ProviderNodeIdentityProof,
        *,
        pool: ComputeUnitRecord,
        admission: ProviderNodeAdmission,
        provider_instance_ids: tuple[str, ...],
    ) -> VerifiedProviderNodeIdentity: ...


class ProviderNodeIdentityProofError(RuntimeError):
    """A provider node could not prove its identity."""


class ProviderNodeIdentityUnavailableError(ProviderNodeIdentityProofError):
    """The provider's metadata service did not answer, as it may not early in a boot."""


class ProviderNodeIdentityProofProvider(Protocol):
    """Signs the identity a provider node presents when it enrolls or reports a boot phase."""

    def create(self, *, expected_region: str | None = None) -> ProviderNodeIdentityProof: ...


__all__ = [
    "ProviderNodeAdmission",
    "ProviderNodeIdentityProof",
    "ProviderNodeIdentityProofError",
    "ProviderNodeIdentityProofProvider",
    "ProviderNodeIdentityUnavailableError",
    "ProviderNodeIdentityVerifier",
    "VerifiedProviderNodeIdentity",
]
