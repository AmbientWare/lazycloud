from datetime import datetime
from uuid import UUID

from pydantic import Field

from shared.billing_preferences import AutomaticReloadPauseReason
from shared.credit_payments import MAX_CREDIT_PURCHASE_CENTS, MIN_CREDIT_PURCHASE_CENTS
from shared.http.base import HttpModel


class BillingPreferences(HttpModel):
    monthly_usage_limit_nanos: int | None = Field(default=None, ge=0, le=2**53 - 1, strict=True)
    reload_enabled: bool = False
    reload_threshold_cents: int = Field(
        default=1000, ge=0, le=MAX_CREDIT_PURCHASE_CENTS, strict=True
    )
    reload_amount_cents: int = Field(
        default=2000,
        ge=MIN_CREDIT_PURCHASE_CENTS,
        le=MAX_CREDIT_PURCHASE_CENTS,
        strict=True,
    )


class AutomaticReloadStatus(HttpModel):
    paused_purchase_id: UUID | None
    pause_reason: AutomaticReloadPauseReason | None
    pending_purchase_id: UUID | None
    month_started_at: datetime
    month_ended_at: datetime
    monthly_payment_committed_cents: int = Field(ge=0)


__all__ = ["AutomaticReloadStatus", "BillingPreferences"]
