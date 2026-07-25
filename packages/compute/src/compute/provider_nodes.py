from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import SecretStr
from shared.aws_connections import AwsAccountConnection
from shared.compute_policy import ComputePoolRecord
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


class ProviderNodeIdentityVerifier(Protocol):
    def verify(
        self,
        proof: ProviderNodeIdentityProof,
        *,
        pool: ComputePoolRecord,
        connection: AwsAccountConnection,
        provider_instance_ids: tuple[str, ...],
    ) -> VerifiedProviderNodeIdentity: ...


__all__ = [
    "ProviderNodeIdentityProof",
    "ProviderNodeIdentityVerifier",
    "VerifiedProviderNodeIdentity",
]
