from __future__ import annotations

from shared.enums import StringEnum


class BillingPlanId(StringEnum):
    """Which plan an account is on.

    The one name the payment protocol, the account row, the JSON contract and the
    published rate card all use, so a plan cannot be a string in one of them and
    a different string in another.

    Lives apart from the rate card so a caller that only has to say *which* plan
    — the provider protocol, the account row — does not import every price
    published beside it.
    """

    Free = "free"
    Team = "team"


__all__ = ["BillingPlanId"]
