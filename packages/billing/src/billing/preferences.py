from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from database.repositories.billing_preferences import BillingPreferencesRepository
from shared.billing_preferences import UsageBudget, usage_budget_month
from shared.http.billing_preferences import BillingPreferences
from shared.timestamps import to_utc, utc_now
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class BillingPreferencesService:
    session: Session

    def get(self, *, user_id: str) -> BillingPreferences:
        return BillingPreferencesRepository(self.session).get(user_id)

    def set(self, *, user_id: str, preferences: BillingPreferences) -> BillingPreferences:
        return BillingPreferencesRepository(self.session).set(user_id, preferences)

    def usage_budget(self, *, user_id: str, at: datetime | None = None) -> UsageBudget:
        start, end = usage_budget_month(to_utc(at or utc_now()))
        repository = BillingPreferencesRepository(self.session)
        limit = repository.get(user_id).monthly_usage_limit_nanos
        spent = repository.gross_usage(user_id=user_id, start=start, end=end)
        return UsageBudget(
            start, end, limit, spent, None if limit is None else max(0, limit - spent)
        )
