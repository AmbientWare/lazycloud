from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_periods import BillingPeriodRepository
from shared.billing_accounts import BillingPlan
from shared.billing_periods import BillingPeriod
from shared.billing_plans import DEFAULT_BILLING_PLANS
from sqlalchemy.orm import Session


def month_bounds(day: date) -> tuple[date, date]:
    """The month a day belongs to, as a half-open interval.

    Exclusive end so consecutive months tile: a day is in exactly one period, and
    the last day of a month is not also the first of the next.
    """

    start = day.replace(day=1)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


@dataclass(frozen=True, slots=True)
class BillingPeriodService:
    """Opens and closes what an account is billed for."""

    session: Session

    def open_for_month(self, *, user_id: str, day: date) -> BillingPeriod:
        start, end = month_bounds(day)
        plan = self._plan_for(user_id)
        return BillingPeriodRepository(self.session).open_period(
            user_id=user_id,
            period_start=start,
            period_end=end,
            plan=plan,
            currency=DEFAULT_BILLING_PLANS.for_plan(plan).currency,
        )

    def close_for_month(self, *, user_id: str, day: date) -> BillingPeriod:
        """Settle the month: total the usage, apply the plan, freeze the result.

        The plan is read now and written onto the period, so an account that
        upgraded mid-month is billed on what it ends the month holding rather
        than on whatever it was when the period first opened. Whatever it is, the
        numbers stop moving here: a rate change tomorrow moves next month.
        """

        start, end = month_bounds(day)
        periods = BillingPeriodRepository(self.session)
        plan = self._plan_for(user_id)
        config = DEFAULT_BILLING_PLANS.for_plan(plan)
        periods.open_period(
            user_id=user_id,
            period_start=start,
            period_end=end,
            plan=plan,
            currency=config.currency,
        )
        usage_cost_nanos = periods.usage_cost_for(
            user_id=user_id, period_start=start, period_end=end
        )
        return periods.close_period(
            user_id=user_id,
            period_start=start,
            plan=plan,
            usage_cost_nanos=usage_cost_nanos,
            included_cost_nanos=config.included_cost_nanos,
            subscription_cost_nanos=config.monthly_price_nanos,
            charged_cost_nanos=config.charge_for(usage_cost_nanos),
        )

    def _plan_for(self, user_id: str) -> BillingPlan:
        account = BillingAccountRepository(self.session).get_by_user(user_id)
        return account.plan if account is not None else BillingPlan.Free


__all__ = ["BillingPeriodService", "month_bounds"]
