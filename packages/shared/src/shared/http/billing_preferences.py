from pydantic import Field

from shared.http.base import HttpModel


class BillingPreferences(HttpModel):
    monthly_usage_limit_nanos: int | None = Field(default=None, ge=0, le=2**53 - 1, strict=True)


__all__ = ["BillingPreferences"]
