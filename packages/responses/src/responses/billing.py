from datetime import datetime

from pydantic import BaseModel


class BillingCycleResponse(BaseModel):
    """Billing cycle dates from the user's active subscription."""

    current_period_start: datetime
    current_period_end: datetime
