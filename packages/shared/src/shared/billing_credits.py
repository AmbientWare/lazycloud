from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from shared.enums import StringEnum


class CreditKind(StringEnum):
    Purchased = "purchased"
    Subscription = "subscription"
    Trial = "trial"


@dataclass(frozen=True, slots=True)
class CreditGrant:
    source_id: str
    kind: CreditKind
    amount_nanos: int
    effective_at: datetime
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        for instant in (self.effective_at, self.expires_at):
            if instant is not None and (instant.tzinfo is None or instant.utcoffset() is None):
                raise ValueError("credit eligibility requires timezone-aware timestamps")
        if not self.source_id or len(self.source_id) > 255:
            raise ValueError("a credit grant requires a stable source identifier")
        if self.amount_nanos <= 0:
            raise ValueError("a credit grant must have a positive amount")
        if self.expires_at is not None and self.expires_at <= self.effective_at:
            raise ValueError("credit expiry must follow its effective time")
        if self.kind is CreditKind.Purchased and self.expires_at is not None:
            raise ValueError("purchased funds do not expire")


@dataclass(frozen=True, slots=True)
class CreditSettlement:
    gross_nanos: int
    credited_nanos: int
    payable_nanos: int


__all__ = ["CreditGrant", "CreditKind", "CreditSettlement"]
