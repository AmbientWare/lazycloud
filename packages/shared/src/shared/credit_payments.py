from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from shared.contracts import ContractModel
from shared.enums import StringEnum

MIN_CREDIT_PURCHASE_CENTS = 2_000
MAX_CREDIT_PURCHASE_CENTS = 100_000


class CreditPaymentStatus(StringEnum):
    Pending = "pending"
    ActionRequired = "action_required"
    Succeeded = "succeeded"
    Declined = "declined"
    Cancelled = "cancelled"


class CreditPurchaseKind(StringEnum):
    Manual = "manual"
    Automatic = "automatic"


class CreditPurchaseCheckout(ContractModel):
    provider_session_id: str = Field(min_length=1, max_length=255)
    provider_customer_id: str = Field(min_length=1, max_length=255)
    purchase_id: str = Field(min_length=1, max_length=255)
    provider_payment_id: str = Field(default="", max_length=255)
    url: str | None = None
    expires_at: datetime
    expired: bool


class CreditPayment(ContractModel):
    provider_payment_id: str = Field(min_length=1, max_length=255)
    provider_customer_id: str = Field(min_length=1, max_length=255)
    purchase_id: str = Field(min_length=1, max_length=255)
    status: CreditPaymentStatus
    amount_nanos: int = Field(gt=0, strict=True)
    received_nanos: int = Field(ge=0, strict=True)
    refunded_nanos: int = Field(ge=0, strict=True)
    disputed_nanos: int = Field(ge=0, strict=True)
    confirmation_required: bool = False

    @model_validator(mode="after")
    def bounded_amounts(self) -> CreditPayment:
        if self.confirmation_required and self.status is not CreditPaymentStatus.Pending:
            raise ValueError("only a pending payment can require confirmation")
        if self.received_nanos > self.amount_nanos or self.refunded_nanos > self.received_nanos:
            raise ValueError("payment amounts do not reconcile")
        if self.disputed_nanos > self.received_nanos:
            raise ValueError("disputed funds exceed the captured payment")
        if (
            self.status is CreditPaymentStatus.Succeeded
            and self.received_nanos != self.amount_nanos
        ):
            raise ValueError("a successful credit payment must capture its full amount")
        return self

    @property
    def retained_nanos(self) -> int:
        return max(0, self.received_nanos - self.refunded_nanos - self.disputed_nanos)
