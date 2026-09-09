from __future__ import annotations

from dataclasses import dataclass

from pydantic import AwareDatetime, Field

from shared.billing_credits import CreditBalance
from shared.contracts import ContractModel

FUNDING_PERMIT_SECONDS = 90
FUNDING_RENEWAL_SECONDS = 30
FUNDING_SHUTDOWN_GRACE_SECONDS = 10


class FundingPermit(ContractModel):
    container_id: str = Field(min_length=1, max_length=160)
    revision: int = Field(ge=1)
    valid_until: AwareDatetime


@dataclass(frozen=True, slots=True)
class FundingBalance:
    credits: CreditBalance
    held_nanos: int
    debt_nanos: int
    available_nanos: int


__all__ = [
    "FUNDING_PERMIT_SECONDS",
    "FUNDING_RENEWAL_SECONDS",
    "FUNDING_SHUTDOWN_GRACE_SECONDS",
    "FundingBalance",
    "FundingPermit",
]
