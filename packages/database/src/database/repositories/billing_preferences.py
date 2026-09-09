from dataclasses import dataclass
from datetime import datetime

from database.repositories.billing import BillingAccountRepository
from database.tables.billing_ledger import BillingLedgerSegmentTable
from database.tables.billing_preferences import BillingPreferencesTable
from shared.billing_quotes import BilledDimension
from shared.errors import NotFoundError
from shared.http.billing_preferences import BillingPreferences
from sqlalchemy import Numeric, cast, func, select
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class BillingPreferencesRepository:
    session: Session

    def get(self, user_id: str) -> BillingPreferences:
        row = self.session.get(BillingPreferencesTable, user_id)
        return BillingPreferences.model_validate(row) if row is not None else BillingPreferences()

    def set(self, user_id: str, preferences: BillingPreferences) -> BillingPreferences:
        if BillingAccountRepository(self.session).get_by_user(user_id, for_update=True) is None:
            raise NotFoundError("the billing account does not exist")
        row = self.session.get(BillingPreferencesTable, user_id)
        if row is None:
            row = BillingPreferencesTable(user_id=user_id)
            self.session.add(row)
        row.monthly_usage_limit_nanos = preferences.monthly_usage_limit_nanos
        self.session.flush()
        return BillingPreferences.model_validate(row)

    def gross_usage(
        self, *, user_id: str, start: datetime, end: datetime, container_id: str | None = None
    ) -> int:
        segment = BillingLedgerSegmentTable
        duration = func.extract("epoch", segment.segment_ended_at - segment.segment_started_at)
        cost = cast(segment.cost_nanos, Numeric(asdecimal=True))
        upper = func.extract(
            "epoch", func.least(segment.segment_ended_at, end) - segment.segment_started_at
        )
        lower = func.extract(
            "epoch", func.greatest(segment.segment_started_at, start) - segment.segment_started_at
        )
        statement = select(
            func.coalesce(
                func.sum(func.floor(cost * upper / duration) - func.floor(cost * lower / duration)),
                0,
            )
        ).where(
            segment.owner_user_id == user_id,
            segment.segment_started_at < end,
            segment.segment_ended_at > start,
        )
        if container_id is not None:
            statement = statement.where(
                segment.subject_type == "container",
                segment.subject_id == container_id,
                segment.dimension == BilledDimension.ComputeRuntime.value,
            )
        return int(self.session.scalar(statement) or 0)


__all__ = ["BillingPreferencesRepository"]
